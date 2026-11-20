import io
import json
from calendar import month_name
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session

from config import settings, DATA_DIR
from database import Base, engine, get_db, SessionLocal
from integration import hikvision
from integration.csv_import import import_attendance_csv, insert_punches, parse_dt
from integration.employee_import import import_employees, template_bytes
from integration.zk_adms import options_response, parse_attlog
from models import (Company, Department, Employee, Shift, Device, AttendanceLog, DailyAttendance, Holiday, Leave,
                    PayrollAdjustment, PayrollRun, User, AuditLog)
from payroll.engine import split_gross
from reports.excel import salary_sheet, attendance_month, attendance_day
from reports.payslip import payslips_pdf
from security import (hash_password, verify_password, create_token, decode_token, page_user,
                      LoginRequired, COOKIE, ROLES)
from services import mobile, devices, maintenance
from services.audit import audit
from services.payroll_service import run_payroll, finalize_payroll, get_run, PayrollLocked
from services.policy import (load_policy, save_policy, policy_rows, get_setting, set_setting, weekend_set,
                             geofence)
from services.processing import process_month, month_bounds
from timeutil import now_local

BASE_DIR = Path(__file__).resolve().parent
MOBILE_COOKIE = "laila_mobile"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OPEN_PATHS = ("/setup", "/static", "/iclock", "/health", "/m")

Base.metadata.create_all(bind=engine)

app = FastAPI(title=settings.app_name, version="3.0.0", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals.update(settings=settings, month_name=month_name)
_state = {"setup_done": None, "started": False}


# ---------------------------------------------------------------- helpers
def go(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    q = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    return RedirectResponse(url + (("&" if "?" in url else "?") + urlencode(q) if q else ""), status_code=303)


def need(user: dict, *roles: str) -> None:
    if user["role"] != "ADMIN" and user["role"] not in roles:
        raise HTTPException(403, "You do not have permission for this page/action")


def render(request: Request, name: str, user: dict | None, **ctx) -> HTMLResponse:
    ctx.update(request=request, user=user, msg=request.query_params.get("msg", ""),
               err=request.query_params.get("err", ""))
    return templates.TemplateResponse(request, name, ctx)


def dec(value: str, field: str) -> Decimal:
    try:
        return Decimal((value or "0").replace(",", "").strip() or "0")
    except InvalidOperation:
        raise ValueError(f"{field}: not a number")


def parse_month(value: str | None) -> tuple[int, int]:
    if value:
        y, m = value.split("-")[:2]
        return int(y), int(m)
    t = now_local()
    return t.year, t.month


def cookie_kwargs(request: Request, hours: int) -> dict:
    return dict(httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=hours * 3600)


def visible_companies(db: Session, user: dict) -> list[Company]:
    stmt = select(Company).order_by(Company.code)
    if user.get("company_id"):
        stmt = stmt.where(Company.id == user["company_id"])
    return list(db.scalars(stmt))


def company_filter(user: dict, requested: str | int | None) -> int | None:
    """Company id to filter on: the user's own company if limited, else the requested one (None = all)."""
    if user.get("company_id"):
        return user["company_id"]
    try:
        return int(requested) if requested not in (None, "", "0", 0) else None
    except ValueError:
        return None


def check_company(user: dict, company_id: int | None) -> None:
    if user.get("company_id") and company_id != user["company_id"]:
        raise HTTPException(403, "That belongs to another company")


def setup_done(db: Session) -> bool:
    if not _state["setup_done"]:
        _state["setup_done"] = get_setting(db, "setup_done", default="0") == "1"
    return _state["setup_done"]


@app.middleware("http")
async def first_run_redirect(request: Request, call_next):
    if not request.url.path.startswith(OPEN_PATHS):
        db = SessionLocal()
        try:
            done = setup_done(db)
        finally:
            db.close()
        if not done:
            return RedirectResponse("/setup", status_code=303)
    return await call_next(request)


@app.exception_handler(LoginRequired)
async def _login_redirect(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    if "text/html" in request.headers.get("accept", "") and exc.status_code in (403, 404):
        return templates.TemplateResponse(request, "error.html",
                                          {"request": request, "code": exc.status_code, "detail": exc.detail},
                                          status_code=exc.status_code)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.on_event("startup")
def startup():
    if _state["started"]:          # two listeners (HTTP + HTTPS) share one app
        return
    _state["started"] = True
    db = SessionLocal()
    try:
        if db.scalar(select(func.count(Shift.id))) == 0:
            db.add(Shift(code="GENERAL", name="General 09:00-18:00", start=time(9, 0), end=time(18, 0)))
            db.commit()
    finally:
        db.close()
    devices.start_poller()
    maintenance.start_daily_backup()


@app.get("/health")
def health():
    return {"status": "healthy", "app": settings.app_name, "time": now_local().isoformat(sep=" ")}


# ---------------------------------------------------------------- first-run setup wizard
@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db)):
    if setup_done(db):
        return go("/login")
    return render(request, "setup.html", None, ips=maintenance.lan_ips())


@app.post("/setup")
async def setup_save(request: Request, db: Session = Depends(get_db)):
    if setup_done(db):
        return go("/login")
    f = await request.form()
    username = str(f.get("username", "")).strip()
    password = str(f.get("password", ""))
    if len(username) < 3 or len(password) < 8 or password != f.get("password2"):
        return go("/setup", err="Admin username needs 3+ characters, password 8+ characters, and both passwords must match")
    companies = []
    for line in str(f.get("companies", "")).splitlines():
        if not line.strip():
            continue
        code, _, name = line.partition(",")
        code, name = code.strip().upper()[:20], name.strip()[:150]
        if not code or not name:
            return go("/setup", err=f"Company line must be CODE, Name: {line!r}")
        companies.append((code, name))
    if not companies or len({c for c, _ in companies}) != len(companies):
        return go("/setup", err="Enter at least one company, each with a different code")
    weekend = ",".join(str(d) for d in range(7) if f.get(f"wd{d}"))
    db.add(User(username=username, password_hash=hash_password(password), role="ADMIN"))
    for code, name in companies:
        db.add(Company(code=code, name=name))
    set_setting(db, "weekend_days", weekend, commit=False)
    for k in ("geofence_lat", "geofence_lon", "geofence_radius_m"):
        if str(f.get(k, "")).strip():
            float(str(f.get(k)))   # validate
            set_setting(db, k, str(f.get(k)).strip(), commit=False)
    set_setting(db, "setup_done", "1", commit=False)
    audit(db, username, "SETUP", f"fresh setup, companies: {', '.join(c for c, _ in companies)}", commit=False)
    db.commit()
    _state["setup_done"] = True
    resp = go("/devices", msg="Setup complete. Next: connect the punch machines, then import employees.")
    resp.set_cookie(COOKIE, create_token(username, "ADMIN"), **cookie_kwargs(request, settings.session_hours))
    return resp


# ---------------------------------------------------------------- login
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", None)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == username.strip(), User.active == True))  # noqa: E712
    ip = request.client.host if request.client else ""
    if not user or not verify_password(password, user.password_hash):
        audit(db, username[:80], "LOGIN_FAILED", ip)
        return go("/login", err="Wrong username or password")
    audit(db, user.username, "LOGIN", ip)
    resp = go("/")
    resp.set_cookie(COOKIE, create_token(user.username, user.role), **cookie_kwargs(request, settings.session_hours))
    return resp


@app.get("/logout")
def logout():
    resp = go("/login")
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    today = now_local().date()
    comps = visible_companies(db, user)
    ids = [c.id for c in comps]
    rows = db.execute(select(Employee.company_id, DailyAttendance.status, func.count())
                      .join(Employee, Employee.id == DailyAttendance.employee_id)
                      .where(DailyAttendance.work_date == today, Employee.company_id.in_(ids or [0]))
                      .group_by(Employee.company_id, DailyAttendance.status)).all()
    active = dict(db.execute(select(Employee.company_id, func.count()).where(
        Employee.employment_status == "ACTIVE", Employee.company_id.in_(ids or [0])).group_by(Employee.company_id)).all())
    per = {c.id: {"company": c, "active": active.get(c.id, 0), "present": 0, "late": 0, "absent": 0, "leave": 0}
           for c in comps}
    for cid, status, n in rows:
        p = per[cid]
        if status in ("PRESENT", "LATE", "MISSING_OUT", "OFFDAY_WORK"):
            p["present"] += n
        if status == "LATE":
            p["late"] += n
        if status == "ABSENT":
            p["absent"] += n
        if status in ("LEAVE_PAID", "LEAVE_UNPAID"):
            p["leave"] += n
    total = {k: sum(p[k] for p in per.values()) for k in ("active", "present", "late", "absent", "leave")}
    since = datetime.combine(today, time.min)
    punches = db.scalar(select(func.count(AttendanceLog.id)).where(AttendanceLog.punch_time >= since)) or 0
    devs = db.scalars(select(Device).order_by(Device.name)).all()
    return render(request, "dashboard.html", user, per=list(per.values()), total=total, today=today,
                  punches_today=punches, devices=devs, processed=bool(rows),
                  pending=sum(1 for d in devs if not d.approved))


# ---------------------------------------------------------------- employees
@app.get("/employees", response_class=HTMLResponse)
def employees_page(request: Request, q: str = "", company: str = "", page: int = 1, user=Depends(page_user),
                   db: Session = Depends(get_db)):
    size = 50
    cid = company_filter(user, company)
    stmt = select(Employee)
    if cid:
        stmt = stmt.where(Employee.company_id == cid)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Employee.employee_code.ilike(like), Employee.full_name.ilike(like),
                              Employee.card_no.ilike(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Employee.employee_code).offset((max(page, 1) - 1) * size).limit(size)).all()
    return render(request, "employees.html", user, rows=rows, q=q, page=page, pages=max(1, -(-total // size)),
                  total=total, company=cid or "", companies=visible_companies(db, user),
                  show_salary=user["role"] in ("ADMIN", "HR", "ACCOUNTS"))


@app.get("/employees/template.xlsx")
def employee_template(user=Depends(page_user)):
    return Response(template_bytes(), media_type=XLSX,
                    headers={"Content-Disposition": "attachment; filename=employee-import-template.xlsx"})


@app.post("/employees/import")
async def employees_import(file: UploadFile = File(...), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "HR")
    try:
        result = import_employees(db, await file.read(), user.get("company_id"))
    except Exception as exc:
        db.rollback()
        return go("/employees", err=str(exc)[:1500])
    audit(db, user, "EMPLOYEE_IMPORT", f"{file.filename}: {result}")
    return go("/employees", msg=f"Import done: {result['added']} added, {result['updated']} updated")


@app.get("/employees/new", response_class=HTMLResponse)
def employee_new(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    return employee_form(request, None, user, db)


@app.get("/employees/{emp_id}", response_class=HTMLResponse)
def employee_form(request: Request, emp_id: int | None, user=Depends(page_user),
                  db: Session = Depends(get_db)):
    need(user, "HR")
    emp = db.get(Employee, emp_id) if emp_id else None
    if emp_id and not emp:
        raise HTTPException(404, "Employee not found")
    if emp:
        check_company(user, emp.company_id)
    return render(request, "employee_form.html", user, e=emp, companies=visible_companies(db, user),
                  shifts=db.scalars(select(Shift).order_by(Shift.code)).all(),
                  departments=db.scalars(select(Department).order_by(Department.name)).all())


@app.post("/employees/save")
def employee_save(request: Request, emp_id: str = Form(""), company_id: int = Form(...),
                  employee_code: str = Form(...), card_no: str = Form(...),
                  full_name: str = Form(...), department: str = Form(""), designation: str = Form(""),
                  section: str = Form(""), grade: str = Form(""), shift_id: str = Form(""),
                  joined_on: str = Form(""), left_on: str = Form(""), employment_status: str = Form("ACTIVE"),
                  gross: str = Form("0"), auto_split: str = Form(""), basic: str = Form("0"),
                  house_rent: str = Form("0"), medical: str = Form("0"), transport: str = Form("0"),
                  food: str = Form("0"), payment_mode: str = Form("CASH"), bank_name: str = Form(""),
                  account_no: str = Form(""), allow_mobile: str = Form(""), mobile_pin: str = Form(""),
                  user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "HR")
    check_company(user, company_id)
    back = f"/employees/{emp_id}" if emp_id else "/employees/new"
    try:
        e = db.get(Employee, int(emp_id)) if emp_id else Employee()
        if emp_id:
            check_company(user, e.company_id)
        if not db.get(Company, company_id):
            raise ValueError("Unknown company")
        code, card = employee_code.strip(), card_no.strip()
        clash = db.scalar(select(Employee).where(or_(Employee.employee_code == code, Employee.card_no == card),
                                                 Employee.id != (e.id or 0)))
        if clash:
            raise ValueError(f"Employee code or card no. already used by {clash.employee_code} "
                             f"({clash.company.code if clash.company else ''})")
        g = dec(gross, "Gross")
        if auto_split:
            comp = split_gross(g, load_policy(db, company_id))
            parts = [comp.basic, comp.house_rent, comp.medical, comp.transport, comp.food]
        else:
            parts = [dec(basic, "Basic"), dec(house_rent, "House rent"), dec(medical, "Medical"),
                     dec(transport, "Transport"), dec(food, "Food")]
            if sum(parts) != g:
                raise ValueError(f"Salary parts add up to {sum(parts)}, not gross {g}")
        if mobile_pin and (not mobile_pin.isdigit() or len(mobile_pin) < 4):
            raise ValueError("Mobile PIN must be at least 4 digits")
        dept = None
        if department.strip():
            dept = db.scalar(select(Department).where(Department.name == department.strip()))
            if not dept:
                dept = Department(name=department.strip())
                db.add(dept)
        e.company_id = company_id
        e.employee_code, e.card_no, e.full_name = code, card, full_name.strip()
        e.department, e.designation, e.section, e.grade = dept, designation or None, section or None, grade or None
        e.shift_id = int(shift_id) if shift_id else None
        e.joined_on = date.fromisoformat(joined_on) if joined_on else None
        e.left_on = date.fromisoformat(left_on) if left_on else None
        e.employment_status = employment_status
        e.gross = g
        e.basic, e.house_rent, e.medical, e.transport, e.food = parts
        e.payment_mode, e.bank_name, e.account_no = payment_mode, bank_name or None, account_no or None
        e.allow_mobile = bool(allow_mobile)
        if mobile_pin:
            e.mobile_pin_hash = hash_password(mobile_pin)
        if not emp_id:
            db.add(e)
        db.flush()
        audit(db, user, "EMPLOYEE_SAVE", f"{code} {full_name}" + (" (PIN changed)" if mobile_pin else ""), commit=False)
        db.commit()
    except (ValueError, TypeError) as exc:
        db.rollback()
        return go(back, err=str(exc))
    return go("/employees", msg=f"Saved {e.employee_code}")


# ---------------------------------------------------------------- punch machines
@app.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, edit: int | None = None, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "HR")
    devs = db.scalars(select(Device).order_by(Device.approved, Device.name)).all()
    return render(request, "devices.html", user, devices=devs, e=db.get(Device, edit) if edit else None,
                  ips=maintenance.lan_ips())


@app.post("/devices/save")
def device_save(device_id: str = Form(""), name: str = Form(...), brand: str = Form(...),
                serial_number: str = Form(""), ip_address: str = Form(""), port: int = Form(80),
                username: str = Form(""), password: str = Form(""), location: str = Form(""),
                approved: str = Form(""), enabled: str = Form(""), user=Depends(page_user),
                db: Session = Depends(get_db)):
    need(user, "HR")
    brand = brand.upper()
    if brand not in ("ZKTECO", "HIKVISION"):
        return go("/devices", err="Brand must be ZKTeco or Hikvision")
    d = db.get(Device, int(device_id)) if device_id else Device()
    if brand == "HIKVISION" and not ip_address.strip():
        return go("/devices", err="Hikvision machines need an IP address")
    if brand == "ZKTECO" and not serial_number.strip():
        return go("/devices", err="ZKTeco machines need the serial number (Menu > System Info > Device Info)")
    d.name, d.brand, d.location = name.strip(), brand, location.strip() or None
    d.serial_number = serial_number.strip() or None
    d.ip_address, d.port, d.username = ip_address.strip() or None, port, username.strip() or None
    if password:
        d.password_enc = devices.encrypt(password)
    d.approved, d.enabled = bool(approved), bool(enabled)
    if not device_id:
        db.add(d)
    audit(db, user, "DEVICE_SAVE", f"{brand} {d.name} {d.ip_address or ''} {d.serial_number or ''} approved={d.approved}",
          commit=False)
    db.commit()
    return go("/devices", msg=f"Machine {d.name} saved")


@app.post("/devices/{device_id}/test")
def device_test(device_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "HR")
    d = db.get(Device, device_id)
    if not d:
        raise HTTPException(404, "Unknown machine")
    if d.brand != "HIKVISION":
        when = f"{d.last_seen:%d/%m/%Y %H:%M}" if d.last_seen else "never"
        return go("/devices", msg=f"{d.name}: ZKTeco machines connect to the server themselves. Last contact: {when}")
    try:
        info = hikvision.device_info(d.ip_address, d.port or 80, d.username or "admin", devices.decrypt(d.password_enc))
    except RuntimeError as exc:
        return go("/devices", err=f"{d.name}: {exc}")
    return go("/devices", msg=f"{d.name}: connected - {info}")


@app.post("/devices/{device_id}/pull")
def device_pull(device_id: int, days_back: int = Form(31), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "HR")
    d = db.get(Device, device_id)
    if not d or d.brand != "HIKVISION":
        return go("/devices", err="Only Hikvision machines can be pulled. ZKTeco machines push by themselves.")
    if not d.approved:
        return go("/devices", err="Approve the machine first")
    r = devices.hik_poll_device(db, d, max(1, min(days_back, 180)))
    audit(db, user, "DEVICE_PULL", f"{d.name} {days_back} days: {r}")
    if not r["ok"]:
        return go("/devices", err=f"{d.name}: {r['message']}")
    return go("/devices", msg=f"{d.name}: read {r['read']} punches, {r['inserted']} new")


@app.post("/devices/{device_id}/delete")
def device_delete(device_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user)
    d = db.get(Device, device_id)
    if d:
        audit(db, user, "DEVICE_DELETE", f"{d.brand} {d.name}", commit=False)
        db.delete(d)
        db.commit()
    return go("/devices", msg="Machine removed (its punches are kept)")


# ZKTeco ADMS push endpoints - called by the machines, no login. Unknown serials are parked as "pending".
@app.get("/iclock/cdata")
@app.get("/iclock/cdata.aspx")
def zk_handshake(request: Request, SN: str = "", db: Session = Depends(get_db)):
    if not SN:
        return PlainTextResponse("ERROR: no SN", status_code=400)
    devices.zk_device(db, SN, request.client.host if request.client else None)
    return PlainTextResponse(options_response(SN))


@app.post("/iclock/cdata")
@app.post("/iclock/cdata.aspx")
async def zk_upload(request: Request, SN: str = "", table: str = "", db: Session = Depends(get_db)):
    if not SN:
        return PlainTextResponse("ERROR: no SN", status_code=400)
    dev = devices.zk_device(db, SN, request.client.host if request.client else None)
    body = (await request.body()).decode("utf-8", "replace")
    if table.upper() == "ATTLOG":
        n, _ = devices.zk_receive(db, dev, body)
        return PlainTextResponse(f"OK: {n}")
    return PlainTextResponse("OK")        # OPERLOG, USERINFO, photos... acknowledged, not stored


@app.get("/iclock/getrequest")
@app.get("/iclock/getrequest.aspx")
def zk_poll(request: Request, SN: str = "", db: Session = Depends(get_db)):
    if SN:
        devices.zk_device(db, SN, request.client.host if request.client else None)
    return PlainTextResponse("OK")


@app.post("/iclock/devicecmd")
@app.post("/iclock/devicecmd.aspx")
def zk_cmd_result():
    return PlainTextResponse("OK")


# Newer push firmware (protocol 2.4+) registers first, then asks for its options via /push.
@app.api_route("/iclock/registry", methods=["GET", "POST"])
@app.api_route("/iclock/registry.aspx", methods=["GET", "POST"])
def zk_registry(request: Request, SN: str = "", db: Session = Depends(get_db)):
    if not SN:
        return PlainTextResponse("ERROR: no SN", status_code=400)
    devices.zk_device(db, SN, request.client.host if request.client else None)
    return PlainTextResponse(f"RegistryCode={SN}")


@app.api_route("/iclock/push", methods=["GET", "POST"])
@app.api_route("/iclock/push.aspx", methods=["GET", "POST"])
def zk_push_options(request: Request, SN: str = "", db: Session = Depends(get_db)):
    if SN:
        devices.zk_device(db, SN, request.client.host if request.client else None)
    return PlainTextResponse(options_response(SN or "?"))


@app.get("/iclock/ping")
@app.get("/iclock/ping.aspx")
def zk_ping():
    return PlainTextResponse("OK")


# ---------------------------------------------------------------- attendance
@app.get("/attendance", response_class=HTMLResponse)
def attendance_page(request: Request, day: str = "", status: str = "", company: str = "",
                    user=Depends(page_user), db: Session = Depends(get_db)):
    d = date.fromisoformat(day) if day else now_local().date()
    cid = company_filter(user, company)
    stmt = select(DailyAttendance).join(Employee).where(DailyAttendance.work_date == d)
    if cid:
        stmt = stmt.where(Employee.company_id == cid)
    if status:
        stmt = stmt.where(DailyAttendance.status == status)
    rows = db.scalars(stmt.order_by(Employee.employee_code)).all()
    return render(request, "attendance.html", user, rows=rows, day=d, status=status, company=cid or "",
                  companies=visible_companies(db, user))


@app.post("/attendance/process")
def attendance_process(month: str = Form(...), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "HR", "ACCOUNTS")
    y, m = parse_month(month)
    ids = [c.id for c in visible_companies(db, user)]
    n = len(process_month(db, y, m, company_ids=ids))
    audit(db, user, "ATTENDANCE_PROCESS", f"{y}-{m:02d}: {n} employees")
    return go("/attendance", msg=f"Processed {month_name[m]} {y} for {n} employees "
                                 "(companies with a FINAL payroll for that month are locked and skipped)")


@app.post("/attendance/import")
async def attendance_import(file: UploadFile = File(...), user=Depends(page_user), db: Session = Depends(get_db)):
    """CSV (card_no, punch_time, device) or a ZKTeco USB export (attlog.dat / *_attlog.dat)."""
    need(user, "HR")
    content = await file.read()
    try:
        if file.filename.lower().endswith((".dat", ".txt")):
            punches, bad = parse_attlog(content.decode("utf-8", "replace"))
            ins, dup = insert_punches(db, [(p, t, "USB") for p, t in punches], "USB", user["username"])
            r = {"inserted": ins, "skipped_duplicates": dup}
            if bad:
                r["unreadable_lines"] = bad
        else:
            r = import_attendance_csv(db, content, user["username"])
    except Exception as exc:
        db.rollback()
        return go("/attendance", err=str(exc)[:1500])
    audit(db, user, "PUNCH_FILE_IMPORT", f"{file.filename}: {r}")
    return go("/attendance", msg=f"{file.filename}: {r['inserted']} punches added, {r['skipped_duplicates']} duplicates skipped")


@app.post("/attendance/manual")
def attendance_manual(card_no: str = Form(...), punch_time: str = Form(...), reason: str = Form(...),
                      user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "HR")
    emp = db.scalar(select(Employee).where(or_(Employee.card_no == card_no.strip(),
                                               Employee.employee_code == card_no.strip())))
    if not emp:
        return go("/attendance", err="No employee with that card no. / code")
    check_company(user, emp.company_id)
    if len(reason.strip()) < 3:
        return go("/attendance", err="A reason is required for manual punches")
    t = parse_dt(punch_time.replace("T", " "))
    ins, _ = insert_punches(db, [(emp.card_no, t, "MANUAL")], "MANUAL", user["username"])
    if ins:
        log = db.scalar(select(AttendanceLog).where(AttendanceLog.card_no == emp.card_no,
                                                    AttendanceLog.punch_time == t))
        log.note = reason.strip()[:250]
        audit(db, user, "MANUAL_PUNCH", f"{emp.employee_code} {t} - {reason}", commit=False)
        db.commit()
    return go(f"/attendance?day={t.date()}", msg="Manual punch added - process the month again to update totals"
              if ins else "", err="" if ins else "That punch already exists")


@app.get("/attendance/day.xlsx")
def attendance_day_xlsx(day: str, company: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    d = date.fromisoformat(day)
    cid = company_filter(user, company)
    stmt = select(DailyAttendance).join(Employee).where(DailyAttendance.work_date == d)
    if cid:
        stmt = stmt.where(Employee.company_id == cid)
    rows = db.scalars(stmt.order_by(Employee.company_id, Employee.employee_code)).all()
    return Response(attendance_day(d, rows), media_type=XLSX,
                    headers={"Content-Disposition": f"attachment; filename=attendance-{d}.xlsx"})


@app.get("/attendance/month.xlsx")
def attendance_month_xlsx(month: str, company: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    y, m = parse_month(month)
    cid = company_filter(user, company)
    first, last = month_bounds(y, m)
    stmt = select(DailyAttendance).join(Employee).where(DailyAttendance.work_date >= first,
                                                        DailyAttendance.work_date <= last)
    if cid:
        stmt = stmt.where(Employee.company_id == cid)
    by_emp: dict[int, list] = {}
    for r in db.scalars(stmt):
        by_emp.setdefault(r.employee_id, []).append(r)
    ids = list(by_emp)
    emps = []
    for i in range(0, len(ids), 500):
        emps += db.scalars(select(Employee).where(Employee.id.in_(ids[i:i + 500]))).all()
    emps.sort(key=lambda e: (e.company.code if e.company else "", e.employee_code))
    return Response(attendance_month(y, m, emps, by_emp), media_type=XLSX,
                    headers={"Content-Disposition": f"attachment; filename=attendance-{y}-{m:02d}.xlsx"})


@app.get("/attendance/punches", response_class=HTMLResponse)
def punches_page(request: Request, card: str = "", day: str = "", user=Depends(page_user),
                 db: Session = Depends(get_db)):
    d = date.fromisoformat(day) if day else now_local().date()
    stmt = select(AttendanceLog).where(AttendanceLog.punch_time >= datetime.combine(d - timedelta(days=1), time.min),
                                       AttendanceLog.punch_time < datetime.combine(d + timedelta(days=2), time.min))
    if card:
        stmt = stmt.where(AttendanceLog.card_no == card.strip())
    if user.get("company_id"):
        cards = select(Employee.card_no).where(Employee.company_id == user["company_id"])
        stmt = stmt.where(AttendanceLog.card_no.in_(cards))
    rows = db.scalars(stmt.order_by(AttendanceLog.punch_time.desc()).limit(500)).all()
    names = {e.card_no: e for e in db.scalars(select(Employee).where(
        Employee.card_no.in_({r.card_no for r in rows} or {""})))}
    return render(request, "punches.html", user, rows=rows, card=card, day=d, names=names)


# ---------------------------------------------------------------- leave
@app.get("/leaves", response_class=HTMLResponse)
def leaves_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    since = now_local().date() - timedelta(days=60)
    stmt = (select(Leave, Employee).join(Employee, Employee.id == Leave.employee_id)
            .where(Leave.end_date >= since).order_by(Leave.start_date.desc()))
    if user.get("company_id"):
        stmt = stmt.where(Employee.company_id == user["company_id"])
    return render(request, "leaves.html", user, rows=db.execute(stmt).all())


@app.post("/leaves")
def leave_add(employee_code: str = Form(...), start_date: str = Form(...), end_date: str = Form(...),
              leave_type: str = Form(...), note: str = Form(""), user=Depends(page_user),
              db: Session = Depends(get_db)):
    need(user, "HR")
    emp = db.scalar(select(Employee).where(Employee.employee_code == employee_code.strip()))
    if not emp:
        return go("/leaves", err="Unknown employee code")
    check_company(user, emp.company_id)
    s, e = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if e < s:
        return go("/leaves", err="End date is before start date")
    db.add(Leave(employee_id=emp.id, start_date=s, end_date=e, leave_type=leave_type,
                 paid=leave_type != "UNPAID", note=note or None))
    audit(db, user, "LEAVE_ADD", f"{emp.employee_code} {leave_type} {s}..{e}", commit=False)
    db.commit()
    return go("/leaves", msg="Leave saved - process the month again to apply it")


@app.post("/leaves/{leave_id}/delete")
def leave_delete(leave_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "HR")
    lv = db.get(Leave, leave_id)
    if lv:
        emp = db.get(Employee, lv.employee_id)
        check_company(user, emp.company_id if emp else None)
        audit(db, user, "LEAVE_DELETE", f"emp {lv.employee_id} {lv.leave_type} {lv.start_date}..{lv.end_date}",
              commit=False)
        db.delete(lv)
        db.commit()
    return go("/leaves", msg="Leave removed")


# ---------------------------------------------------------------- shifts
@app.get("/shifts", response_class=HTMLResponse)
def shifts_page(request: Request, edit: int | None = None, user=Depends(page_user), db: Session = Depends(get_db)):
    shifts = db.scalars(select(Shift).order_by(Shift.code)).all()
    used = dict(db.execute(select(Employee.shift_id, func.count()).group_by(Employee.shift_id)).all())
    return render(request, "shifts.html", user, shifts=shifts, used=used, companies=visible_companies(db, user),
                  e=db.get(Shift, edit) if edit else None)


@app.post("/shifts/save")
def shift_save(shift_id: str = Form(""), code: str = Form(...), name: str = Form(...), start: str = Form(...),
               end: str = Form(...), grace_minutes: int = Form(10), break_minutes: int = Form(60),
               break_after_minutes: int = Form(300), early_window_minutes: int = Form(180),
               ot_round_minutes: int = Form(30), max_ot_minutes: int = Form(120), offday_work_is_ot: str = Form(""),
               user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "HR")
    code = code.strip().upper()
    s = db.get(Shift, int(shift_id)) if shift_id else Shift()
    if db.scalar(select(Shift).where(Shift.code == code, Shift.id != (s.id or 0))):
        return go("/shifts", err=f"Shift code {code} already exists")
    if not 0 <= early_window_minutes <= 360 or ot_round_minutes < 1 or max_ot_minutes < 0:
        return go("/shifts", err="Check the minute values")
    s.code, s.name = code, name.strip()
    s.start, s.end = time.fromisoformat(start), time.fromisoformat(end)
    s.grace_minutes, s.break_minutes, s.break_after_minutes = grace_minutes, break_minutes, break_after_minutes
    s.early_window_minutes, s.ot_round_minutes, s.max_ot_minutes = early_window_minutes, ot_round_minutes, max_ot_minutes
    s.offday_work_is_ot = bool(offday_work_is_ot)
    if not shift_id:
        db.add(s)
    audit(db, user, "SHIFT_SAVE", f"{code} {start}-{end}", commit=False)
    db.commit()
    return go("/shifts", msg=f"Shift {code} saved - process the month again to apply changes")


@app.post("/shifts/assign")
def shift_assign(shift_id: int = Form(...), employee_codes: str = Form(""), department: str = Form(""),
                 company_id: str = Form(""), user=Depends(page_user), db: Session = Depends(get_db)):
    """Assign a shift to employee codes, a department and/or a whole company."""
    need(user, "HR")
    shift = db.get(Shift, shift_id)
    if not shift:
        return go("/shifts", err="Unknown shift")
    codes = [c.strip() for c in employee_codes.replace(",", "\n").splitlines() if c.strip()]
    conds = []
    if codes:
        conds.append(Employee.employee_code.in_(codes))
    if department.strip():
        conds.append(Employee.department.has(Department.name == department.strip()))
    cid = company_filter(user, company_id)
    if company_id and cid:
        conds.append(Employee.company_id == cid)
    if not conds:
        return go("/shifts", err="Give employee codes, a department or a company")
    stmt = select(Employee).where(or_(*conds))
    if user.get("company_id"):
        stmt = stmt.where(Employee.company_id == user["company_id"])
    emps = db.scalars(stmt).all()
    for e in emps:
        e.shift_id = shift.id
    audit(db, user, "SHIFT_ASSIGN", f"{shift.code} -> {len(emps)} employees", commit=False)
    db.commit()
    return go("/shifts", msg=f"{shift.code} assigned to {len(emps)} employees")


# ---------------------------------------------------------------- payroll (per company)
def _payroll_url(cid, y, m) -> str:
    return f"/payroll?company={cid}&month={y}-{m:02d}"


@app.get("/payroll", response_class=HTMLResponse)
def payroll_page(request: Request, month: str = "", company: str = "", user=Depends(page_user),
                 db: Session = Depends(get_db)):
    need(user, "HR", "ACCOUNTS")
    y, m = parse_month(month)
    comps = visible_companies(db, user)
    cid = company_filter(user, company) or (comps[0].id if comps else None)
    check_company(user, cid)
    runs = {r.company_id: r for r in db.scalars(select(PayrollRun).where(PayrollRun.year == y, PayrollRun.month == m))}
    run = runs.get(cid)
    lines = sorted(run.lines, key=lambda l: (l.department, l.employee_code)) if run else []
    totals = {k: sum(getattr(l, k) for l in lines) for k in
              ("earned_gross", "ot_amount", "absent_deduction", "total_deductions", "net_pay")} if lines else {}
    by_mode: dict[str, Decimal] = {}
    for l in lines:
        by_mode[l.payment_mode] = by_mode.get(l.payment_mode, 0) + l.net_pay
    return render(request, "payroll.html", user, y=y, m=m, run=run, lines=lines, totals=totals, by_mode=by_mode,
                  companies=comps, cid=cid, company=db.get(Company, cid) if cid else None, runs=runs,
                  warn_count=sum(1 for l in lines if l.warnings))


@app.post("/payroll/process")
def payroll_process(month: str = Form(...), company_id: int = Form(...), user=Depends(page_user),
                    db: Session = Depends(get_db)):
    need(user, "HR", "ACCOUNTS")
    check_company(user, company_id)
    y, m = parse_month(month)
    try:
        run = run_payroll(db, company_id, y, m, user)
    except (PayrollLocked, ValueError) as exc:
        return go(_payroll_url(company_id, y, m), err=str(exc))
    err = ("Skipped: " + "; ".join(run.errors)[:1200]) if run.errors else ""
    return go(_payroll_url(company_id, y, m), msg=f"Draft payroll ready for {len(run.lines)} employees", err=err)


@app.post("/payroll/finalize")
def payroll_finalize(month: str = Form(...), company_id: int = Form(...), confirm: str = Form(""),
                     user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "ACCOUNTS")
    check_company(user, company_id)
    y, m = parse_month(month)
    if confirm.strip().upper() != "FINAL":
        return go(_payroll_url(company_id, y, m), err="Type FINAL to confirm")
    try:
        finalize_payroll(db, company_id, y, m, user)
    except (PayrollLocked, ValueError) as exc:
        return go(_payroll_url(company_id, y, m), err=str(exc))
    return go(_payroll_url(company_id, y, m), msg="Payroll finalized and locked")


def _run_for(db, user, run_id: int) -> PayrollRun:
    run = db.get(PayrollRun, run_id)
    if not run:
        raise HTTPException(404, "No such payroll")
    check_company(user, run.company_id)
    return run


@app.get("/payroll/run/{run_id}/salary.xlsx")
def payroll_xlsx(run_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "HR", "ACCOUNTS")
    run = _run_for(db, user, run_id)
    audit(db, user, "PAYROLL_EXPORT", f"{run.company.code} {run.year}-{run.month:02d} salary sheet")
    return Response(salary_sheet(run), media_type=XLSX, headers={
        "Content-Disposition": f"attachment; filename=salary-{run.company.code}-{run.year}-{run.month:02d}.xlsx"})


@app.get("/payroll/run/{run_id}/payslips.pdf")
def payroll_pdf(run_id: int, employee_code: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "HR", "ACCOUNTS")
    run = _run_for(db, user, run_id)
    lines = [l for l in run.lines if not employee_code or l.employee_code == employee_code.strip()]
    audit(db, user, "PAYSLIP_EXPORT", f"{run.company.code} {run.year}-{run.month:02d} {employee_code or 'all'}")
    return Response(payslips_pdf(run, lines), media_type="application/pdf", headers={
        "Content-Disposition": f"inline; filename=payslips-{run.company.code}-{run.year}-{run.month:02d}.pdf"})


@app.post("/payroll/adjustments")
async def payroll_adjustments(month: str = Form(...), company_id: int = Form(...), file: UploadFile = File(...),
                              user=Depends(page_user), db: Session = Depends(get_db)):
    """Excel: employee_code, other_addition, advance, other_deduction, tax, note (one company)."""
    need(user, "HR", "ACCOUNTS")
    check_company(user, company_id)
    from openpyxl import load_workbook
    y, m = parse_month(month)
    back = _payroll_url(company_id, y, m)
    run = get_run(db, company_id, y, m)
    if run and run.status == "FINAL":
        return go(back, err="Payroll is final - adjustments are locked")
    try:
        ws = load_workbook(io.BytesIO(await file.read()), data_only=True).worksheets[0]
        it = ws.iter_rows(values_only=True)
        head = [str(h or "").strip().lower() for h in next(it)]
        if "employee_code" not in head:
            raise ValueError("first row must contain employee_code")
        emps = {e.employee_code: e for e in db.scalars(select(Employee).where(Employee.company_id == company_id))}
        n = 0
        for row in it:
            r = dict(zip(head, row))
            code = str(r.get("employee_code") or "").strip()
            if code.endswith(".0"):
                code = code[:-2]
            if not code:
                continue
            e = emps.get(code)
            if not e:
                raise ValueError(f"employee_code {code} is not in this company")
            a = db.scalar(select(PayrollAdjustment).where(PayrollAdjustment.employee_id == e.id,
                                                          PayrollAdjustment.year == y, PayrollAdjustment.month == m))
            if not a:
                a = PayrollAdjustment(employee_id=e.id, year=y, month=m)
                db.add(a)
            for k in ("other_addition", "advance", "other_deduction", "tax"):
                setattr(a, k, dec(str(r.get(k) or 0), k))
            a.note = (str(r.get("note") or "").strip() or None)
            n += 1
        audit(db, user, "ADJUSTMENT_IMPORT", f"company {company_id} {y}-{m:02d}: {n} rows", commit=False)
        db.commit()
    except Exception as exc:
        db.rollback()
        return go(back, err=f"Nothing saved: {exc}")
    return go(back, msg=f"{n} adjustments saved - process the payroll again to apply them")


@app.get("/payroll/adjustments-template.xlsx")
def adjustments_template(user=Depends(page_user)):
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.append(["employee_code", "other_addition", "advance", "other_deduction", "tax", "note"])
    wb.active.append(["LG0001", 0, 1000, 0, 0, "Advance"])
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type=XLSX,
                    headers={"Content-Disposition": "attachment; filename=adjustments-template.xlsx"})


# ---------------------------------------------------------------- settings
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, policy_company: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    pc = company_filter(user, policy_company) or 0
    year = now_local().year
    holidays = db.scalars(select(Holiday).where(Holiday.day >= date(year - 1, 1, 1)).order_by(Holiday.day)).all()
    lat, lon, radius = geofence(db)
    return render(request, "settings.html", user, policy=policy_rows(db, pc), policy_company=pc,
                  holidays=holidays, weekend=sorted(weekend_set(db)), lat=lat, lon=lon, radius=radius,
                  companies=visible_companies(db, user),
                  all_companies=db.scalars(select(Company).order_by(Company.code)).all(),
                  data_dir=str(DATA_DIR), db_kind="SQLite" if settings.db_url.startswith("sqlite") else "SQL Server")


@app.post("/settings/policy")
async def settings_policy(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "ACCOUNTS")
    form = {k: str(v).strip() for k, v in (await request.form()).items()}
    pc = int(form.pop("policy_company", "0") or 0)
    if user.get("company_id") and pc != user["company_id"]:
        raise HTTPException(403, "You can only change your own company's policy")
    try:
        save_policy(db, form, pc)
    except Exception as exc:
        db.rollback()
        return go(f"/settings?policy_company={pc}", err=f"Not saved: {exc}")
    audit(db, user, "POLICY_SAVE", f"company {pc or 'group default'}: {json.dumps(form)}")
    return go(f"/settings?policy_company={pc}", msg="Pay policy saved")


@app.post("/settings/general")
async def settings_general(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user)
    f = await request.form()
    try:
        weekend = ",".join(str(d) for d in range(7) if f.get(f"wd{d}"))
        lat, lon, radius = float(f.get("geofence_lat") or 0), float(f.get("geofence_lon") or 0), int(f.get("geofence_radius_m") or 200)
        if not -90 <= lat <= 90 or not -180 <= lon <= 180 or not 20 <= radius <= 5000:
            raise ValueError("location out of range")
    except ValueError as exc:
        return go("/settings", err=f"Not saved: {exc}")
    set_setting(db, "weekend_days", weekend, commit=False)
    set_setting(db, "geofence_lat", str(lat), commit=False)
    set_setting(db, "geofence_lon", str(lon), commit=False)
    set_setting(db, "geofence_radius_m", str(radius), commit=False)
    audit(db, user, "SETTINGS_SAVE", f"weekend={weekend} office={lat},{lon} r={radius}", commit=False)
    db.commit()
    return go("/settings", msg="Saved - process the month again if the weekly off day changed")


@app.post("/settings/companies")
def company_save(company_id: str = Form(""), code: str = Form(...), name: str = Form(...), address: str = Form(""),
                 active: str = Form(""), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user)
    code = code.strip().upper()[:20]
    c = db.get(Company, int(company_id)) if company_id else Company()
    if db.scalar(select(Company).where(Company.code == code, Company.id != (c.id or 0))):
        return go("/settings", err=f"Company code {code} already exists")
    c.code, c.name, c.address, c.active = code, name.strip(), address.strip() or None, bool(active)
    if not company_id:
        db.add(c)
    audit(db, user, "COMPANY_SAVE", f"{code} {name}", commit=False)
    db.commit()
    return go("/settings", msg=f"Company {code} saved")


@app.post("/settings/holidays")
def holiday_add(day: str = Form(...), name: str = Form(...), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "HR")
    d = date.fromisoformat(day)
    if db.scalar(select(Holiday).where(Holiday.day == d)):
        return go("/settings", err="That date is already a holiday")
    db.add(Holiday(day=d, name=name.strip()))
    audit(db, user, "HOLIDAY_ADD", f"{d} {name}", commit=False)
    db.commit()
    return go("/settings", msg="Holiday added")


@app.post("/settings/holidays/{hid}/delete")
def holiday_delete(hid: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "HR")
    h = db.get(Holiday, hid)
    if h:
        audit(db, user, "HOLIDAY_DELETE", f"{h.day} {h.name}", commit=False)
        db.delete(h)
        db.commit()
    return go("/settings", msg="Holiday removed")


@app.post("/settings/backup")
def backup(user=Depends(page_user)):
    need(user)
    path = maintenance.backup_now()
    return go("/settings", msg=f"Backup saved: {path}" if path else "Backups are only made for the built-in SQLite database")


# ---------------------------------------------------------------- users & audit
@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user)
    return render(request, "users.html", user, users=db.scalars(select(User).order_by(User.username)).all(),
                  roles=ROLES, companies=db.scalars(select(Company).order_by(Company.code)).all())


@app.post("/users/save")
def user_save(username: str = Form(...), role: str = Form(...), password: str = Form(""), active: str = Form(""),
              company_id: str = Form(""), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user)
    if role not in ROLES:
        return go("/users", err="Unknown role")
    u = db.scalar(select(User).where(User.username == username.strip()))
    if not u:
        if len(password) < 8:
            return go("/users", err="New users need a password of 8+ characters")
        u = User(username=username.strip(), password_hash=hash_password(password))
        db.add(u)
    elif password:
        if len(password) < 8:
            return go("/users", err="Password must be 8+ characters")
        u.password_hash = hash_password(password)
    if u.username == user["username"] and (role != "ADMIN" or not active or company_id):
        db.rollback()
        return go("/users", err="You cannot remove your own group-wide admin access")
    u.role, u.active = role, bool(active)
    u.company_id = int(company_id) if company_id else None
    audit(db, user, "USER_SAVE", f"{u.username} role={role} company={company_id or 'ALL'} active={bool(active)}"
          + (" password changed" if password else ""), commit=False)
    db.commit()
    return go("/users", msg=f"User {u.username} saved")


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user)
    rows = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(500)).all()
    return render(request, "audit.html", user, rows=rows)


# ---------------------------------------------------------------- mobile attendance
def mobile_employee(request: Request, db: Session) -> Employee | None:
    token = request.cookies.get(MOBILE_COOKIE)
    if not token:
        return None
    try:
        data = decode_token(token)
    except Exception:
        return None
    if data.get("role") != "EMPLOYEE":
        return None
    return db.scalar(select(Employee).where(Employee.employee_code == data["sub"]))


@app.get("/m", response_class=HTMLResponse)
def mobile_page(request: Request, db: Session = Depends(get_db)):
    if request.url.scheme != "https" and request.headers.get("x-forwarded-proto") != "https":
        # GPS only works on HTTPS: send phones to the HTTPS listener
        host = request.url.hostname or maintenance.lan_ips()[0]
        return RedirectResponse(f"https://{host}:{settings.https_port}/m", status_code=303)
    emp = mobile_employee(request, db)
    return render(request, "mobile.html", None, emp=emp, punches=mobile.today_punches(db, emp) if emp else [])


@app.post("/m/login")
def mobile_login(request: Request, employee_code: str = Form(...), pin: str = Form(...),
                 db: Session = Depends(get_db)):
    try:
        emp = mobile.login(db, employee_code, pin)
    except mobile.MobileError as exc:
        return go("/m", err=str(exc))
    resp = go("/m")
    resp.set_cookie(MOBILE_COOKIE, create_token(emp.employee_code, "EMPLOYEE", 24 * 30), **cookie_kwargs(request, 24 * 30))
    return resp


@app.get("/m/logout")
def mobile_logout():
    resp = go("/m")
    resp.delete_cookie(MOBILE_COOKIE)
    return resp


@app.post("/m/punch")
def mobile_punch(request: Request, latitude: float = Form(...), longitude: float = Form(...),
                 accuracy: float = Form(...), db: Session = Depends(get_db)):
    emp = mobile_employee(request, db)
    if not emp:
        return JSONResponse({"ok": False, "message": "Please log in again"}, status_code=401)
    try:
        log = mobile.punch(db, emp, latitude, longitude, accuracy)
    except mobile.MobileError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    return {"ok": True, "message": f"Punch recorded at {log.punch_time:%I:%M %p}", "time": f"{log.punch_time:%H:%M}"}

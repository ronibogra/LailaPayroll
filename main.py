import io
import json
from calendar import month_name
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse, PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session

from config import settings, DATA_DIR
from database import Base, engine, get_db, SessionLocal
from integration.csv_import import insert_punches, parse_dt
from integration.punch_import import read_punch_file, ImportProblem
from integration.employee_import import import_employees, template_bytes
from integration.zk_adms import options_response
from models import (Company, Department, Employee, Shift, Device, AttendanceLog, DailyAttendance, Holiday, Leave,
                    PayrollAdjustment, PayrollRun, PayrollLine, User, ErrorLog, Role, EmployeeDocument,
                    LeaveType, SpecialHoliday, PayComponent, EmployeeComponent)
from payroll.engine import split_gross
from reports.excel import (salary_sheet, attendance_month, attendance_day, punches_sheet, first_last_sheet,
                           combined_salary)
from reports.payslip import payslips_pdf
from security import (hash_password, verify_password, create_token, decode_token, page_user,
                      LoginRequired, COOKIE)
from services.permissions import PERMS, GROUPS, BUILTIN_ROLES, ROLE_MIGRATIONS, parse as parse_perms
from services import mobile, devices, maintenance, migrate, branding, backup as backups
from services import leave as leave_svc, documents as docs, components as comp_svc
from services.errors import log_error
from services.photos import save_photo, photo_path, bulk_photos
from integration import generic_import
from services.punch_reports import punch_query, first_last, employees_by_card
from reports.defs import REPORTS, CENTRE, Problem as ReportProblem
from reports.filters import FILTERS, options as filter_options, describe as describe_filters
from reports.engine import groups as report_groups
from reports.engine import chosen_cols, save_cols, to_excel, to_pdf
from services.audit import audit
from services.payroll_service import run_payroll, finalize_payroll, get_run, PayrollLocked
from services.policy import (load_policy, save_policy, policy_rows, get_setting, set_setting, weekend_set,
                             geofence)
from services.processing import process_month, month_bounds
from timeutil import now_local

BASE_DIR = Path(__file__).resolve().parent
MOBILE_COOKIE = "laila_mobile"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OPEN_PATHS = ("/setup", "/static", "/iclock", "/health", "/m", "/brand")

Base.metadata.create_all(bind=engine)
migrate.upgrade(engine, Base.metadata)

app = FastAPI(title=settings.app_name, version="3.0.0", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals.update(settings=settings, month_name=month_name, brand=branding.BRAND)
_state = {"setup_done": None, "started": False}


# ---------------------------------------------------------------- helpers
def go(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    q = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    url, _, frag = url.partition("#")        # the message goes before the #anchor, or the browser drops it
    url += ("&" if "?" in url else "?") + urlencode(q) if q else ""
    return RedirectResponse(url + (f"#{frag}" if frag else ""), status_code=303)


def need(user: dict, *perms: str) -> None:
    """Raise 403 unless the user's role has ALL the given permissions (ADMIN has every permission)."""
    missing = [p for p in perms if p not in user["perms"]]
    if missing:
        raise HTTPException(403, "Your role does not allow this: " + ", ".join(PERMS.get(p, p) for p in missing))


def render(request: Request, name: str, user: dict | None, **ctx) -> HTMLResponse:
    ctx.update(request=request, user=user, msg=request.query_params.get("msg", ""),
               err=request.query_params.get("err", ""))
    if user and {"leave.approve_dept", "leave.approve_final"} & user["perms"] and "pending_leave_count" not in ctx:
        try:
            with SessionLocal() as s:
                ctx["pending_leave_count"] = len(leave_svc.pending_for(s, user, user.get("company_id")))
        except Exception:  # noqa: BLE001 - the badge must never break a page
            ctx["pending_leave_count"] = 0
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


@app.middleware("http")
async def catch_errors(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:  # noqa: BLE001 - every unexpected error is logged and shown politely
        log_error("PAGE", f"{request.method} {request.url.path}: {exc}", exc=exc)
        if "text/html" in request.headers.get("accept", ""):
            return templates.TemplateResponse(request, "error.html", {
                "request": request, "code": 500,
                "detail": f"Something went wrong ({exc.__class__.__name__}). It was saved in the Error log."},
                status_code=500)
        return JSONResponse({"detail": "internal error - see Error log"}, status_code=500)


@app.exception_handler(LoginRequired)
async def _login_redirect(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    if "text/html" in request.headers.get("accept", "") and exc.status_code in (400, 403, 404):
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
        for rname, (label, perms) in BUILTIN_ROLES.items():
            r = db.scalar(select(Role).where(Role.name == rname))
            if r is None:
                db.add(Role(name=rname, label=label, permissions=",".join(perms), builtin=True))
        db.flush()
        version = int(get_setting(db, "roles_version", default="1") or 1)
        for ver, rname, add, remove in ROLE_MIGRATIONS:
            if ver > version:
                r = db.scalar(select(Role).where(Role.name == rname))
                if r is not None:
                    have = parse_perms(r.permissions)
                    r.permissions = ",".join(p for p in PERMS if (p in have or p in add) and p not in remove)
        set_setting(db, "roles_version", str(max([v for v, *_ in ROLE_MIGRATIONS] + [version])), commit=False)
        branding.refresh(db)
        leave_svc.seed_types(db)
        if get_setting(db, "ho_ot_default_done", default="") != "1":
            ho = db.scalar(select(Company).where(Company.code == "HO"))
            if ho and get_setting(db, "pay.ot_enabled", ho.id, default="") == "":
                set_setting(db, "pay.ot_enabled", "false", ho.id, commit=False)
            if ho:
                set_setting(db, "ho_ot_default_done", "1", commit=False)
        if db.scalar(select(func.count(Shift.id))) == 0:
            db.add(Shift(code="GENERAL", name="General 09:00-18:00", start=time(9, 0), end=time(18, 0)))
            db.commit()
    finally:
        db.close()
    devices.start_poller()
    backups.start_scheduler()


@app.get("/brand/logo.png")
def brand_logo():
    return FileResponse(branding.logo_path(), media_type="image/png", headers={"Cache-Control": "public, max-age=300"})


@app.post("/settings/headoffice")
async def settings_headoffice(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "company.manage")
    f = await request.form()
    name = str(f.get("ho_name", "")).strip()
    if len(name) < 2:
        return go("/settings", err="Enter the Head Office name")
    for k in branding.KEYS:
        set_setting(db, k, str(f.get(k, "")).strip()[:240], commit=False)
    db.commit()
    logo = f.get("logo")
    try:
        if logo is not None and getattr(logo, "filename", ""):
            branding.save_logo(await logo.read())
        if f.get("reset_logo"):
            branding.reset_logo()
    except ValueError as exc:
        return go("/settings", err=f"Logo: {exc}")
    branding.refresh(db)
    audit(db, user, "HEADOFFICE_SAVE", f"{name}" + (" + new logo" if logo is not None and getattr(logo, 'filename', '') else ""))
    return go("/settings", msg="Head Office saved")


def qparams(request: Request) -> dict:
    """Query parameters as a dict; repeated keys (multi-select filters) become lists."""
    p: dict = {}
    for k in request.query_params.keys():
        vals = [v for v in request.query_params.getlist(k) if v != ""]
        if not vals:
            continue
        p[k] = vals if (k in FILTERS and FILTERS[k].kind == "multi") else vals[-1]
    return p


def report_ctx(db: Session, user: dict, rid: str, params: dict) -> dict:
    """Rows + chosen columns for a report shown on a page (see reports/defs.py)."""
    rep = REPORTS[rid]
    try:
        rows, subtitle, total = rep.fetch(db, user, params, False)
    except (ReportProblem, ValueError) as exc:
        raise HTTPException(400, str(exc))
    return {"rep": rep, "rows": rows, "subtitle": subtitle, "total": total,
            "cols": chosen_cols(db, user, rep), "all_cols": rep.allowed_cols(user)}


@app.get("/reports", response_class=HTMLResponse)
def reports_centre(request: Request, user=Depends(page_user)):
    items = [REPORTS[k] for k in CENTRE if REPORTS[k].perm in user["perms"]]
    if not items:
        raise HTTPException(403, "Your role does not allow any reports")
    return render(request, "reports.html", user, items=items)


@app.get("/reports/{rid}", response_class=HTMLResponse)
def report_page(request: Request, rid: str, page: int = 1, user=Depends(page_user), db: Session = Depends(get_db)):
    rep = REPORTS.get(rid)
    if not rep or rid not in CENTRE:
        raise HTTPException(404, "Unknown report")
    need(user, rep.perm)
    p = qparams(request)
    today = now_local().date()
    if "start" in rep.filters and not p.get("start") and not p.get("month"):      # sensible default period
        p.setdefault("start", str(today.replace(day=1)) if rid in ("attendance", "leaves", "first_last") else str(today))
        p.setdefault("end", str(today))
    if rid == "jobcard" and not p.get("month") and not p.get("start"):
        p["month"] = f"{today:%Y-%m}"
    r = report_ctx(db, user, rid, p)
    fl = []
    for k in rep.filters:
        f = FILTERS[k]
        if k == "company" and user.get("company_id"):
            continue
        opts = filter_options(db, user, f.options) if f.options else f.choices
        val = p.get(k, [] if f.kind == "multi" else "")
        fl.append({"f": f, "opts": opts, "val": val})
    preview = r["rows"][:rep.preview_limit]
    grouped = report_groups(rep, preview) if rep.group_key else None
    return render(request, "report_page.html", user, r=r, rep=rep, fl=fl, p=p, preview=preview, grouped=grouped,
                  page=max(page, 1), pages=max(1, -(-r["total"] // 200)) if rid == "punches" else 1,
                  filters_text=describe_filters(p, db, user), export_qs=urlencode(p, doseq=True))


@app.get("/export/{rid}.{fmt}")
def export_report(request: Request, rid: str, fmt: str, user=Depends(page_user), db: Session = Depends(get_db)):
    """Excel or PDF of any report with the user's chosen columns and the same filters as the page."""
    rep = REPORTS.get(rid)
    if not rep or fmt not in ("xlsx", "pdf"):
        raise HTTPException(404, "Unknown report")
    need(user, rep.perm, "reports.export")
    params = qparams(request)
    try:
        rows, subtitle, _ = rep.fetch(db, user, params, True)
        extra = describe_filters(params, db, user)
        if extra:
            subtitle = f"{subtitle} · {extra}"
    except (ReportProblem, ValueError) as exc:
        raise HTTPException(400, str(exc))
    cols = chosen_cols(db, user, rep, params.get("cols"))
    name = f"{rep.title} {subtitle}".replace("/", "-").replace(" · ", " ").strip()
    name = "".join(ch for ch in name if ch.isalnum() or ch in " -_.")[:90].strip().replace(" ", "_")
    audit(db, user, "EXPORT", f"{rep.title} {fmt} ({len(rows)} rows)")
    if fmt == "xlsx":
        return Response(to_excel(rep, cols, rows, subtitle), media_type=XLSX,
                        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
    return Response(to_pdf(rep, cols, rows, subtitle), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}.pdf"'})


@app.post("/report-columns/{rid}")
async def report_columns(request: Request, rid: str, user=Depends(page_user), db: Session = Depends(get_db)):
    rep = REPORTS.get(rid)
    if not rep:
        raise HTTPException(404, "Unknown report")
    need(user, rep.perm)
    f = await request.form()
    keys = [str(k) for k in f.getlist("col")]
    if f.get("reset"):
        keys = []
    order = str(f.get("order", "")).split(",")
    keys = sorted(keys, key=lambda k: order.index(k) if k in order else 999)
    save_cols(db, user, rep, keys)
    back = str(f.get("back") or "/")
    return RedirectResponse(back if back.startswith("/") else "/", status_code=303)


IMPORT_PERMS = {"leaves": "leave.manage", "holidays": "holidays.manage", "shifts": "shifts.manage",
                "machines": "machines.manage", "companies": "company.manage", "punches": "punches.edit"}
IMPORT_BACK = {"leaves": "/leaves", "holidays": "/settings", "shifts": "/shifts", "machines": "/devices",
               "companies": "/settings", "punches": "/punches"}


@app.get("/templates/{kind}.xlsx")
def import_template(kind: str, user=Depends(page_user)):
    if kind not in generic_import.TEMPLATES:
        raise HTTPException(404, "Unknown template")
    need(user, IMPORT_PERMS[kind])
    return Response(generic_import.template(kind), media_type=XLSX,
                    headers={"Content-Disposition": f"attachment; filename={kind}-import-template.xlsx"})


@app.post("/import/{kind}")
async def import_generic(kind: str, file: UploadFile = File(...), user=Depends(page_user), db: Session = Depends(get_db)):
    if kind not in IMPORT_PERMS or kind == "punches":
        raise HTTPException(404, "Unknown import")
    need(user, IMPORT_PERMS[kind])
    back = IMPORT_BACK[kind]
    try:
        r = generic_import.import_kind(db, kind, file.filename or "", await file.read(), user)
    except ValueError as exc:
        db.rollback()
        log_error("IMPORT", f"{kind} file {file.filename}: {str(exc)[:400]}", detail=str(exc), username=user["username"])
        return go(back, err=f"{file.filename}: {str(exc)[:1500]}")
    audit(db, user, "IMPORT", f"{kind} {file.filename}: {r}")
    return go(back, msg=f"{file.filename}: {r['added']} added, {r['updated']} updated")


@app.post("/employees/photos")
async def employees_photos(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.edit")
    f = await request.form()
    files = [(u.filename or "", await u.read()) for u in f.getlist("files") if getattr(u, "filename", "")]
    try:
        r = bulk_photos(db, files, user.get("company_id"))
    except ValueError as exc:
        log_error("IMPORT", f"photos: {exc}", username=user["username"])
        return go("/employees", err=str(exc))
    if r["unmatched"] or r["bad"]:
        log_error("IMPORT", f"photos: {len(r['unmatched'])} not matched, {len(r['bad'])} unreadable",
                  detail="Not matched (name must be employee code or card no.):\n" + "\n".join(r["unmatched"])
                         + ("\n\nUnreadable:\n" + "\n".join(r["bad"]) if r["bad"] else ""), username=user["username"])
    audit(db, user, "PHOTO_IMPORT", f"{r['saved']} saved, {len(r['unmatched'])} unmatched")
    msg = f"{r['saved']} photos saved"
    err = ""
    if r["unmatched"]:
        err = f"{len(r['unmatched'])} photos did not match an employee code / card no.: " + ", ".join(r["unmatched"][:15])
    if r["bad"]:
        err += (" | " if err else "") + "Unreadable: " + "; ".join(r["bad"][:5])
    return go("/employees", msg=msg, err=err)


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
    ho_name = str(f.get("ho_name", "")).strip() or settings.group_name
    set_setting(db, "ho_name", ho_name, commit=False)
    set_setting(db, "ho_address", str(f.get("ho_address", "")).strip()[:240], commit=False)
    logo = f.get("logo")
    if logo is not None and getattr(logo, "filename", ""):
        try:
            branding.save_logo(await logo.read())
        except ValueError as exc:
            return go("/setup", err=f"Logo: {exc}")
    db.add(User(full_name="Administrator", username=username, password_hash=hash_password(password), role="ADMIN"))
    for code, name in companies:
        db.add(Company(code=code, name=name))
    set_setting(db, "weekend_days", weekend, commit=False)
    for k in ("geofence_lat", "geofence_lon", "geofence_radius_m"):
        if str(f.get(k, "")).strip():
            float(str(f.get(k)))   # validate
            set_setting(db, k, str(f.get(k)).strip(), commit=False)
    db.flush()
    ho = db.scalar(select(Company).where(Company.code == "HO"))
    if ho:      # overtime does not apply to Head Office / Central Office staff (can be changed in the pay policy)
        set_setting(db, "pay.ot_enabled", "false", ho.id, commit=False)
    set_setting(db, "ho_ot_default_done", "1", commit=False)
    set_setting(db, "setup_done", "1", commit=False)
    audit(db, username, "SETUP", f"fresh setup, companies: {', '.join(c for c, _ in companies)}", commit=False)
    db.commit()
    _state["setup_done"] = True
    branding.refresh(db)
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
def dashboard(request: Request, day: str = "", company: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    """Colourful overview with charts. All numbers follow the user's company limit and the company filter."""
    need(user, "dashboard.view")
    from collections import Counter
    from calendar import month_abbr
    from services import charts
    today = now_local().date()
    d = date.fromisoformat(day) if day else today
    comps = visible_companies(db, user)
    cid = company_filter(user, company)
    ids = [cid] if cid else [c.id for c in comps]
    emp_ids = select(Employee.id).where(Employee.company_id.in_(ids or [0]))
    cards = select(Employee.card_no).where(Employee.company_id.in_(ids or [0]))
    present_st = ("PRESENT", "LATE", "MISSING_OUT", "OFFDAY_WORK")

    # --- today by company
    rows = db.execute(select(Employee.company_id, DailyAttendance.status, func.count())
                      .join(Employee, Employee.id == DailyAttendance.employee_id)
                      .where(DailyAttendance.work_date == d, Employee.company_id.in_(ids or [0]))
                      .group_by(Employee.company_id, DailyAttendance.status)).all()
    active = dict(db.execute(select(Employee.company_id, func.count()).where(
        Employee.employment_status == "ACTIVE", Employee.company_id.in_(ids or [0])).group_by(Employee.company_id)).all())
    per = {c.id: {"company": c, "active": active.get(c.id, 0), "present": 0, "late": 0, "absent": 0, "leave": 0, "off": 0}
           for c in comps if c.id in ids}
    for c_id, st, n in rows:
        x = per.get(c_id)
        if not x:
            continue
        if st in present_st:
            x["present"] += n
        if st == "LATE":
            x["late"] += n
        if st == "ABSENT":
            x["absent"] += n
        if st in ("LEAVE_PAID", "LEAVE_UNPAID"):
            x["leave"] += n
        if st == "OFFDAY":
            x["off"] += n
    total = {k: sum(x[k] for x in per.values()) for k in ("active", "present", "late", "absent", "leave", "off")}
    expected = total["present"] + total["absent"] + total["leave"]
    total["rate"] = round(100 * total["present"] / expected) if expected else None
    since = datetime.combine(d, time.min)
    until = since + timedelta(days=1)
    day_punches = db.execute(select(AttendanceLog.punch_time).where(
        AttendanceLog.punch_time >= since, AttendanceLog.punch_time < until,
        or_(AttendanceLog.card_no.in_(cards), AttendanceLog.card_no.not_in(select(Employee.card_no))) if not user.get("company_id") and not cid
        else AttendanceLog.card_no.in_(cards))).scalars().all()
    total["punches"] = len(day_punches)
    total["mobile"] = db.scalar(select(func.count(AttendanceLog.id)).where(
        AttendanceLog.punch_time >= since, AttendanceLog.punch_time < until, AttendanceLog.source == "MOBILE",
        AttendanceLog.card_no.in_(cards))) or 0

    names = [x["company"].code for x in per.values()]
    vals = list(per.values())
    status_chart = charts.stacked_hbars(names, [
        ("Present", charts.STATUS["Present"], [x["present"] - x["late"] for x in vals]),
        ("Late", charts.STATUS["Late"], [x["late"] for x in vals]),
        ("Absent", charts.STATUS["Absent"], [x["absent"] for x in vals]),
        ("Leave", charts.STATUS["Leave"], [x["leave"] for x in vals])]) if vals else ""

    # --- 14-day attendance rate trend
    start14 = d - timedelta(days=13)
    trend_rows = db.execute(select(DailyAttendance.work_date, DailyAttendance.status, func.count())
                            .where(DailyAttendance.work_date >= start14, DailyAttendance.work_date <= d,
                                   DailyAttendance.employee_id.in_(emp_ids))
                            .group_by(DailyAttendance.work_date, DailyAttendance.status)).all()
    by_day: dict = {}
    for wd, st, n in trend_rows:
        x = by_day.setdefault(wd, Counter())
        x[st] += n
    t_labels, t_rate, t_table = [], [], []
    for i in range(14):
        wd = start14 + timedelta(days=i)
        c = by_day.get(wd, Counter())
        pres = sum(c[k] for k in present_st)
        exp = pres + c["ABSENT"] + c["LEAVE_PAID"] + c["LEAVE_UNPAID"]
        rate = round(100 * pres / exp, 1) if exp else None
        t_labels.append(wd.strftime("%d %b"))
        t_rate.append(rate)
        t_table.append((wd.strftime("%a %d %b"), pres, c["ABSENT"], c["LATE"], rate))
    trend_chart = charts.line(t_labels, t_rate, charts.CAT[0], "%", top=100) if any(v is not None for v in t_rate) else ""

    # --- punches per hour on the day
    hours = Counter(t.hour for t in day_punches)
    h_from = min([5] + list(hours)) if hours else 5
    h_to = max([22] + list(hours)) if hours else 22
    hour_labels = [f"{h:02d}" for h in range(h_from, h_to + 1)]
    hour_vals = [hours.get(h, 0) for h in range(h_from, h_to + 1)]
    hour_chart = charts.columns(hour_labels, hour_vals, charts.CAT[0], " punches") if day_punches else ""

    # --- headcount by department
    dept_rows = db.execute(select(Department.name, func.count(Employee.id)).join(Employee, Employee.department_id == Department.id)
                           .where(Employee.employment_status == "ACTIVE", Employee.company_id.in_(ids or [0]))
                           .group_by(Department.name).order_by(func.count(Employee.id).desc())).all()
    no_dept = db.scalar(select(func.count(Employee.id)).where(Employee.employment_status == "ACTIVE",
                                                            Employee.department_id.is_(None),
                                                            Employee.company_id.in_(ids or [0]))) or 0
    dept = [(n or "-", c) for n, c in dept_rows][:9]
    rest = sum(c for _, c in dept_rows[9:]) + no_dept
    if rest:
        dept.append(("Other / none", rest))
    dept_chart = charts.hbars([x[0] for x in dept], [x[1] for x in dept], charts.CAT[2]) if dept else ""

    # --- payroll net pay, last 6 months, by company
    pay_chart, pay_table, pay_legend = "", [], ""
    if "payroll.view" in user["perms"]:
        months = []
        y_, m_ = d.year, d.month
        for _ in range(6):
            months.insert(0, (y_, m_))
            y_, m_ = (y_ - 1, 12) if m_ == 1 else (y_, m_ - 1)
        runs = db.execute(select(PayrollRun.company_id, PayrollRun.year, PayrollRun.month, func.sum(PayrollLine.net_pay))
                          .join(PayrollLine, PayrollLine.run_id == PayrollRun.id)
                          .where(PayrollRun.company_id.in_(ids or [0]), (PayrollRun.year * 12 + PayrollRun.month) >=
                                 months[0][0] * 12 + months[0][1])
                          .group_by(PayrollRun.company_id, PayrollRun.year, PayrollRun.month)).all()
        net = {(c_, yy, mm): float(v or 0) for c_, yy, mm, v in runs}
        comp_list = [c for c in comps if c.id in ids]
        color = {c.id: charts.CAT[i % len(charts.CAT)] for i, c in enumerate(sorted(comps, key=lambda c: c.id))}
        series = [(c.code, color[c.id], [net.get((c.id, yy, mm), 0) for yy, mm in months]) for c in comp_list]
        if any(v for _, _, vals in series for v in vals):
            labels = [f"{month_abbr[mm]} {str(yy)[2:]}" for yy, mm in months]
            pay_chart = charts.stacked_columns(labels, series, " Tk", height=300, width=1280)
            pay_legend = charts.legend([(c.code, color[c.id]) for c in comp_list])
            pay_table = [(lab, [vals[i] for _, _, vals in series]) for i, lab in enumerate(labels)]

    # --- machines, latest punches, late today
    devs = db.scalars(select(Device).order_by(Device.name)).all()
    now = now_local()
    online = sum(1 for x in devs if x.enabled and x.approved and x.last_seen and (now - x.last_seen).total_seconds() < 900)
    recent = db.scalars(select(AttendanceLog).where(AttendanceLog.card_no.in_(cards))
                        .order_by(AttendanceLog.punch_time.desc()).limit(8)).all()
    recent_emps = employees_by_card(db, [r.card_no for r in recent])
    late_rows = db.scalars(select(DailyAttendance).where(DailyAttendance.work_date == d, DailyAttendance.late_minutes > 0,
                                                         DailyAttendance.employee_id.in_(emp_ids))
                           .order_by(DailyAttendance.late_minutes.desc()).limit(8)).all()
    return render(request, "dashboard.html", user, per=vals, total=total, day=d, today=today, devices=devs,
                  online=online, processed=bool(rows), pending=sum(1 for x in devs if not x.approved),
                  status_chart=status_chart, status_legend=charts.legend([(k, v) for k, v in charts.STATUS.items()]),
                  trend_chart=trend_chart, trend_table=t_table, hour_chart=hour_chart,
                  hour_table=list(zip(hour_labels, hour_vals)), dept_chart=dept_chart, dept_table=dept,
                  pay_chart=pay_chart, pay_table=pay_table, pay_legend=pay_legend,
                  pay_codes=[c.code for c in comps if c.id in ids], recent=recent, recent_emps=recent_emps,
                  late_rows=late_rows, companies=comps, company=cid or "")


# ---------------------------------------------------------------- employees
@app.get("/employees", response_class=HTMLResponse)
def employees_page(request: Request, page: int = 1, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.view")
    params = dict(request.query_params)
    r = report_ctx(db, user, "employees", params)
    return render(request, "employees.html", user, r=r, q=params.get("q", ""), page=max(page, 1),
                  pages=max(1, -(-r["total"] // 50)), company=company_filter(user, params.get("company")) or "",
                  status=params.get("status", ""), companies=visible_companies(db, user))


@app.get("/employees/template.xlsx")
def employee_template(user=Depends(page_user)):
    need(user, "employees.edit")
    return Response(template_bytes(), media_type=XLSX,
                    headers={"Content-Disposition": "attachment; filename=employee-import-template.xlsx"})


@app.post("/employees/import")
async def employees_import(file: UploadFile = File(...), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.edit")
    try:
        result = import_employees(db, await file.read(), user.get("company_id"),
                                  allow_salary="salary.edit" in user["perms"])
    except Exception as exc:
        db.rollback()
        log_error("IMPORT", f"employee file {file.filename}: {str(exc)[:450]}", detail=str(exc),
                  username=user["username"])
        return go("/employees", err=str(exc)[:1500])
    audit(db, user, "EMPLOYEE_IMPORT", f"{file.filename}: {result}")
    return go("/employees", msg=f"Import done: {result['added']} added, {result['updated']} updated")


PROFILE_FIELDS = ("father_name", "mother_name", "spouse_name", "gender", "blood_group", "religion", "marital_status",
                  "nid", "birth_certificate_no", "passport_no", "phone", "email", "emergency_contact", "present_address",
                  "permanent_address", "education", "notes")


def _emp_or_404(db: Session, user: dict, emp_id: int) -> Employee:
    emp = db.get(Employee, emp_id)
    if not emp:
        raise HTTPException(404, "Employee not found")
    check_company(user, emp.company_id)
    return emp


@app.get("/employees/new", response_class=HTMLResponse)
def employee_new(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.edit")
    return render(request, "employee_form.html", user, e=None, companies=visible_companies(db, user),
                  shifts=db.scalars(select(Shift).order_by(Shift.code)).all(),
                  departments=db.scalars(select(Department).order_by(Department.name)).all())


@app.get("/employees/{emp_id}", response_class=HTMLResponse)
def employee_profile(request: Request, emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    """Full profile: photo, personal and job details, this month's attendance, recent punches, leave, payslips."""
    need(user, "employees.view")
    emp = _emp_or_404(db, user, emp_id)
    today = now_local().date()
    first, last = month_bounds(today.year, today.month)
    month_rows = db.scalars(select(DailyAttendance).where(DailyAttendance.employee_id == emp.id,
                                                          DailyAttendance.work_date >= first,
                                                          DailyAttendance.work_date <= last)
                            .order_by(DailyAttendance.work_date)).all()
    summary = {k: 0 for k in ("present", "late", "absent", "leave", "off", "ot")}
    for r in month_rows:
        summary["present"] += r.status in ("PRESENT", "LATE", "MISSING_OUT", "OFFDAY_WORK")
        summary["late"] += r.late_minutes > 0
        summary["absent"] += r.status == "ABSENT"
        summary["leave"] += r.status in ("LEAVE_PAID", "LEAVE_UNPAID")
        summary["off"] += r.status == "OFFDAY"
        summary["ot"] += r.ot_minutes + r.extra_ot_minutes
    fl = first_last(db, today - timedelta(days=30), today, q=emp.card_no)
    fl = [r for r in fl if r["card_no"] == emp.card_no][::-1]
    leaves = db.scalars(select(Leave).where(Leave.employee_id == emp.id).order_by(Leave.start_date.desc()).limit(20)).all()
    show_salary = "salary.view" in user["perms"]
    documents = db.scalars(select(EmployeeDocument).where(EmployeeDocument.employee_id == emp.id)
                           .order_by(EmployeeDocument.doc_type, EmployeeDocument.uploaded_at)).all()
    pays = []
    if show_salary:
        pays = db.execute(select(PayrollLine, PayrollRun).join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
                          .where(PayrollLine.employee_id == emp.id)
                          .order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).limit(12)).all()
    return render(request, "employee_profile.html", user, e=emp, summary=summary, month_rows=month_rows, today=today,
                  first_last=fl, leaves=leaves, pays=pays, show_salary=show_salary, documents=documents,
                  doc_types=docs.DOC_TYPES, bal=leave_svc.balances(db, emp), leave_labels=leave_svc.STATUS_LABEL,
                  components=comp_svc.for_employee(emp, *comp_svc.load_all(db)) if show_salary else [],
                  calcs=comp_svc.CALCS)


@app.get("/employees/{emp_id}/edit", response_class=HTMLResponse)
def employee_form(request: Request, emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.edit")
    emp = _emp_or_404(db, user, emp_id)
    return render(request, "employee_form.html", user, e=emp, companies=visible_companies(db, user),
                  shifts=db.scalars(select(Shift).order_by(Shift.code)).all(),
                  departments=db.scalars(select(Department).order_by(Department.name)).all())


@app.get("/employees/{emp_id}/photo")
def employee_photo(emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.view")
    emp = _emp_or_404(db, user, emp_id)
    path = photo_path(emp.photo)
    if not path:
        return RedirectResponse("/static/img/no-photo.png", status_code=303)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


@app.post("/employees/{emp_id}/documents")
async def employee_docs_upload(request: Request, emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.edit")
    emp = _emp_or_404(db, user, emp_id)
    f = await request.form()
    doc_type = str(f.get("doc_type") or "OTHER")
    title = str(f.get("title") or "").strip()
    saved, errors = 0, []
    for up in f.getlist("files"):
        if not getattr(up, "filename", ""):
            continue
        try:
            stored, size = docs.save_file(f"emp_{emp.id}", up.filename, await up.read())
        except ValueError as exc:
            errors.append(str(exc))
            continue
        db.add(EmployeeDocument(employee_id=emp.id, doc_type=doc_type if doc_type in docs.DOC_TYPES else "OTHER",
                                title=title or up.filename, stored_name=stored, original_name=up.filename[:200],
                                size=size, uploaded_by=user["username"]))
        saved += 1
    audit(db, user, "DOCUMENT_UPLOAD", f"{emp.employee_code}: {saved} file(s) {doc_type}", commit=False)
    db.commit()
    if errors:
        log_error("IMPORT", f"documents for {emp.employee_code}: {'; '.join(errors)}", username=user["username"])
    return go(f"/employees/{emp.id}#documents", msg=f"{saved} document(s) saved" if saved else "",
              err="; ".join(errors) or ("" if saved else "Choose a file"))


@app.get("/employees/{emp_id}/documents/{doc_id}")
def employee_doc_get(emp_id: int, doc_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.view")
    emp = _emp_or_404(db, user, emp_id)
    d = db.get(EmployeeDocument, doc_id)
    path = docs.path_of(d.stored_name) if d and d.employee_id == emp.id else None
    if not path:
        raise HTTPException(404, "Document not found")
    return FileResponse(path, media_type=docs.media_type(d.stored_name), filename=d.original_name or path.name,
                        content_disposition_type="inline")


@app.post("/employees/{emp_id}/documents/{doc_id}/delete")
def employee_doc_delete(emp_id: int, doc_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.edit")
    emp = _emp_or_404(db, user, emp_id)
    d = db.get(EmployeeDocument, doc_id)
    if d and d.employee_id == emp.id:
        docs.delete_file(d.stored_name)
        audit(db, user, "DOCUMENT_DELETE", f"{emp.employee_code}: {d.title}", commit=False)
        db.delete(d)
        db.commit()
    return go(f"/employees/{emp.id}#documents", msg="Document removed")


@app.get("/employees/{emp_id}/profile.pdf")
def employee_profile_pdf(emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.view")
    from reports.profile_pdf import profile_pdf
    emp = _emp_or_404(db, user, emp_id)
    documents = db.scalars(select(EmployeeDocument).where(EmployeeDocument.employee_id == emp.id)
                           .order_by(EmployeeDocument.uploaded_at)).all()
    pdf = profile_pdf(emp, photo_path(emp.photo), leave_svc.balances(db, emp), documents,
                      show_salary="salary.view" in user["perms"],
                      components=comp_svc.for_employee(emp, *comp_svc.load_all(db)))
    audit(db, user, "PROFILE_PDF", emp.employee_code)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="profile-{emp.employee_code}.pdf"'})


@app.post("/employees/save")
async def employee_save(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.edit")
    f = await request.form()
    val = lambda k: str(f.get(k) or "").strip()  # noqa: E731
    emp_id = val("emp_id")
    back = f"/employees/{emp_id}/edit" if emp_id else "/employees/new"
    try:
        company_id = int(val("company_id") or 0)
        check_company(user, company_id)
        e = db.get(Employee, int(emp_id)) if emp_id else Employee()
        if emp_id:
            check_company(user, e.company_id)
        if not db.get(Company, company_id):
            raise ValueError("Choose the company")
        code = val("employee_code")
        card = val("card_no") or code
        if not code or not val("full_name"):
            raise ValueError("Employee code and name are required")
        clash = db.scalar(select(Employee).where(or_(Employee.employee_code == code, Employee.card_no == card),
                                                 Employee.id != (e.id or 0)))
        if clash:
            raise ValueError(f"Employee code or card no. already used by {clash.employee_code} "
                             f"({clash.company.code if clash.company else ''})")
        can_salary = "salary.edit" in user["perms"]
        if can_salary:
            g = dec(val("gross"), "Gross")
            if val("auto_split") and g > 0:
                comp = split_gross(g, load_policy(db, company_id))
                parts = [comp.basic, comp.house_rent, comp.medical, comp.transport, comp.food]
            else:
                parts = [dec(val("basic"), "Basic"), dec(val("house_rent"), "House rent"), dec(val("medical"), "Medical"),
                         dec(val("transport"), "Transport"), dec(val("food"), "Food")]
                if sum(parts) != g:
                    raise ValueError(f"Salary parts add up to {sum(parts)}, not gross {g}. Tick 'split automatically'.")
        pin = val("mobile_pin")
        if pin and (not pin.isdigit() or len(pin) < 4):
            raise ValueError("Mobile PIN must be at least 4 digits")
        dept = None
        if val("department"):
            dept = db.scalar(select(Department).where(Department.name == val("department")))
            if not dept:
                dept = Department(name=val("department"))
                db.add(dept)
        e.company_id, e.employee_code, e.card_no, e.full_name = company_id, code, card, val("full_name")
        e.department, e.designation, e.section, e.grade = dept, val("designation") or None, val("section") or None, val("grade") or None
        e.shift_id = int(val("shift_id")) if val("shift_id") else None
        e.joined_on = date.fromisoformat(val("joined_on")) if val("joined_on") else None
        e.left_on = date.fromisoformat(val("left_on")) if val("left_on") else None
        e.date_of_birth = date.fromisoformat(val("date_of_birth")) if val("date_of_birth") else None
        e.employment_status = val("employment_status") or "ACTIVE"
        if can_salary:          # salary, pay mode and OT eligibility only for roles with "salary.edit"
            e.gross = g
            e.basic, e.house_rent, e.medical, e.transport, e.food = parts
            e.payment_mode = val("payment_mode") or "CASH"
            e.bank_name, e.account_no = val("bank_name") or None, val("account_no") or None
            if f.get("ot_field"):       # the form has the OT tick box (other callers leave OT unchanged)
                e.ot_eligible = bool(f.get("ot_eligible"))
            elif not emp_id:
                e.ot_eligible = True
        elif not emp_id:
            e.gross = e.basic = e.house_rent = e.medical = e.transport = e.food = Decimal(0)
            e.payment_mode, e.ot_eligible = "CASH", True
        for k in PROFILE_FIELDS:
            setattr(e, k, val(k)[:300] or None if k != "notes" else val(k) or None)
        e.allow_mobile = bool(f.get("allow_mobile"))
        if pin:
            e.mobile_pin_hash = hash_password(pin)
        if not emp_id:
            db.add(e)
        db.flush()
        photo = f.get("photo")
        if photo is not None and getattr(photo, "filename", ""):
            e.photo = save_photo(e.id, await photo.read())
        if val("remove_photo"):
            e.photo = None
        audit(db, user, "EMPLOYEE_SAVE", f"{code} {e.full_name}" + (" (PIN changed)" if pin else ""), commit=False)
        db.commit()
    except (ValueError, TypeError) as exc:
        db.rollback()
        return go(back, err=str(exc))
    return go(f"/employees/{e.id}", msg=f"Saved {e.employee_code}")


# ---------------------------------------------------------------- punch machines
@app.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, edit: int | None = None, new: str = "", user=Depends(page_user),
                 db: Session = Depends(get_db)):
    need(user, "machines.manage")
    r = report_ctx(db, user, "machines", {})
    return render(request, "devices.html", user, r=r, devices=r["rows"], e=db.get(Device, edit) if edit else None,
                  ips=maintenance.lan_ips(), brands=devices.BRANDS, show_form=bool(edit or new))


@app.post("/devices/save")
def device_save(device_id: str = Form(""), name: str = Form(""), brand: str = Form(...),
                serial_number: str = Form(""), ip_address: str = Form(""), port: str = Form(""),
                machine_no: str = Form(""), direction: str = Form("BOTH"), username: str = Form(""),
                password: str = Form(""), location: str = Form(""), approved: str = Form(""),
                enabled: str = Form(""), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "machines.manage")
    brand = brand.upper()
    back = f"/devices?edit={device_id}" if device_id else "/devices?new=1"
    if brand not in devices.BRANDS:
        return go(back, err="Choose the machine type")
    ip = ip_address.strip()
    if brand in devices.PULL_BRANDS:
        import ipaddress
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return go(back, err="Enter the machine's IP address, e.g. 192.168.10.17")
    if brand == "ZKTECO_PUSH" and not serial_number.strip():
        return go(back, err="Enter the serial number (Menu > System Info > Device Info)")
    if brand == "ZKTECO" and password.strip() and not password.strip().isdigit():
        return go(back, err="Comm Key must be a number (0 = none)")
    try:
        port_n = int(port) if port.strip() else (4370 if brand == "ZKTECO" else 80)
        mno = int(machine_no) if machine_no.strip() else None
    except ValueError:
        return go(back, err="Port and Machine No. must be numbers")
    clash = db.scalar(select(Device).where(Device.ip_address == ip, Device.brand == brand,
                                           Device.id != (int(device_id) if device_id else 0))) if ip else None
    if clash and brand in devices.PULL_BRANDS:
        return go(back, err=f"{ip} is already added as '{clash.name}'")
    d = db.get(Device, int(device_id)) if device_id else Device()
    d.brand, d.ip_address, d.port, d.machine_no = brand, ip or None, port_n, mno
    d.name = name.strip() or (f"Machine {mno}" if mno else f"{brand.title()} {ip or serial_number}")
    d.serial_number = serial_number.strip() or d.serial_number
    d.direction = direction if direction in ("IN", "OUT", "BOTH") else "BOTH"
    d.location, d.username = location.strip() or None, username.strip() or None
    if password.strip():
        d.password_enc = devices.encrypt(password.strip())
    d.approved, d.enabled = bool(approved), bool(enabled)
    if not device_id:
        db.add(d)
    audit(db, user, "DEVICE_SAVE", f"{brand} {d.name} {d.ip_address or ''} {d.serial_number or ''} "
                                   f"active={d.enabled} approved={d.approved}", commit=False)
    db.commit()
    if brand in devices.PULL_BRANDS and d.enabled and d.approved:
        try:
            info = devices.test_device(d)
            d.last_status = f"{now_local():%d/%m %H:%M} connected: {info}"
            db.commit()
            return go("/devices", msg=f"{d.name} saved and connected: {info}. Click 'Download now' to get its punches.")
        except Exception as exc:  # noqa: BLE001
            d.last_status = f"{now_local():%d/%m %H:%M} ERROR: {exc}"
            db.commit()
            log_error("MACHINE", f"{d.name} ({ip}): {exc}", username=user["username"])
            return go("/devices", err=f"{d.name} saved, but the connection test failed: {exc}")
    return go("/devices", msg=f"Machine {d.name} saved")


@app.post("/devices/{device_id}/test")
def device_test(device_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "machines.manage")
    d = db.get(Device, device_id)
    if not d:
        raise HTTPException(404, "Unknown machine")
    try:
        info = devices.test_device(d)
    except Exception as exc:  # noqa: BLE001
        d.last_status = f"{now_local():%d/%m %H:%M} ERROR: {exc}"
        db.commit()
        log_error("MACHINE", f"{d.name} ({d.ip_address}): {exc}", username=user["username"])
        return go("/devices", err=f"{d.name}: {exc}")
    return go("/devices", msg=f"{d.name}: connected - {info}")


@app.post("/devices/{device_id}/pull")
def device_pull(device_id: int, days_back: str = Form(""), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "punches.edit")
    d = db.get(Device, device_id)
    if not d or d.brand not in devices.PULL_BRANDS:
        return go("/devices", err="This machine sends punches by itself - nothing to download")
    if not d.approved or not d.enabled:
        return go("/devices", err="Tick 'Active' for this machine first")
    db_days = int(days_back) if days_back.strip().isdigit() else None
    r = devices.pull_device(db, d, db_days)
    audit(db, user, "DEVICE_DOWNLOAD", f"{d.name}: {r}")
    if not r["ok"]:
        return go("/devices", err=f"{d.name}: {r['message']}")
    return go("/devices", msg=f"{d.name}: read {r['read']} punches, {r['inserted']} new")


@app.post("/devices/pull-all")
def device_pull_all(user=Depends(page_user)):
    need(user, "punches.edit")
    results = devices.pull_all()
    if not results:
        return go("/devices", err="No active ZKTeco-by-IP or Hikvision machines to download from")
    errs = [r for r in results if "ERROR" in r]
    return go("/devices", msg="Downloaded: " + "; ".join(r for r in results if "ERROR" not in r),
              err="; ".join(errs))


@app.post("/devices/{device_id}/delete")
def device_delete(device_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "machines.manage")
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
def attendance_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "attendance.view")
    p = dict(request.query_params)
    d = date.fromisoformat(p["day"]) if p.get("day") else now_local().date()
    p["day"] = str(d)
    r = report_ctx(db, user, "attendance", p)
    return render(request, "attendance.html", user, r=r, day=d, status=p.get("att_status", ""),
                  company=company_filter(user, p.get("company")) or "", companies=visible_companies(db, user))


@app.post("/attendance/process")
def attendance_process(month: str = Form(...), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "attendance.process")
    y, m = parse_month(month)
    ids = [c.id for c in visible_companies(db, user)]
    n = len(process_month(db, y, m, company_ids=ids))
    audit(db, user, "ATTENDANCE_PROCESS", f"{y}-{m:02d}: {n} employees")
    return go("/attendance", msg=f"Processed {month_name[m]} {y} for {n} employees "
                                 "(companies with a FINAL payroll for that month are locked and skipped)")


@app.post("/attendance/manual")
def attendance_manual(card_no: str = Form(...), punch_time: str = Form(...), reason: str = Form(...),
                      user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "punches.edit")
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
    need(user, "attendance.view", "reports.export")
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
    need(user, "attendance.view", "reports.export")
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


@app.get("/attendance/punches")
def old_punches_link(request: Request):
    return RedirectResponse("/punches?" + str(request.query_params), status_code=303)


# ---------------------------------------------------------------- Punch menu
def _dates(start: str, end: str) -> tuple[date, date]:
    today = now_local().date()
    s_ = date.fromisoformat(start) if start else today
    e_ = date.fromisoformat(end) if end else s_
    if e_ < s_:
        s_, e_ = e_, s_
    if (e_ - s_).days > 92:
        raise HTTPException(400, "Choose at most 3 months at a time")
    return s_, e_


@app.get("/punches", response_class=HTMLResponse)
def punches_page(request: Request, page: int = 1, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "punches.view")
    p = dict(request.query_params)
    r = report_ctx(db, user, "punches", p)
    s_, e_ = _dates(p.get("start", ""), p.get("end", ""))
    return render(request, "punches.html", user, r=r, start=s_, end=e_, company=company_filter(user, p.get("company")) or "",
                  q=p.get("q", ""), machine=p.get("machine", ""), source=p.get("source", ""), unknown=p.get("unknown", ""),
                  page=max(page, 1), pages=max(1, -(-r["total"] // 200)), companies=visible_companies(db, user),
                  machines=db.scalars(select(Device).order_by(Device.name)).all())


@app.get("/punches.xlsx")
def punches_xlsx(start: str = "", end: str = "", company: str = "", q: str = "", machine: str = "",
                 source: str = "", unknown: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "punches.view", "reports.export")
    s_, e_ = _dates(start, end)
    stmt = punch_query(db, s_, e_, company_filter(user, company), q, int(machine) if machine.isdigit() else None,
                       source, bool(unknown))
    rows = db.scalars(stmt.order_by(AttendanceLog.punch_time, AttendanceLog.card_no).limit(300000)).all()
    return Response(punches_sheet(rows, employees_by_card(db, [r.card_no for r in rows]), s_, e_), media_type=XLSX,
                    headers={"Content-Disposition": f"attachment; filename=punches-{s_}-to-{e_}.xlsx"})


@app.post("/punches/import")
async def punches_import(file: UploadFile = File(...), user=Depends(page_user), db: Session = Depends(get_db)):
    """Machine export files: Excel (.xlsx), CSV, ZKTeco attlog.dat - columns are recognised by name."""
    need(user, "punches.edit")
    content = await file.read()
    try:
        rows, row_errors = read_punch_file(file.filename or "file", content)
    except ImportProblem as exc:
        log_error("IMPORT", f"punch file {file.filename}: {exc}", username=user["username"])
        return go("/punches", err=f"{file.filename}: {exc}")
    except Exception as exc:  # noqa: BLE001
        log_error("IMPORT", f"punch file {file.filename}: {exc}", username=user["username"], exc=exc)
        return go("/punches", err=f"{file.filename}: cannot read this file ({exc})")
    if row_errors:
        log_error("IMPORT", f"punch file {file.filename}: {len(row_errors)} rows skipped",
                  detail="\n".join(row_errors[:500]), username=user["username"])
    if not rows:
        return go("/punches", err=f"{file.filename}: no punches found" +
                  (f" ({'; '.join(row_errors[:5])})" if row_errors else ""))
    ins, dup = insert_punches(db, rows, "FILE", user["username"])
    lo, hi = min(r[1] for r in rows).date(), max(r[1] for r in rows).date()
    audit(db, user, "PUNCH_FILE_IMPORT", f"{file.filename}: {ins} new, {dup} duplicates, {len(row_errors)} bad rows")
    return go(f"/punches?start={lo}&end={min(hi, lo + timedelta(days=92))}",
              msg=f"{file.filename}: {ins} punches added, {dup} already there"
                  + (f", {len(row_errors)} rows skipped (see Error log)" if row_errors else "")
                  + ". Process the month on Daily attendance to update attendance.")


@app.post("/punches/download")
def punches_download(user=Depends(page_user)):
    need(user, "punches.edit")
    results = devices.pull_all()
    if not results:
        return go("/punches", err="No active machines to download from - add them on the Machines page")
    errs = [r for r in results if "ERROR" in r]
    return go("/punches", msg="Downloaded: " + "; ".join(r for r in results if "ERROR" not in r),
              err=("Problems (see Error log): " + "; ".join(errs)) if errs else "")


@app.get("/punches/first-last", response_class=HTMLResponse)
def first_last_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "punches.view")
    p = dict(request.query_params)
    r = report_ctx(db, user, "first_last", p)
    s_, e_ = _dates(p.get("start", ""), p.get("end", ""))
    return render(request, "first_last.html", user, r=r, start=s_, end=e_, q=p.get("q", ""),
                  company=company_filter(user, p.get("company")) or "", companies=visible_companies(db, user))


@app.get("/punches/first-last.xlsx")
def first_last_xlsx(start: str = "", end: str = "", company: str = "", q: str = "", user=Depends(page_user),
                    db: Session = Depends(get_db)):
    need(user, "punches.view", "reports.export")
    s_, e_ = _dates(start, end)
    cid = company_filter(user, company)
    rows = first_last(db, s_, e_, cid, q, include_unknown=not cid)
    return Response(first_last_sheet(rows, s_, e_), media_type=XLSX,
                    headers={"Content-Disposition": f"attachment; filename=first-last-{s_}-to-{e_}.xlsx"})


# ---------------------------------------------------------------- error log
@app.get("/errors", response_class=HTMLResponse)
def errors_page(request: Request, source: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "logs.view")
    r = report_ctx(db, user, "errors", dict(request.query_params))
    sources = [x[0] for x in db.execute(select(ErrorLog.source).distinct())]
    return render(request, "errors.html", user, r=r, source=source, sources=sources)


@app.post("/errors/clear")
def errors_clear(user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "logs.view", "users.manage")
    n = db.query(ErrorLog).delete()
    audit(db, user, "ERRORLOG_CLEAR", f"{n} entries", commit=False)
    db.commit()
    return go("/errors", msg=f"Cleared {n} entries")


@app.get("/errors/server.log")
def errors_server_log(user=Depends(page_user)):
    need(user, "logs.view")
    f = DATA_DIR / "logs" / "server.log"
    if not f.exists():
        raise HTTPException(404, "No server log yet")
    return FileResponse(f, media_type="text/plain", filename="server.log")


# ---------------------------------------------------------------- leave
@app.get("/leaves", response_class=HTMLResponse)
def leaves_page(request: Request, emp: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "attendance.view")
    r = report_ctx(db, user, "leaves", dict(request.query_params))
    lookup, bal = None, []
    if emp.strip():
        lookup = db.scalar(select(Employee).where(or_(Employee.employee_code == emp.strip(), Employee.card_no == emp.strip())))
        if lookup and (not user.get("company_id") or lookup.company_id == user["company_id"]):
            bal = leave_svc.balances(db, lookup)
        else:
            lookup = None
    waiting = len(leave_svc.pending_for(db, user, user.get("company_id")))
    return render(request, "leaves.html", user, r=r, types=leave_svc.types(db), lookup=lookup, bal=bal, emp=emp,
                  waiting=waiting, today=now_local().date())


async def _leave_document(f, emp: Employee) -> str | None:
    up = f.get("document")
    if not getattr(up, "filename", ""):
        return None
    stored, _ = docs.save_file(f"emp_{emp.id}/leave", up.filename, await up.read())
    return stored


@app.post("/leaves")
async def leave_add(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    """HR grants leave (approved at once). Document rules of the leave type still apply."""
    need(user, "leave.manage")
    f = await request.form()
    code = str(f.get("employee_code") or "").strip()
    emp = db.scalar(select(Employee).where(or_(Employee.employee_code == code, Employee.card_no == code)))
    if not emp:
        return go("/leaves", err=f"Unknown employee code {code}")
    check_company(user, emp.company_id)
    lt = db.scalar(select(LeaveType).where(LeaveType.code == str(f.get("leave_type") or "")))
    try:
        s, e = date.fromisoformat(str(f.get("start_date"))), date.fromisoformat(str(f.get("end_date")))
        has_doc = bool(getattr(f.get("document"), "filename", ""))
        over = bool(f.get("over_balance")) and user["role"] == "ADMIN"
        days = leave_svc.validate(db, emp, lt, s, e, has_doc, ignore_balance=over)
        stored = await _leave_document(f, emp)
    except ValueError as exc:
        return go(f"/leaves?emp={emp.employee_code}", err=f"{emp.employee_code}: {exc}")
    db.add(Leave(employee_id=emp.id, start_date=s, end_date=e, leave_type=lt.code, paid=lt.paid, days=days,
                 status="APPROVED", source="HR", note=str(f.get("note") or "").strip()[:250] or None,
                 reason=str(f.get("note") or "").strip()[:300] or None, document=stored,
                 requested_by=user["username"], requested_at=now_local(), final_approved_by=user["username"],
                 final_approved_at=now_local()))
    audit(db, user, "LEAVE_ADD", f"{emp.employee_code} {lt.code} {s}..{e} ({days} days)", commit=False)
    db.commit()
    return go(f"/leaves?emp={emp.employee_code}", msg=f"Leave saved: {lt.name} for {emp.full_name} ({days} day(s)) - "
                                                     "process the month again to apply it")


@app.post("/leaves/{leave_id}/delete")
def leave_delete(leave_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "leave.manage")
    lv = db.get(Leave, leave_id)
    if lv:
        emp = db.get(Employee, lv.employee_id)
        check_company(user, emp.company_id if emp else None)
        audit(db, user, "LEAVE_DELETE", f"emp {lv.employee_id} {lv.leave_type} {lv.start_date}..{lv.end_date}",
              commit=False)
        if lv.document:
            docs.delete_file(lv.document)
        db.delete(lv)
        db.commit()
    return go("/leaves", msg="Leave removed")


@app.get("/leaves/{leave_id}/document")
def leave_document(leave_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    lv = db.get(Leave, leave_id)
    if not lv or not lv.document:
        raise HTTPException(404, "No document")
    emp = lv.employee
    if not ({"attendance.view", "leave.approve_final"} & user["perms"] or leave_svc.can_act(user, lv, emp)):
        raise HTTPException(403, "Not allowed")
    check_company(user, emp.company_id)
    path = docs.path_of(lv.document)
    if not path:
        raise HTTPException(404, "Document file is missing")
    return FileResponse(path, media_type=docs.media_type(lv.document), filename=path.name,
                        content_disposition_type="inline")


@app.get("/leaves/approvals", response_class=HTMLResponse)
def leave_approvals(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    if not {"leave.approve_dept", "leave.approve_final"} & user["perms"] and user["role"] != "ADMIN":
        raise HTTPException(403, "Only Department Heads and final approvers see leave requests")
    rows = leave_svc.pending_for(db, user, user.get("company_id"))
    recent = db.scalars(select(Leave).where(Leave.source == "SELF", Leave.status.in_(("APPROVED", "REJECTED")),
                                            or_(Leave.dept_approved_by == user["username"],
                                                Leave.final_approved_by == user["username"],
                                                Leave.rejected_by == user["username"]))
                        .order_by(Leave.id.desc()).limit(30)).all()
    info = {lv.id: leave_svc.balances(db, lv.employee) for lv in rows}
    my_depts = db.scalars(select(Department).where(Department.head_username == user["username"])).all()
    return render(request, "leave_approvals.html", user, rows=rows, info=info, recent=recent, my_depts=my_depts,
                  labels=leave_svc.STATUS_LABEL, workflow=leave_svc.WORKFLOWS[leave_svc.workflow(db)],
                  types={t.code: t for t in leave_svc.types(db, active_only=False)})


@app.post("/leaves/{leave_id}/decide")
def leave_decide(leave_id: int, action: str = Form(...), reason: str = Form(""), user=Depends(page_user),
                 db: Session = Depends(get_db)):
    lv = db.get(Leave, leave_id)
    if not lv:
        return go("/leaves/approvals", err="Leave request not found")
    emp = lv.employee
    if not leave_svc.can_act(user, lv, emp):
        return go("/leaves/approvals", err="You cannot decide this request (already decided or not your department)")
    if user.get("company_id"):
        check_company(user, emp.company_id)
    if action == "approve":
        lt = db.scalar(select(LeaveType).where(LeaveType.code == lv.leave_type))
        try:      # balance may have changed since the request was made
            leave_svc.validate(db, emp, lt, lv.start_date, lv.end_date, True, exclude_id=lv.id,
                               ignore_balance=user["role"] == "ADMIN")
        except ValueError as exc:
            return go("/leaves/approvals", err=f"{emp.employee_code}: {exc}")
        st = leave_svc.approve(db, lv, user)
        msg = f"{emp.full_name}: " + ("approved" if st == "APPROVED" else "approved - sent for final approval")
    elif action == "reject":
        if not reason.strip():
            return go("/leaves/approvals", err="Write a reason for rejecting")
        leave_svc.reject(lv, user, reason)
        msg = f"{emp.full_name}: rejected"
    else:
        return go("/leaves/approvals", err="Unknown action")
    audit(db, user, "LEAVE_" + action.upper(), f"{emp.employee_code} {lv.leave_type} {lv.start_date}..{lv.end_date} "
                                                f"-> {lv.status}", commit=False)
    db.commit()
    return go("/leaves/approvals", msg=msg)


# ---------------------------------------------------------------- leave policy (Admin)
@app.get("/leave-policy", response_class=HTMLResponse)
def leave_policy_page(request: Request, edit: int | None = None, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "leave.policy")
    users = db.scalars(select(User).where(User.active == True).order_by(User.username)).all()  # noqa: E712
    return render(request, "leave_policy.html", user, types=leave_svc.types(db, active_only=False),
                  e=db.get(LeaveType, edit) if edit else None, workflows=leave_svc.WORKFLOWS,
                  workflow=leave_svc.workflow(db), depts=db.scalars(select(Department).order_by(Department.name)).all(),
                  users=users)


@app.post("/leave-policy/type")
def leave_type_save(type_id: str = Form(""), code: str = Form(...), name: str = Form(...), days_per_year: int = Form(0),
                    paid: str = Form(""), doc_rule: str = Form("NEVER"), doc_after_days: int = Form(0),
                    doc_label: str = Form(""), gender: str = Form(""), self_request: str = Form(""),
                    active: str = Form(""), sort: int = Form(0), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "leave.policy")
    code = "".join(ch for ch in code.strip().upper() if ch.isalnum() or ch == "_")[:20]
    if not code or not name.strip():
        return go("/leave-policy", err="Code and name are needed")
    if doc_rule not in ("NEVER", "ALWAYS", "OVER_DAYS") or days_per_year < 0 or doc_after_days < 0:
        return go("/leave-policy", err="Check the values")
    t = db.get(LeaveType, int(type_id)) if type_id else LeaveType()
    if db.scalar(select(LeaveType).where(LeaveType.code == code, LeaveType.id != (t.id or 0))):
        return go("/leave-policy", err=f"Leave type {code} already exists")
    if t.id and t.code != code and db.scalar(select(Leave.id).where(Leave.leave_type == t.code).limit(1)):
        return go("/leave-policy", err=f"{t.code} is already used by leave records - its code cannot change")
    t.code, t.name, t.days_per_year, t.paid = code, name.strip()[:80], days_per_year, bool(paid)
    t.doc_rule, t.doc_after_days = doc_rule, doc_after_days
    t.doc_label = doc_label.strip()[:80] or "Document"
    t.gender = gender if gender in ("Male", "Female") else None
    t.self_request, t.active, t.sort = bool(self_request), bool(active), sort
    if not type_id:
        db.add(t)
    audit(db, user, "LEAVE_TYPE_SAVE", f"{code}: {days_per_year} days/yr, doc {doc_rule} {doc_after_days}, "
                                       f"paid={bool(paid)}", commit=False)
    db.commit()
    return go("/leave-policy", msg=f"Leave type {code} saved")


@app.post("/leave-policy/workflow")
async def leave_workflow_save(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "leave.policy")
    f = await request.form()
    w = str(f.get("workflow") or "")
    if w not in leave_svc.WORKFLOWS:
        return go("/leave-policy", err="Choose an approval workflow")
    set_setting(db, "leave_approval", w, commit=False)
    names = {u.username for u in db.scalars(select(User))}
    for d in db.scalars(select(Department)):
        v = str(f.get(f"head_{d.id}") or "").strip()
        d.head_username = v if v in names else None
    audit(db, user, "LEAVE_WORKFLOW", w, commit=False)
    db.commit()
    return go("/leave-policy", msg="Approval workflow and Department Heads saved")


# ---------------------------------------------------------------- shifts
@app.get("/shifts", response_class=HTMLResponse)
def shifts_page(request: Request, edit: int | None = None, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "attendance.view")
    r = report_ctx(db, user, "shifts", {})
    return render(request, "shifts.html", user, r=r, shifts=r["rows"], companies=visible_companies(db, user),
                  e=db.get(Shift, edit) if edit else None)


@app.post("/shifts/save")
def shift_save(shift_id: str = Form(""), code: str = Form(...), name: str = Form(...), start: str = Form(...),
               end: str = Form(...), grace_minutes: int = Form(10), break_minutes: int = Form(60),
               break_after_minutes: int = Form(300), early_window_minutes: int = Form(180),
               ot_round_minutes: int = Form(30), max_ot_minutes: int = Form(120), offday_work_is_ot: str = Form(""),
               late_count_from: str = Form("START"), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "shifts.manage")
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
    s.late_count_from = "GRACE_END" if late_count_from == "GRACE_END" else "START"
    if not shift_id:
        db.add(s)
    audit(db, user, "SHIFT_SAVE", f"{code} {start}-{end}", commit=False)
    db.commit()
    return go("/shifts", msg=f"Shift {code} saved - process the month again to apply changes")


@app.post("/shifts/assign")
def shift_assign(shift_id: int = Form(...), employee_codes: str = Form(""), department: str = Form(""),
                 company_id: str = Form(""), user=Depends(page_user), db: Session = Depends(get_db)):
    """Assign a shift to employee codes, a department and/or a whole company."""
    need(user, "shifts.manage")
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
    need(user, "payroll.view")
    y, m = parse_month(month)
    comps = visible_companies(db, user)
    cid = company_filter(user, company) or (comps[0].id if comps else None)
    check_company(user, cid)
    runs = {r.company_id: r for r in db.scalars(select(PayrollRun).where(PayrollRun.year == y, PayrollRun.month == m))}
    run = runs.get(cid)
    lines = sorted(run.lines, key=lambda l: (l.department, l.employee_code)) if run else []
    totals = {k: sum((getattr(l, k) or 0) for l in lines) for k in
              ("earned_gross", "ot_amount", "absent_deduction", "total_deductions", "net_pay", "custom_additions",
               "late_deduction")} if lines else {}
    by_mode: dict[str, Decimal] = {}
    for l in lines:
        by_mode[l.payment_mode] = by_mode.get(l.payment_mode, 0) + l.net_pay
    r = report_ctx(db, user, "payroll", {"company": str(cid or ""), "month": f"{y}-{m:02d}"})
    return render(request, "payroll.html", user, y=y, m=m, run=run, lines=lines, totals=totals, by_mode=by_mode,
                  companies=comps, cid=cid, company=db.get(Company, cid) if cid else None, runs=runs,
                  warn_count=sum(1 for l in lines if l.warnings), r=r)


@app.post("/payroll/process")
def payroll_process(month: str = Form(...), company_id: int = Form(...), user=Depends(page_user),
                    db: Session = Depends(get_db)):
    need(user, "payroll.process")
    check_company(user, company_id)
    y, m = parse_month(month)
    try:
        run = run_payroll(db, company_id, y, m, user)
    except (PayrollLocked, ValueError) as exc:
        return go(_payroll_url(company_id, y, m), err=str(exc))
    err = ("Skipped: " + "; ".join(run.errors)[:1200]) if run.errors else ""
    if run.errors:
        log_error("PAYROLL", f"company {company_id} {y}-{m:02d}: {len(run.errors)} employees skipped",
                  detail="\n".join(run.errors), username=user["username"])
    return go(_payroll_url(company_id, y, m), msg=f"Draft payroll ready for {len(run.lines)} employees", err=err)


@app.post("/payroll/finalize")
def payroll_finalize(month: str = Form(...), company_id: int = Form(...), confirm: str = Form(""),
                     user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "payroll.finalize")
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
    need(user, "payroll.view", "reports.export")
    run = _run_for(db, user, run_id)
    audit(db, user, "PAYROLL_EXPORT", f"{run.company.code} {run.year}-{run.month:02d} salary sheet")
    return Response(salary_sheet(run), media_type=XLSX, headers={
        "Content-Disposition": f"attachment; filename=salary-{run.company.code}-{run.year}-{run.month:02d}.xlsx"})


@app.get("/payroll/run/{run_id}/payslips.pdf")
def payroll_pdf(run_id: int, employee_code: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "payroll.view")
    run = _run_for(db, user, run_id)
    lines = [l for l in run.lines if not employee_code or l.employee_code == employee_code.strip()]
    audit(db, user, "PAYSLIP_EXPORT", f"{run.company.code} {run.year}-{run.month:02d} {employee_code or 'all'}")
    return Response(payslips_pdf(run, lines), media_type="application/pdf", headers={
        "Content-Disposition": f"inline; filename=payslips-{run.company.code}-{run.year}-{run.month:02d}.pdf"})


@app.get("/payroll/combined.xlsx")
def payroll_combined(month: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    """Head Office: every company's salary sheet for the month in one workbook, with a summary."""
    need(user, "payroll.view", "reports.export")
    y, m = parse_month(month)
    ids = [c.id for c in visible_companies(db, user)]
    runs = db.scalars(select(PayrollRun).where(PayrollRun.year == y, PayrollRun.month == m,
                                               PayrollRun.company_id.in_(ids or [0]))).all()
    runs = sorted(runs, key=lambda r: r.company.code)
    if not runs:
        raise HTTPException(404, "No payroll processed for that month yet")
    audit(db, user, "PAYROLL_EXPORT", f"combined {y}-{m:02d}")
    return Response(combined_salary(runs, y, m), media_type=XLSX,
                    headers={"Content-Disposition": f"attachment; filename=HO-salary-all-companies-{y}-{m:02d}.xlsx"})


# ---------------------------------------------------------------- pay components (Admin)
@app.get("/payroll/components", response_class=HTMLResponse)
def components_page(request: Request, edit: int | None = None, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "payroll.edit")
    comps = db.scalars(select(PayComponent).order_by(PayComponent.sort, PayComponent.id)).all()
    assigned = db.execute(select(EmployeeComponent, Employee).join(Employee, Employee.id == EmployeeComponent.employee_id)
                          .order_by(EmployeeComponent.component_id, Employee.employee_code)).all()
    by_comp: dict[int, list] = {}
    for ec, e in assigned:
        by_comp.setdefault(ec.component_id, []).append((ec, e))
    return render(request, "pay_components.html", user, comps=comps, by_comp=by_comp, calcs=comp_svc.CALCS,
                  e=db.get(PayComponent, edit) if edit else None, companies=visible_companies(db, user),
                  depts=db.scalars(select(Department).order_by(Department.name)).all(),
                  company_names={c.id: c.code for c in db.scalars(select(Company))},
                  dept_names={d.id: d.name for d in db.scalars(select(Department))})


@app.post("/payroll/components/save")
def component_save(comp_id: str = Form(""), code: str = Form(...), name: str = Form(...), kind: str = Form("ADD"),
                   calc: str = Form("FIXED"), value: str = Form("0"), prorate: str = Form(""), company_id: int = Form(0),
                   department_id: int = Form(0), assigned_only: str = Form(""), active: str = Form(""),
                   sort: int = Form(0), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "payroll.edit")
    code = "".join(ch for ch in code.strip().upper() if ch.isalnum() or ch == "_")[:20]
    if not code or not name.strip() or kind not in ("ADD", "DED") or calc not in comp_svc.CALCS:
        return go("/payroll/components", err="Code, name, type and calculation are needed")
    try:
        v = dec(value, "Value")
    except ValueError as exc:
        return go("/payroll/components", err=str(exc))
    c = db.get(PayComponent, int(comp_id)) if comp_id else PayComponent()
    if db.scalar(select(PayComponent).where(PayComponent.code == code, PayComponent.id != (c.id or 0))):
        return go("/payroll/components", err=f"Component {code} already exists")
    c.code, c.name, c.kind, c.calc, c.value = code, name.strip()[:80], kind, calc, v
    c.prorate, c.assigned_only, c.active, c.sort = bool(prorate), bool(assigned_only), bool(active), sort
    c.company_id, c.department_id = company_id or 0, department_id or 0
    if not comp_id:
        db.add(c)
    audit(db, user, "PAY_COMPONENT_SAVE", f"{code} {kind} {calc} {v} company={company_id} dept={department_id} "
                                          f"assigned_only={bool(assigned_only)} active={bool(active)}", commit=False)
    db.commit()
    return go("/payroll/components", msg=f"{name} saved - process the payroll again to apply it")


@app.post("/payroll/components/assign")
def component_assign(component_id: int = Form(...), employee_codes: str = Form(""), value: str = Form(""),
                     mode: str = Form("value"), user=Depends(page_user), db: Session = Depends(get_db)):
    """Give listed employees their own value, include them (assigned-only components) or switch it off for them."""
    need(user, "payroll.edit")
    c = db.get(PayComponent, component_id)
    codes = [x.strip() for x in employee_codes.replace(",", "\n").splitlines() if x.strip()]
    if not c or not codes:
        return go("/payroll/components", err="Choose a component and give employee codes")
    try:
        v = dec(value, "Value") if value.strip() else None
    except ValueError as exc:
        return go("/payroll/components", err=str(exc))
    stmt = select(Employee).where(or_(Employee.employee_code.in_(codes), Employee.card_no.in_(codes)))
    if user.get("company_id"):
        stmt = stmt.where(Employee.company_id == user["company_id"])
    emps = db.scalars(stmt).all()
    found = {e.employee_code for e in emps} | {e.card_no for e in emps}
    missing = [x for x in codes if x not in found]
    for e in emps:
        ec = db.scalar(select(EmployeeComponent).where(EmployeeComponent.employee_id == e.id,
                                                       EmployeeComponent.component_id == c.id))
        if mode == "remove":
            if ec:
                db.delete(ec)
            continue
        if not ec:
            ec = EmployeeComponent(employee_id=e.id, component_id=c.id)
            db.add(ec)
        ec.excluded = mode == "exclude"
        ec.value = None if mode == "exclude" else v
    audit(db, user, "PAY_COMPONENT_ASSIGN", f"{c.code} {mode} {v} -> {len(emps)} employees", commit=False)
    db.commit()
    return go("/payroll/components", msg=f"{c.name}: {len(emps)} employee(s) updated",
              err=("Not found: " + ", ".join(missing[:20])) if missing else "")


# ---------------------------------------------------------------- edit one payroll line (Admin)
def _ym(month: str) -> tuple[int, int]:
    return parse_month(month)


@app.get("/payroll/line", response_class=HTMLResponse)
def payroll_line_page(request: Request, company: int, month: str, emp: int, user=Depends(page_user),
                      db: Session = Depends(get_db)):
    need(user, "payroll.edit")
    check_company(user, company)
    y, m = _ym(month)
    e = _emp_or_404(db, user, emp)
    run = get_run(db, company, y, m)
    line = next((l for l in run.lines if l.employee_id == e.id), None) if run else None
    a = db.scalar(select(PayrollAdjustment).where(PayrollAdjustment.employee_id == e.id, PayrollAdjustment.year == y,
                                                  PayrollAdjustment.month == m))
    items = json.loads(a.items_json or "[]") if a else []
    ov = json.loads(a.overrides_json or "{}") if a else {}
    line_items = json.loads(line.items_json or "[]") if line and line.items_json else []
    return render(request, "payroll_line.html", user, e=e, run=run, line=line, a=a, items=items, ov=ov,
                  line_items=line_items, y=y, m=m, mval=f"{y:04d}-{m:02d}", cid=company,
                  comps=comp_svc.for_employee(e, *comp_svc.load_all(db)), calcs=comp_svc.CALCS)


@app.post("/payroll/line")
async def payroll_line_save(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "payroll.edit")
    f = await request.form()
    cid, emp_id = int(f.get("company_id")), int(f.get("employee_id"))
    check_company(user, cid)
    y, m = _ym(str(f.get("month")))
    e = _emp_or_404(db, user, emp_id)
    back = f"/payroll/line?company={cid}&month={y:04d}-{m:02d}&emp={e.id}"
    run = get_run(db, cid, y, m)
    if run and run.status == "FINAL":
        return go(back, err="This payroll is final (locked) - it cannot be edited")
    try:
        a = db.scalar(select(PayrollAdjustment).where(PayrollAdjustment.employee_id == e.id,
                                                      PayrollAdjustment.year == y, PayrollAdjustment.month == m))
        if not a:
            a = PayrollAdjustment(employee_id=e.id, year=y, month=m)
            db.add(a)
        if f.get("reset"):
            a.overrides_json = a.items_json = None
            a.other_addition = a.advance = a.other_deduction = a.tax = Decimal(0)
            a.note = None
        else:
            for k in ("other_addition", "advance", "other_deduction", "tax"):
                setattr(a, k, dec(str(f.get(k) or "0"), k.replace("_", " ")))
            a.note = str(f.get("note") or "").strip()[:250] or None
            ov = {}
            for k in ("present_days", "absent_days", "late_days", "ot_hours"):
                v = str(f.get(k) or "").strip()
                if v:
                    num = dec(v, k.replace("_", " "))
                    if num < 0 or num > (31 if k != "ot_hours" else 400):
                        raise ValueError(f"{k.replace('_', ' ')} {v} is out of range")
                    ov[k] = str(num) if k == "ot_hours" else int(num)
            if ov:
                ov["by"] = user["username"]
            a.overrides_json = json.dumps(ov) if ov else None
            items = []
            for name, kind, amt in zip(f.getlist("item_name"), f.getlist("item_kind"), f.getlist("item_amount")):
                name, amt = str(name).strip(), str(amt).strip()
                if not name and not amt:
                    continue
                if not name or not amt:
                    raise ValueError("each one-off item needs a name and an amount")
                items.append({"name": name[:60], "kind": "DED" if kind == "DED" else "ADD",
                              "amount": str(dec(amt, name))})
            a.items_json = json.dumps(items) if items else None
        a.edited_by = user["username"]
        audit(db, user, "PAYROLL_EDIT", f"{e.employee_code} {y}-{m:02d}: overrides={a.overrides_json} "
                                        f"items={a.items_json} add={a.other_addition} adv={a.advance} "
                                        f"ded={a.other_deduction} tax={a.tax}", commit=False)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return go(back, err=str(exc))
    if run:      # recalculate this company's payroll so the change shows at once
        try:
            run_payroll(db, cid, y, m, user)
        except (PayrollLocked, ValueError) as exc:
            return go(back, err=f"Saved, but re-processing failed: {exc}")
    return go(back, msg="Saved" + (" and payroll recalculated" if run else " - process the payroll to apply it"))


@app.post("/payroll/adjustments")
async def payroll_adjustments(month: str = Form(...), company_id: int = Form(...), file: UploadFile = File(...),
                              user=Depends(page_user), db: Session = Depends(get_db)):
    """Excel: employee_code, other_addition, advance, other_deduction, tax, note (one company)."""
    need(user, "payroll.process")
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
    need(user, "payroll.process")
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
    del year
    lat, lon, radius = geofence(db)
    sp = db.scalars(select(SpecialHoliday).where(SpecialHoliday.day >= date(now_local().year - 1, 1, 1))
                    .order_by(SpecialHoliday.day.desc())).all()
    if user.get("company_id"):
        sp = [h for h in sp if h.scope != "COMPANY" or h.scope_value == str(user["company_id"])]
    return render(request, "settings.html", user, policy=policy_rows(db, pc), policy_company=pc, special=sp,
                  depts=db.scalars(select(Department).order_by(Department.name)).all(),
                  geo_mode=get_setting(db, "geofence_mode", default="STRICT") or "STRICT",
                  r=report_ctx(db, user, "holidays", {}), weekend=sorted(weekend_set(db)), lat=lat, lon=lon, radius=radius,
                  companies=visible_companies(db, user),
                  all_companies=db.scalars(select(Company).order_by(Company.code)).all(),
                  data_dir=str(DATA_DIR), db_kind="SQLite" if settings.db_url.startswith("sqlite") else "SQL Server")


@app.post("/settings/policy")
async def settings_policy(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "policy.edit")
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
    need(user, "company.manage")
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
    mode = "RECORD" if f.get("geofence_mode") == "RECORD" else "STRICT"
    set_setting(db, "geofence_mode", mode, commit=False)
    audit(db, user, "SETTINGS_SAVE", f"weekend={weekend} office={lat},{lon} r={radius} outside={mode}", commit=False)
    db.commit()
    return go("/settings", msg="Saved - process the month again if the weekly off day changed")


@app.post("/settings/companies")
def company_save(company_id: str = Form(""), code: str = Form(...), name: str = Form(...), address: str = Form(""),
                 active: str = Form(""), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "company.manage")
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
    need(user, "holidays.manage")
    d = date.fromisoformat(day)
    if db.scalar(select(Holiday).where(Holiday.day == d)):
        return go("/settings", err="That date is already a holiday")
    db.add(Holiday(day=d, name=name.strip()))
    audit(db, user, "HOLIDAY_ADD", f"{d} {name}", commit=False)
    db.commit()
    return go("/settings", msg="Holiday added")


@app.post("/settings/special-holidays")
def special_holiday_add(day: str = Form(...), day_to: str = Form(""), name: str = Form(...), scope: str = Form(...),
                        company_id: str = Form(""), department_id: str = Form(""), employee_codes: str = Form(""),
                        user=Depends(page_user), db: Session = Depends(get_db)):
    """Holiday for only some staff (one company, one department or listed employees), one day or a range."""
    need(user, "holidays.manage")
    try:
        d1 = date.fromisoformat(day)
        d2 = date.fromisoformat(day_to) if day_to else d1
    except ValueError:
        return go("/settings#special", err="Give a valid date")
    if d2 < d1 or (d2 - d1).days > 60:
        return go("/settings#special", err="Date range must be 1-61 days")
    if scope == "COMPANY":
        cid = company_filter(user, company_id)
        if not cid:
            return go("/settings#special", err="Choose a company")
        value = str(cid)
    elif scope == "DEPARTMENT":
        if not department_id.isdigit():
            return go("/settings#special", err="Choose a department")
        value = department_id
    elif scope == "EMPLOYEES":
        codes = [c.strip() for c in employee_codes.replace("\n", ",").split(",") if c.strip()]
        found = {e.employee_code for e in db.scalars(select(Employee).where(
            or_(Employee.employee_code.in_(codes), Employee.card_no.in_(codes))))} if codes else set()
        if not codes:
            return go("/settings#special", err="Give employee codes")
        cards = {e.card_no for e in db.scalars(select(Employee).where(Employee.card_no.in_(codes)))}
        missing = [c for c in codes if c not in found and c not in cards]
        if missing:
            return go("/settings#special", err="Unknown employee codes: " + ", ".join(missing[:20]))
        value = ",".join(codes)
    else:
        return go("/settings#special", err="Choose who the holiday is for")
    d = d1
    while d <= d2:
        db.add(SpecialHoliday(day=d, name=name.strip()[:120], scope=scope, scope_value=value))
        d += timedelta(days=1)
    audit(db, user, "SPECIAL_HOLIDAY_ADD", f"{d1}..{d2} {name} {scope}={value[:200]}", commit=False)
    db.commit()
    return go("/settings#special", msg="Holiday saved - process the month again to apply it")


@app.post("/settings/special-holidays/{hid}/delete")
def special_holiday_delete(hid: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "holidays.manage")
    h = db.get(SpecialHoliday, hid)
    if h:
        if user.get("company_id") and h.scope == "COMPANY" and h.scope_value != str(user["company_id"]):
            raise HTTPException(403, "That belongs to another company")
        audit(db, user, "SPECIAL_HOLIDAY_DELETE", f"{h.day} {h.name} {h.scope}", commit=False)
        db.delete(h)
        db.commit()
    return go("/settings#special", msg="Holiday removed")


@app.post("/settings/holidays/{hid}/delete")
def holiday_delete(hid: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "holidays.manage")
    h = db.get(Holiday, hid)
    if h:
        audit(db, user, "HOLIDAY_DELETE", f"{h.day} {h.name}", commit=False)
        db.delete(h)
        db.commit()
    return go("/settings", msg="Holiday removed")


@app.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "backup.manage")
    return render(request, "backup.html", user, backups=backups.list_backups(db), folder=str(backups.backup_dir(db)),
                  custom_dir=get_setting(db, "backup_dir", default="") or "",
                  at=get_setting(db, "backup_time", default="02:00"), auto=get_setting(db, "backup_auto", default="1"),
                  keep=get_setting(db, "backup_keep_days", default=str(settings.backup_keep_days)),
                  last=get_setting(db, "backup_last_auto", default=""), data_dir=str(DATA_DIR),
                  sqlite=settings.db_url.startswith("sqlite"))


@app.post("/backup/create")
def backup_create(user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "backup.manage")
    try:
        path = backups.create_backup(db, "manual", user["username"])
    except Exception as exc:  # noqa: BLE001
        log_error("BACKUP", f"backup failed: {exc}", username=user["username"], exc=exc)
        return go("/backup", err=f"Backup failed: {exc}")
    audit(db, user, "BACKUP_CREATE", path.name)
    return go("/backup", msg=f"Backup saved: {path}")


@app.post("/backup/settings")
def backup_settings(folder: str = Form(""), at: str = Form("02:00"), keep: int = Form(30), auto: str = Form(""),
                    user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "backup.manage")
    folder = folder.strip()
    if folder:
        try:
            p = Path(folder)
            p.mkdir(parents=True, exist_ok=True)
            test = p / ".laila_write_test"
            test.write_text("ok")
            test.unlink()
        except Exception as exc:  # noqa: BLE001
            return go("/backup", err=f"Cannot use folder {folder}: {exc}")
    try:
        hh, mm = (int(x) for x in at.split(":")[:2])
        assert 0 <= hh < 24 and 0 <= mm < 60
    except (ValueError, AssertionError):
        return go("/backup", err="Time must be like 02:00")
    set_setting(db, "backup_dir", folder, commit=False)
    set_setting(db, "backup_time", f"{hh:02d}:{mm:02d}", commit=False)
    set_setting(db, "backup_keep_days", str(max(1, min(keep, 3650))), commit=False)
    set_setting(db, "backup_auto", "1" if auto else "0", commit=False)
    audit(db, user, "BACKUP_SETTINGS", f"folder={folder or 'default'} at={at} keep={keep} auto={bool(auto)}", commit=False)
    db.commit()
    return go("/backup", msg="Backup settings saved")


@app.get("/backup/download/{name}")
def backup_download(name: str, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "backup.manage")
    try:
        path = backups.safe_path(db, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    audit(db, user, "BACKUP_DOWNLOAD", name)
    return FileResponse(path, media_type="application/zip", filename=name)


@app.post("/backup/upload")
async def backup_upload(file: UploadFile = File(...), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "backup.manage")
    dest = backups.backup_dir(db) / f"{backups.PREFIX}{datetime.now():%Y%m%d_%H%M%S}_uploaded.zip"
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
    try:
        m = backups.validate(dest)
    except ValueError as exc:
        dest.unlink(missing_ok=True)
        log_error("BACKUP", f"uploaded backup {file.filename} refused: {exc}", username=user["username"])
        return go("/backup", err=f"{file.filename}: {exc}")
    audit(db, user, "BACKUP_UPLOAD", f"{file.filename} -> {dest.name}")
    return go("/backup", msg=f"{file.filename} uploaded and checked (made {m.get('created', '?')[:16]}). "
                             "Click Restore next to it to use it.")


@app.post("/backup/restore")
def backup_restore(request: Request, name: str = Form(...), confirm: str = Form(""), user=Depends(page_user),
                   db: Session = Depends(get_db)):
    need(user, "backup.manage")
    if confirm.strip().upper() != "RESTORE":
        return go("/backup", err="Type RESTORE to confirm")
    try:
        path = backups.safe_path(db, name)
        backups.validate(path)
    except ValueError as exc:
        return go("/backup", err=str(exc))
    try:
        safety = backups.create_backup(db, "before_restore", user["username"])
    except Exception as exc:  # noqa: BLE001
        safety = None
        log_error("BACKUP", f"safety backup before restore failed: {exc}", username=user["username"])
    page = render(request, "restarting.html", None, backup_name=name, safety=safety.name if safety else "")
    backups.schedule_restore(path)
    audit(db, user, "BACKUP_RESTORE", f"{name} (safety copy: {safety.name if safety else 'none'})")
    backups.restart_program()
    return page


@app.post("/backup/delete")
def backup_delete(name: str = Form(...), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "backup.manage")
    try:
        backups.safe_path(db, name).unlink()
    except (ValueError, OSError) as exc:
        return go("/backup", err=str(exc))
    audit(db, user, "BACKUP_DELETE", name)
    return go("/backup", msg=f"Deleted {name}")


# ---------------------------------------------------------------- users & audit
@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, role: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "users.manage")
    roles = db.scalars(select(Role).order_by(Role.builtin.desc(), Role.name)).all()
    return render(request, "users.html", user, users=db.scalars(select(User).order_by(User.username)).all(),
                  roles=roles, edit_role=db.scalar(select(Role).where(Role.name == role)) if role else None,
                  companies=db.scalars(select(Company).order_by(Company.code)).all(), perms=PERMS, groups=GROUPS,
                  role_perms={r.name: (set(PERMS) if r.name == "ADMIN" else parse_perms(r.permissions)) for r in roles},
                  counts=dict(db.execute(select(User.role, func.count()).group_by(User.role)).all()))


@app.post("/users/save")
def user_save(username: str = Form(...), role: str = Form(...), password: str = Form(""), active: str = Form(""),
              company_id: str = Form(""), full_name: str = Form(""), user=Depends(page_user),
              db: Session = Depends(get_db)):
    need(user, "users.manage")
    if not db.scalar(select(Role).where(Role.name == role)):
        return go("/users", err="Unknown role")
    if role == "ADMIN" and user["role"] != "ADMIN":
        return go("/users", err="Only an administrator can create administrators")
    name = username.strip()
    if len(name) < 3:
        return go("/users", err="Username needs 3+ characters")
    u = db.scalar(select(User).where(User.username == name))
    if not u:
        if len(password) < 8:
            return go("/users", err="New users need a password of 8+ characters")
        u = User(username=name, password_hash=hash_password(password))
        db.add(u)
    elif password:
        if len(password) < 8:
            return go("/users", err="Password must be 8+ characters")
        u.password_hash = hash_password(password)
    if u.role == "ADMIN" and user["role"] != "ADMIN":
        db.rollback()
        return go("/users", err="Only an administrator can change an administrator")
    if u.username == user["username"] and (role != user["role"] or not active or company_id):
        db.rollback()
        return go("/users", err="You cannot change your own role, company limit or active status")
    u.role, u.active, u.full_name = role, bool(active), full_name.strip() or u.full_name
    u.company_id = int(company_id) if company_id else None
    audit(db, user, "USER_SAVE", f"{u.username} role={role} company={company_id or 'ALL'} active={bool(active)}"
          + (" password changed" if password else ""), commit=False)
    db.commit()
    return go("/users", msg=f"User {u.username} saved")


@app.post("/roles/save")
async def role_save(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "users.manage")
    f = await request.form()
    name = str(f.get("name", "")).strip().upper().replace(" ", "_")[:30]
    if not name or not name.replace("_", "").isalnum():
        return go("/users", err="Role name: letters, numbers and _ only")
    if name == "ADMIN":
        return go("/users", err="ADMIN always has every permission and cannot be changed")
    chosen = [p for p in PERMS if f.get(f"perm_{p}")]
    if user["role"] != "ADMIN":
        extra = [p for p in chosen if p not in user["perms"]]
        if extra:
            return go(f"/users?role={name}", err="You cannot give permissions you do not have: " + ", ".join(extra))
    r = db.scalar(select(Role).where(Role.name == name))
    if not r:
        r = Role(name=name, builtin=False)
        db.add(r)
    r.label = str(f.get("label", "")).strip()[:120] or name.title()
    r.permissions = ",".join(chosen)
    audit(db, user, "ROLE_SAVE", f"{name}: {r.permissions}", commit=False)
    db.commit()
    return go(f"/users?role={name}", msg=f"Role {name} saved ({len(chosen)} permissions)")


@app.post("/roles/{name}/delete")
def role_delete(name: str, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "users.manage")
    r = db.scalar(select(Role).where(Role.name == name))
    if not r or r.builtin:
        return go("/users", err="Built-in roles cannot be removed")
    if db.scalar(select(func.count(User.id)).where(User.role == name)):
        return go("/users", err="Some users still have this role - give them another role first")
    audit(db, user, "ROLE_DELETE", name, commit=False)
    db.delete(r)
    db.commit()
    return go("/users", msg=f"Role {name} removed")


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "logs.view")
    r = report_ctx(db, user, "audit", dict(request.query_params))
    return render(request, "audit.html", user, r=r, q=request.query_params.get("q", ""))


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
    ctx = {}
    if emp:
        ctx = dict(bal=leave_svc.balances(db, emp), ltypes=[t for t in leave_svc.types(db) if t.self_request and
                                                             (not t.gender or t.gender == emp.gender)],
                   my_leave=db.scalars(select(Leave).where(Leave.employee_id == emp.id)
                                       .order_by(Leave.start_date.desc()).limit(12)).all(),
                   labels=leave_svc.STATUS_LABEL, today=now_local().date(), tab=request.query_params.get("tab", ""))
    return render(request, "mobile.html", None, emp=emp, punches=mobile.today_punches(db, emp) if emp else [], **ctx)


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
        log = mobile.punch(db, emp, latitude, longitude, accuracy, client_ip=request.client.host if request.client else None)
    except mobile.MobileError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    return {"ok": True, "message": f"Punch recorded at {log.punch_time:%I:%M %p}", "time": f"{log.punch_time:%H:%M}",
            "location": f"{log.latitude:.6f}, {log.longitude:.6f} (±{log.accuracy_m} m) · {log.note}"}


@app.post("/m/leave")
async def mobile_leave(request: Request, db: Session = Depends(get_db)):
    """Employee requests leave from the phone; it goes to the approval workflow."""
    emp = mobile_employee(request, db)
    if not emp or emp.employment_status != "ACTIVE":
        return go("/m", err="Please log in again")
    f = await request.form()
    lt = db.scalar(select(LeaveType).where(LeaveType.code == str(f.get("leave_type") or "")))
    try:
        if not lt or not lt.self_request:
            raise ValueError("this leave type cannot be requested here - ask HR")
        s, e = date.fromisoformat(str(f.get("start_date"))), date.fromisoformat(str(f.get("end_date")))
        if e < now_local().date() - timedelta(days=30):
            raise ValueError("leave more than 30 days in the past must be entered by HR")
        reason = str(f.get("reason") or "").strip()
        if not reason:
            raise ValueError("write a reason")
        has_doc = bool(getattr(f.get("document"), "filename", ""))
        days = leave_svc.validate(db, emp, lt, s, e, has_doc)
        stored = await _leave_document(f, emp)
    except ValueError as exc:
        return go("/m?tab=leave", err=str(exc)[:300])
    status = leave_svc.first_status(db, emp)
    db.add(Leave(employee_id=emp.id, start_date=s, end_date=e, leave_type=lt.code, paid=lt.paid, days=days,
                 status=status, source="SELF", reason=reason[:300], note=reason[:250], document=stored,
                 requested_by=emp.employee_code, requested_at=now_local()))
    audit(db, f"mobile:{emp.employee_code}", "LEAVE_REQUEST", f"{lt.code} {s}..{e} ({days} days) -> {status}",
          commit=False)
    db.commit()
    return go("/m?tab=leave", msg=f"Leave request sent - {leave_svc.STATUS_LABEL[status].lower()}")


@app.post("/m/leave/{leave_id}/cancel")
def mobile_leave_cancel(request: Request, leave_id: int, db: Session = Depends(get_db)):
    emp = mobile_employee(request, db)
    lv = db.get(Leave, leave_id)
    if not emp or not lv or lv.employee_id != emp.id or lv.status not in leave_svc.OPEN:
        return go("/m?tab=leave", err="This request cannot be cancelled")
    lv.status = "CANCELLED"
    audit(db, f"mobile:{emp.employee_code}", "LEAVE_CANCEL", f"{lv.leave_type} {lv.start_date}..{lv.end_date}",
          commit=False)
    db.commit()
    return go("/m?tab=leave", msg="Request cancelled")

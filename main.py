import io
import re
import json
from calendar import month_abbr, month_name
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse, PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, or_, delete
from sqlalchemy.orm import Session

from config import settings, DATA_DIR
from database import Base, engine, get_db, SessionLocal
from integration.csv_import import insert_punches, parse_dt
from integration.punch_import import read_punch_file, ImportProblem
from integration.employee_import import import_employees, template_bytes
from integration.zk_adms import options_response
from models import (Company, Department, Employee, Shift, Device, AttendanceLog, DailyAttendance, Holiday, Leave,
                    PayrollAdjustment, PayrollRun, PayrollLine, User, ErrorLog, Role, EmployeeDocument,
                    LeaveType, SpecialHoliday, PayComponent, EmployeeComponent, EmployeeAsset, EmployeeSeparation,
                    AuditLog, EmployeeChange)
from payroll.engine import split_gross
from reports.excel import (salary_sheet, attendance_month, attendance_day, punches_sheet, first_last_sheet,
                           combined_salary)
from reports.payslip import payslips_pdf
from security import (hash_password, verify_password, create_token, decode_token, page_user,
                      LoginRequired, COOKIE)
from services.permissions import PERMS, GROUPS, BUILTIN_ROLES, ROLE_MIGRATIONS, SUPER_PERMS, parse as parse_perms
from services import mobile, devices, maintenance, migrate, branding, backup as backups
from services import leave as leave_svc, documents as docs, components as comp_svc, disbursement as disb
from services.datadelete import (wipe_employee as _wipe_employee, unlink_photo as _unlink_photo,
                                 unlink_punch_photo as _unlink_punch_photo)
from services import datadelete as dd  # noqa: F401  (used below)
from services import assets as asset_svc, letters as letter_svc, changes as change_svc
from services.errors import log_error
from services.photos import save_photo, save_signature, photo_path, bulk_photos
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
from timeutil import now_local, parse_day
from timeutil import parse_month as _parse_month

BASE_DIR = Path(__file__).resolve().parent
MOBILE_COOKIE = "laila_mobile"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OPEN_PATHS = ("/setup", "/static", "/iclock", "/health", "/m", "/brand", "/https/ca")

Base.metadata.create_all(bind=engine)
migrate.upgrade(engine, Base.metadata)

app = FastAPI(title=settings.app_name, version="3.0.0", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
from version import VERSION, BUILT_ON
templates.env.globals.update(settings=settings, month_name=month_name, brand=branding.BRAND, app_version=VERSION,
                             built_on=BUILT_ON)
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
        raise HTTPException(403, "Your role does not allow this: " + ", ".join(PERMS.get(p, SUPER_PERMS.get(p, p))
                                                                              for p in missing))


def render(request: Request, name: str, user: dict | None, **ctx) -> HTMLResponse:
    ctx.update(request=request, user=user, msg=request.query_params.get("msg", ""),
               err=request.query_params.get("err", ""))
    if user and user.get("must_change") and name != "me_password.html":
        return go("/me/password")      # the page explains why
    if user and "salary.edit" in user["perms"] and "increments_due_count" not in ctx:
        ctx["increments_due_count"] = _due_count(user)
    if user and {"leave.approve_dept", "leave.approve_final"} & user["perms"] and "pending_leave_count" not in ctx:
        try:
            with SessionLocal() as s:
                ctx["pending_leave_count"] = len(leave_svc.pending_for(s, user, user.get("company_id")))
        except Exception:  # noqa: BLE001 - the badge must never break a page
            ctx["pending_leave_count"] = 0
    return templates.TemplateResponse(request, name, ctx)


_due_cache: dict = {}


def _due_count(user) -> int:
    """Increments due in the next 30 days (for the menu badge), refreshed at most once a minute."""
    import time as _t
    key = user.get("company_id") or 0
    hit = _due_cache.get(key)
    if hit and _t.time() - hit[0] < 60:
        return hit[1]
    try:
        with SessionLocal() as s:
            comps = [c.id for c in s.scalars(select(Company))] if not key else [key]
            n = sum(1 for x in change_svc.due_list(s, 30, comps) if not x["pending"])
    except Exception:  # noqa: BLE001 - the badge must never break a page
        n = 0
    _due_cache[key] = (_t.time(), n)
    return n


def dec(value: str, field: str) -> Decimal:
    try:
        return Decimal((value or "0").replace(",", "").strip() or "0")
    except InvalidOperation:
        raise ValueError(f"{field}: not a number")


GROUP_COMPANIES = [("BDL", "BANDO DESIGN"), ("LAUNDRY", "Laundry Factory"), ("LSL", "LAILA STYLES")]
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def weekly_off_from_form(f, prefix: str) -> str:
    """Form has <prefix>_mode = DEFAULT / DAYS / NONE and <prefix>_d0..d6 tick boxes."""
    mode = str(f.get(f"{prefix}_mode") or "DEFAULT")
    if mode == "NONE":
        return "NONE"
    if mode == "DAYS":
        days = [str(d) for d in range(7) if f.get(f"{prefix}_d{d}")]
        if not days:
            raise ValueError("tick at least one weekly off day, or choose 'no weekly off'")
        return ",".join(days)
    return ""


def weekly_off_text(raw: str | None) -> str:
    raw = (raw or "").strip().upper()
    if not raw:
        return "default"
    if raw == "NONE":
        return "none"
    return ", ".join(DAY_NAMES[int(x)] for x in raw.split(",") if x.isdigit() and int(x) < 7)


templates.env.globals.update(DAY_NAMES=DAY_NAMES, weekly_off_text=weekly_off_text)

_MONTHS = {n.lower(): i for i, n in enumerate(month_name) if n} | {n.lower()[:3]: i for i, n in enumerate(month_name) if n}


def parse_month(value: str | None) -> tuple[int, int]:
    """Accepts 2026-11, 11/2026, Nov 2026, 01/11/2026 ... (see timeutil.parse_month)."""
    try:
        return _parse_month(value)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


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


HTTPS_EXEMPT = ("/iclock", "/health", "/https/ca", "/static")   # machines push over plain HTTP


def force_https(db=None) -> bool:
    if "force_https" not in _state:
        own = db is None
        db = db or SessionLocal()
        try:
            _state["force_https"] = get_setting(db, "force_https", default="0") == "1"
        finally:
            if own:
                db.close()
    return _state["force_https"]


@app.middleware("http")
async def https_redirect(request: Request, call_next):
    """With 'Use HTTPS' on, office PCs opening http://server:port/... are sent to https://server:https_port/...
    ZKTeco machines keep pushing punches over HTTP (they cannot use this certificate)."""
    if (request.url.scheme == "http" and request.headers.get("x-forwarded-proto") != "https"
            and request.method in ("GET", "HEAD") and not request.url.path.startswith(HTTPS_EXEMPT)
            and force_https()):
        host = request.url.hostname or "localhost"
        q = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"https://{host}:{settings.https_port}{request.url.path}{q}", status_code=307)
    return await call_next(request)


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


@app.exception_handler(ValueError)
async def _value_error(request: Request, exc: ValueError):
    """A wrongly typed value (date, number ...) shows a readable message instead of a crash."""
    return await _http_error(request, HTTPException(400, str(exc)))


def ensure_super_admin(db: Session) -> None:
    """There is always a Super Admin: the first (oldest) active administrator, if nobody is ticked."""
    if db.scalar(select(func.count(User.id)).where(User.super_admin == True, User.active == True,  # noqa: E712
                                                   User.role == "ADMIN")):
        return
    first = db.scalar(select(User).where(User.role == "ADMIN", User.active == True).order_by(User.id))  # noqa: E712
    if first:
        first.super_admin = True


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
        ensure_super_admin(db)
        branding.refresh(db)
        leave_svc.seed_types(db)
        if setup_done(db) and get_setting(db, "group_companies_v1", default="") != "1":
            # LAILA GROUP Head Office also handles staff of these companies (all punch at the Head Office)
            have = list(db.scalars(select(Company)))
            for code, name in GROUP_COMPANIES:
                if not any(c.code == code or c.name.strip().lower() == name.lower() for c in have):
                    db.add(Company(code=code, name=name))
            set_setting(db, "group_companies_v1", "1", commit=False)
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
    from services.processing import start_auto_refresh
    start_auto_refresh()
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
        kind = FILTERS[k].kind if k in FILTERS else ("date" if k == "day" else "")
        try:                                   # typed dates (01/01/2026, Nov 2026 ...) -> one form
            if kind == "date":
                p[k] = str(parse_day(p[k]))
            elif kind == "month":
                p[k] = "%04d-%02d" % _parse_month(p[k])
        except ValueError as exc:
            raise HTTPException(400, f"{FILTERS[k].label if k in FILTERS else k}: {exc}")
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
    if "start" in rep.filters and not p.get("start") and not p.get("month") and rid != "separations":  # default period
        p.setdefault("start", str(today.replace(day=1)) if rid in ("attendance", "leaves", "first_last") else str(today))
        p.setdefault("end", str(today))
    if rid == "increment_list":
        p.setdefault("year", str(today.year))
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
    return {"status": "healthy", "app": settings.app_name, "version": VERSION, "time": now_local().isoformat(sep=" ")}


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
    db.add(User(full_name="Administrator", username=username, password_hash=hash_password(password), role="ADMIN",
                super_admin=True))
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
    set_setting(db, "group_companies_v1", "1", commit=False)    # the person chose the companies here
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
    if user is None:
        user = _staff_first_login(db, username.strip(), password)
    elif user.role == "STAFF" and not staff_login_on(db):
        user = None             # staff login switched off by the Admin
    if not user or not verify_password(password, user.password_hash):
        audit(db, username[:80], "LOGIN_FAILED", ip)
        return go("/login", err="Wrong username or password")
    audit(db, user.username, "LOGIN", ip)
    resp = go("/")
    resp.set_cookie(COOKIE, create_token(user.username, user.role), **cookie_kwargs(request, settings.session_hours))
    return resp


def staff_login_on(db) -> bool:
    return (get_setting(db, "staff_login", default="1") or "1") == "1"


def staff_default_password(db) -> str:
    return get_setting(db, "staff_default_password", default="12345678") or "12345678"


def _staff_first_login(db, code: str, password: str):
    """An employee's first login with Employee ID + the default password creates their Staff account
    (they must choose their own password straight away)."""
    if not code or not staff_login_on(db) or password != staff_default_password(db):
        return None
    if db.scalar(select(User).where(User.username == code)):      # account exists (maybe blocked)
        return None
    emp = db.scalar(select(Employee).where(Employee.employee_code == code, Employee.employment_status == "ACTIVE"))
    if not emp:
        return None
    u = User(username=code, full_name=emp.full_name, role="STAFF", employee_id=emp.id, company_id=None,
             password_hash=hash_password(password), must_change=True, active=True)
    db.add(u)
    audit(db, code, "STAFF_ACCOUNT", f"created at first login for {emp.full_name}", commit=False)
    db.commit()
    return u


@app.get("/logout")
def logout():
    resp = go("/login")
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, day: str = "", company: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    """Colourful overview with charts. All numbers follow the user's company limit and the company filter."""
    if "dashboard.view" not in user["perms"] and "self.service" in user["perms"]:
        return go("/me")
    need(user, "dashboard.view")
    from collections import Counter
    from services import charts
    today = now_local().date()
    d = parse_day(day) if day else today
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


def _idcard_design(db) -> dict:
    from services.policy import get_setting
    try:
        return json.loads(get_setting(db, "idcard.design", default="") or "{}")
    except ValueError:
        return {}


AUTH_SIGN_FILE = branding.BRAND_DIR / "auth_signature.png"
LETTER_DOCS = {"APPOINTMENT", "CONFIRMATION", "RELEASE", "EXPERIENCE", "PROMOTION", "INCREMENT"}
TPL_FILES = {"front": branding.BRAND_DIR / "idcard_front.png", "back": branding.BRAND_DIR / "idcard_back.png"}


def _save_template(content: bytes) -> bytes:
    """Card template -> PNG at 300 dpi card size (638 x 1011 px), keeping transparency."""
    from PIL import Image, ImageOps
    if not content:
        raise ValueError("empty file")
    if len(content) > 25 * 1024 * 1024:
        raise ValueError("larger than 25 MB")
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(content)))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("not an image - upload a PNG (or JPG)") from exc
    if im.width > im.height * 1.05:
        raise ValueError(f"the template is landscape ({im.width} x {im.height} px) - cards are portrait "
                         "54 x 85.6 mm, e.g. 638 x 1011 px")
    im = im.convert("RGBA").resize((638, 1011), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


@app.get("/idcards/design", response_class=HTMLResponse)
def idcard_design_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "company.manage")
    from services import idcard
    d = idcard.design_of({"design": _idcard_design(db)})
    sample = db.scalar(select(Employee).where(Employee.employment_status == "ACTIVE")
                       .order_by(Employee.photo.is_(None), Employee.employee_code).limit(1))
    return render(request, "idcard_design.html", user, d=d, fields=idcard.FIELDS, sample=sample,
                  tpl={k: (int(p.stat().st_mtime) if p.exists() else 0) for k, p in TPL_FILES.items()},
                  auth_sign=AUTH_SIGN_FILE.exists(), v=int(AUTH_SIGN_FILE.stat().st_mtime) if AUTH_SIGN_FILE.exists() else 0)


@app.post("/idcards/design")
async def idcard_design_save(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "company.manage")
    from services import idcard
    f = await request.form()
    if f.get("reset"):
        set_setting(db, "idcard.design", "{}")
        return go("/idcards/design", msg="ID card design set back to the standard")
    def order(prefix):      # ticked fields, sorted by their order number
        keys = [k for k in idcard.FIELDS if f.get(f"{prefix}_{k}")]
        return sorted(keys, key=lambda k: float(str(f.get(f"{prefix}o_{k}") or 99) or 99))
    color = lambda k, dflt: (str(f.get(k) or dflt).strip() if re.fullmatch(r"#[0-9a-fA-F]{6}", str(f.get(k) or "").strip())  # noqa: E731
                             else dflt)
    d = {"style": f.get("style") if f.get("style") in ("CLASSIC", "LABELS", "LAILA", "TEMPLATE") else "LAILA",
         "header_color": color("header_color", "#E8332F"), "accent_color": color("accent_color", "#00a651"),
         "text_color": color("text_color", "#1f2733"), "title": str(f.get("title") or "").strip()[:40] or "EMPLOYEE IDENTITY CARD",
         "front_fields": order("ff"), "back_fields": order("bf"),
         "labels": {k: str(f.get(f"lab_{k}") or "").strip()[:20] or v for k, v in idcard.FIELDS.items()},
         "show_photo": bool(f.get("show_photo")), "show_holder_sign": bool(f.get("show_holder_sign")),
         "show_auth_sign": bool(f.get("show_auth_sign")), "show_qr": bool(f.get("show_qr")),
         "show_address": bool(f.get("show_address")),
         "holder_sign_label": str(f.get("holder_sign_label") or "").strip()[:30] or "Card holder Signature",
         "auth_sign_label": str(f.get("auth_sign_label") or "").strip()[:30] or "Authorized Signature",
         "back_note": str(f.get("back_note") or "").strip()[:160]}
    if not d["front_fields"]:
        return go("/idcards/design", err="Tick at least one field for the front")
    up = f.get("auth_sign")
    if up is not None and getattr(up, "filename", ""):
        from services.photos import clean_signature
        try:
            branding.BRAND_DIR.mkdir(parents=True, exist_ok=True)
            AUTH_SIGN_FILE.write_bytes(clean_signature(await up.read()))
        except ValueError as exc:
            return go("/idcards/design", err=f"Authorized signature: {exc}")
    if f.get("remove_auth_sign") and AUTH_SIGN_FILE.exists():
        AUTH_SIGN_FILE.unlink()
    for side, path in TPL_FILES.items():
        up = f.get(f"tpl_{side}")
        if up is not None and getattr(up, "filename", ""):
            try:
                branding.BRAND_DIR.mkdir(parents=True, exist_ok=True)
                path.write_bytes(_save_template(await up.read()))
            except ValueError as exc:
                return go("/idcards/design", err=f"{side.title()} template: {exc}")
            if side == "front":
                d["style"] = "TEMPLATE"      # a new front template switches the layout to it
        if f.get(f"remove_tpl_{side}") and path.exists():
            path.unlink()
    if d["style"] == "TEMPLATE" and not TPL_FILES["front"].exists():
        return go("/idcards/design", err="Upload the front template (PNG) first, or choose another layout")
    for k in ("photo_x", "photo_y", "photo_w", "photo_h", "rows_x", "rows_y", "font_size", "row_gap", "label_w",
              "sign_y", "sign_x1", "sign_x2", "sign_w", "back_rows_y", "qr_y", "qr_size"):
        try:
            v = float(str(f.get(k) or idcard.DEFAULT_DESIGN[k]))
        except ValueError:
            return go("/idcards/design", err=f"{k.replace('_', ' ')}: give a number in mm")
        if not 0 <= v <= 86:
            return go("/idcards/design", err=f"{k.replace('_', ' ')} must be 0-86 mm")
        d[k] = v
    for k in ("photo_border", "print_labels", "sign_lines", "back_rows"):
        d[k] = bool(f.get(k))
    set_setting(db, "idcard.design", json.dumps(d))
    audit(db, user, "IDCARD_DESIGN", f"style {d['style']}, front {','.join(d['front_fields'])}")
    return go("/idcards/design", msg="ID card design saved")


@app.get("/idcards/template-{side}.png")
def idcard_template_file(side: str, user=Depends(page_user)):
    need(user, "employees.view")
    path = TPL_FILES.get(side)
    if not path or not path.exists():
        raise HTTPException(404, "No template")
    return FileResponse(path, media_type="image/png")


@app.get("/idcards/auth-signature.png")
def idcard_auth_sign(user=Depends(page_user)):
    need(user, "employees.view")
    if not AUTH_SIGN_FILE.exists():
        raise HTTPException(404, "No authorized signature")
    return FileResponse(AUTH_SIGN_FILE, media_type="image/png")


@app.get("/idcards", response_class=HTMLResponse)
def idcards_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.view")
    from services.policy import get_setting
    return render(request, "idcards.html", user, companies=visible_companies(db, user),
                  depts=db.scalars(select(Department).order_by(Department.name)).all(),
                  signatory=get_setting(db, "idcard.signatory", default="Head of HR & Admin") or "",
                  today=now_local().date())


def _idcard_staff(db, user, company, department, codes, status, missing_photo) -> list[Employee]:
    stmt = select(Employee)
    cid = company_filter(user, company)
    if cid:
        stmt = stmt.where(Employee.company_id == cid)
    if department.isdigit():
        stmt = stmt.where(Employee.department_id == int(department))
    code_list = [c.strip() for c in codes.replace(",", "\n").replace(" ", "\n").splitlines() if c.strip()]
    if code_list:
        stmt = stmt.where(or_(Employee.employee_code.in_(code_list), Employee.card_no.in_(code_list)))
    if status in ("ACTIVE", "INACTIVE"):
        stmt = stmt.where(Employee.employment_status == status)
    emps = list(db.scalars(stmt))
    if missing_photo == "skip":
        emps = [e for e in emps if photo_path(e.photo)]
    if code_list:      # keep the typed order
        order = {c: i for i, c in enumerate(code_list)}
        emps.sort(key=lambda e: order.get(e.employee_code, order.get(e.card_no, 1e9)))
    else:
        emps.sort(key=lambda e: ((e.company.code if e.company else ""), (e.department.name if e.department else ""),
                                 e.employee_code))
    return emps


@app.get("/idcards/data.{fmt}")
def idcards_data(fmt: str, company: str = "", department: str = "", codes: str = "", status: str = "ACTIVE",
                 missing_photo: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    """ID card data for your own design (CorelDRAW / Photoshop / ID card software print merge):
    xlsx or csv = the data; zip = data + photos/<ID>.jpg + signatures/<ID>.png + authorized_signature.png."""
    need(user, "employees.view", "reports.export")
    import csv
    import zipfile
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    if fmt not in ("xlsx", "csv", "zip"):
        raise HTTPException(404, "Unknown format")
    emps = _idcard_staff(db, user, company, department, codes, status, missing_photo)
    if not emps:
        return go("/idcards", err="No employees match - check the company, department or codes")
    head = ["SL", "ID No", "Name", "Designation", "Department", "Section", "Company", "Company code", "DOJ",
            "Blood Group", "Mobile", "Address", "Emergency contact", "Father", "Date of birth", "NID", "Card no",
            "Photo", "Signature"]
    rows = []
    for i, e in enumerate(emps, 1):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", e.employee_code)
        rows.append([i, e.employee_code, e.full_name, e.designation or "", e.department.name if e.department else "",
                     e.section or "", e.company.name if e.company else "", e.company.code if e.company else "",
                     e.joined_on.strftime("%d-%m-%Y") if e.joined_on else "", e.blood_group or "", e.phone or "",
                     e.present_address or "", e.emergency_contact or "", e.father_name or "",
                     e.date_of_birth.strftime("%d-%m-%Y") if e.date_of_birth else "", e.nid or "", e.card_no,
                     f"photos\\{safe}.jpg" if photo_path(e.photo) else "",
                     f"signatures\\{safe}.png" if photo_path(e.signature) else ""])
    wb = Workbook()
    ws = wb.active
    ws.title = "ID card data"
    ws.append(head)
    for cell in ws[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="E8332F")
    for r in rows:
        ws.append(r)
    for col, w in zip("ABCDEFGHIJKLMNOPQRS", (5, 12, 28, 24, 20, 12, 26, 10, 12, 8, 14, 32, 22, 22, 12, 18, 10, 20, 22)):
        ws.column_dimensions[col].width = w
    for r in range(2, ws.max_row + 1):
        for c_ in (2, 11, 16, 17):
            ws.cell(r, c_).number_format = "@"
    xbuf = io.BytesIO()
    wb.save(xbuf)
    cbuf = io.StringIO()
    csv.writer(cbuf).writerows([head] + rows)
    csv_bytes = ("\ufeff" + cbuf.getvalue()).encode("utf-8")      # BOM: Excel / CorelDRAW read Bangla names right
    audit(db, user, "IDCARD_DATA", f"{len(emps)} employees ({fmt})")
    if fmt == "xlsx":
        return Response(xbuf.getvalue(), media_type=XLSX,
                        headers={"Content-Disposition": f"attachment; filename=id-card-data-{len(emps)}.xlsx"})
    if fmt == "csv":
        return Response(csv_bytes, media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=id-card-data-{len(emps)}.csv"})
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("id-card-data.xlsx", xbuf.getvalue())
        z.writestr("id-card-data.csv", csv_bytes)
        for e, r in zip(emps, rows):
            if r[17]:
                z.write(photo_path(e.photo), r[17].replace("\\", "/"))
            if r[18]:
                z.write(photo_path(e.signature), r[18].replace("\\", "/"))
        if AUTH_SIGN_FILE.exists():
            z.write(AUTH_SIGN_FILE, "authorized_signature.png")
        z.writestr("README.txt", "ID card data from SPhere Payroll\r\n\r\n"
                   "1. Unzip this folder (e.g. to C:\\IDCards).\r\n"
                   "2. In CorelDRAW: Tools > Print Merge > Create/Load > choose id-card-data.csv. Add the text fields "
                   "(Name, ID No, Designation ...) onto your design. For pictures, the 'Photo' and 'Signature' columns "
                   "hold the file names (photos\\<ID>.jpg, signatures\\<ID>.png) relative to this folder.\r\n"
                   "3. Other ID card software (BarTender, ID Card Workshop, Photoshop Data Sets): import the Excel/CSV "
                   "and link the Photo / Signature columns as image fields.\r\n"
                   "Signatures are PNG with a transparent background. Photos are JPG, at most 600 px.\r\n")
    return Response(zbuf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=id-card-data-{len(emps)}.zip"})


@app.get("/idcards.pdf")
def idcards_pdf(request: Request, company: str = "", department: str = "", codes: str = "", status: str = "ACTIVE",
                layout: str = "a4", sides: str = "both", valid: str = "", issued: str = "", signatory: str = "",
                missing_photo: str = "", show_nid: str = "", preview: str = "", user=Depends(page_user),
                db: Session = Depends(get_db)):
    """Staff ID cards: whole company / department / chosen employee codes."""
    need(user, "employees.view")
    from services import idcard
    from services.policy import get_setting
    emps = _idcard_staff(db, user, company, department, codes, status, missing_photo)
    if not emps:
        return go("/idcards", err="No employees match - check the company, department or codes")
    if len(emps) > 1500:
        return go("/idcards", err=f"{len(emps)} cards at once is too many - print by company or department")
    try:
        valid_d = parse_day(valid) if valid else None
        issued_d = parse_day(issued) if issued else now_local().date()
    except ValueError:
        return go("/idcards", err="Give valid dates")
    if signatory.strip():
        set_setting(db, "idcard.signatory", signatory.strip()[:60])
    from services.branding import BRAND
    pdf = idcard.cards_pdf(emps, {
        "layout": "card" if layout == "card" else "a4", "sides": sides if sides in ("both", "front", "back") else "both",
        "group": BRAND["name"], "logo": str(branding.logo_path()), "photo": lambda e: photo_path(e.photo),
        "address": BRAND.get("address") or "", "phone": BRAND.get("phone") or "", "issued": issued_d, "valid": valid_d,
        "signatory": signatory.strip() or get_setting(db, "idcard.signatory", default="Authorised signature"),
        "signature": lambda e: photo_path(e.signature), "design": _idcard_design(db),
        "auth_sign": str(AUTH_SIGN_FILE) if AUTH_SIGN_FILE.exists() else None,
        "tpl_front": str(TPL_FILES["front"]) if TPL_FILES["front"].exists() else None,
        "tpl_back": str(TPL_FILES["back"]) if TPL_FILES["back"].exists() else None,
        "show_nid": bool(show_nid), "credit": settings.developer_credit})
    if not preview:
        audit(db, user, "ID_CARDS", f"{len(emps)} cards ({layout}, {sides})")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="id-cards-{len(emps)}.pdf"'})


def _work_hours(db, emp) -> list[dict]:
    """Required (office hours) vs worked hours per month and per year from processed attendance, newest first."""
    from services import hours as hrs
    shifts = hrs.shift_table(db)
    months: dict[tuple[int, int], dict] = {}
    for r in db.scalars(select(DailyAttendance).where(DailyAttendance.employee_id == emp.id)):
        hrs.add(months.setdefault((r.work_date.year, r.work_date.month), hrs.blank()), r, shifts)
    years: dict[int, dict] = {}
    for (y, m), x in sorted(months.items(), reverse=True):
        yr = years.setdefault(y, {"year": y, "months": [], **hrs.blank()})
        for k in hrs.blank():
            yr[k] += x[k]
        x = hrs.finish(dict(x))
        yr["months"].append({"m": m, **x, "leave": x["leave_days"],
                             "avg": round(x["worked_min"] / 60 / x["present"], 1) if x["present"] else 0})
    out = []
    for y in sorted(years, reverse=True):
        yr = hrs.finish(years[y])
        yr.update(leave=yr["leave_days"], avg=round(yr["worked_min"] / 60 / yr["present"], 1) if yr["present"] else 0,
                  hours=yr["worked"], ot_h=yr["ot"])
        for x in yr["months"]:
            x.update(hours=x["worked"], ot_h=x["ot"])
        out.append(yr)
    return out


def _history(db, emp, user) -> list[tuple]:
    """Everything that happened to this employee, newest first (kept after they leave)."""
    from services.assets import STATUS as AST
    ev = []
    if emp.joined_on:
        ev.append((emp.joined_on, "🟢", f"Joined {emp.company.code if emp.company else ''} as {emp.designation or '-'}"
                   f"{' - ' + emp.department.name if emp.department else ''}"))
    if emp.confirmed_on:
        ev.append((emp.confirmed_on, "✅", "Service confirmed"))
    for d in db.scalars(select(EmployeeDocument).where(EmployeeDocument.employee_id == emp.id)):
        if d.doc_type not in LETTER_DOCS or "appointment.letter" in user["perms"]:
            ev.append((d.uploaded_at, "📄", f"{docs.DOC_TYPES.get(d.doc_type, d.doc_type)}: {d.title}"))
    for lv in db.scalars(select(Leave).where(Leave.employee_id == emp.id)):
        ev.append((lv.start_date, "🌴", f"{lv.leave_type} leave {lv.start_date:%d/%m/%Y} - {lv.end_date:%d/%m/%Y} "
                                       f"({leave_svc.STATUS_LABEL.get(lv.status, lv.status).lower()})"))
    for a in db.scalars(select(EmployeeAsset).where(EmployeeAsset.employee_id == emp.id)):
        ev.append((a.issued_on, "💻", f"Received {a.name} {a.serial_no or ''}".strip()))
        if a.returned_on:
            ev.append((a.returned_on, "↩", f"{a.name}: {AST.get(a.status, a.status).lower()}"))
    for ch in db.scalars(select(EmployeeChange).where(EmployeeChange.employee_id == emp.id,
                                                      EmployeeChange.status != "CANCELLED")):
        what = []
        if ch.new_designation and ch.new_designation != ch.old_designation:
            what.append(f"{ch.old_designation or '-'} → {ch.new_designation}")
        if ch.new_grade and ch.new_grade != ch.old_grade:
            what.append(f"grade {ch.old_grade or '-'} → {ch.new_grade}")
        if ch.new_department:
            what.append(f"to {ch.new_department}")
        if ch.new_gross != ch.old_gross and "salary.view" in user["perms"]:
            what.append(f"gross Tk {ch.old_gross:,.0f} → {ch.new_gross:,.0f}")
        ev.append((ch.effective_date, "⬆", f"{change_svc.KINDS.get(ch.kind, ch.kind)}: {', '.join(what)}"
                   + (" (from this date, not applied yet)" if ch.status == "PENDING" else "")))
    for sp in db.scalars(select(EmployeeSeparation).where(EmployeeSeparation.employee_id == emp.id)):
        ev.append((sp.created_at, "📝", f"{letter_svc.SEP_TYPES.get(sp.sep_type, sp.sep_type).title()} recorded, last "
                                       f"working day {sp.last_working_day:%d/%m/%Y}"
                                       + (" (cancelled)" if sp.status == "CANCELLED" else "")))
        if sp.completed_at:
            ev.append((sp.last_working_day, "🔴", "Left the company"
                       + (f" - {sp.reason}" if sp.reason else "")))
    for lg in db.scalars(select(AuditLog).where(AuditLog.detail.like(f"{emp.employee_code}%"),
                                                AuditLog.action.in_(("EMPLOYEE_SAVE", "SALARY_CHANGE", "PROFILE_PDF",
                                                                     "ID_CARDS")))
                         .order_by(AuditLog.id.desc()).limit(25)):
        ev.append((lg.created_at, "✏", f"Profile updated by {lg.username}" if lg.action == "EMPLOYEE_SAVE"
                   else f"{lg.action.replace('_', ' ').title()} by {lg.username}"))
    key = lambda x: x[0] if isinstance(x[0], datetime) else datetime.combine(x[0], time.min)  # noqa: E731
    return sorted(ev, key=key, reverse=True)[:80]


@app.get("/employees/{emp_id}", response_class=HTMLResponse)
def employee_profile(request: Request, emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    """Full profile: photo, personal and job details, this month's attendance, recent punches, leave, payslips."""
    need(user, "employees.view")
    change_svc.apply_due(db)
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
    documents = [d for d in db.scalars(select(EmployeeDocument).where(EmployeeDocument.employee_id == emp.id)
                                       .order_by(EmployeeDocument.doc_type, EmployeeDocument.uploaded_at))
                 if d.doc_type not in LETTER_DOCS or "appointment.letter" in user["perms"]]
    pays = []
    if show_salary:
        pays = db.execute(select(PayrollLine, PayrollRun).join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
                          .where(PayrollLine.employee_id == emp.id)
                          .order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).limit(12)).all()
    return render(request, "employee_profile.html", user, e=emp, summary=summary, month_rows=month_rows, today=today,
                  first_last=fl, leaves=leaves, pays=pays, show_salary=show_salary, documents=documents,
                  doc_types=docs.DOC_TYPES, bal=leave_svc.balances(db, emp), leave_labels=leave_svc.STATUS_LABEL,
                  assets=db.scalars(select(EmployeeAsset).where(EmployeeAsset.employee_id == emp.id)
                                    .order_by(EmployeeAsset.status != "ISSUED", EmployeeAsset.issued_on.desc())).all(),
                  asset_cats=asset_svc.CATEGORIES, asset_status=asset_svc.STATUS, asset_conds=asset_svc.CONDITIONS,
                  sep=(sep := _open_sep(db, emp.id)), sep_check=letter_svc.checklist(sep), sep_types=letter_svc.SEP_TYPES,
                  check_items=letter_svc.CHECKLIST, letter_kinds=letter_svc.KINDS, history=_history(db, emp, user),
                  changes=db.scalars(select(EmployeeChange).where(EmployeeChange.employee_id == emp.id)
                                     .order_by(EmployeeChange.effective_date.desc(), EmployeeChange.id.desc())).all(),
                  change_kinds=change_svc.KINDS, hours=_work_hours(db, emp),
                  service=letter_svc.service_length(emp.joined_on, emp.left_on or now_local().date()),
                  components=comp_svc.for_employee(emp, *comp_svc.load_all(db)) if show_salary else [],
                  calcs=comp_svc.CALCS)


@app.get("/employees/{emp_id}/edit", response_class=HTMLResponse)
def employee_form(request: Request, emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.edit")
    emp = _emp_or_404(db, user, emp_id)
    return render(request, "employee_form.html", user, e=emp, companies=visible_companies(db, user),
                  shifts=db.scalars(select(Shift).order_by(Shift.code)).all(),
                  departments=db.scalars(select(Department).order_by(Department.name)).all())


@app.get("/employees/{emp_id}/signature")
def employee_signature(emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.view")
    emp = _emp_or_404(db, user, emp_id)
    path = photo_path(emp.signature)
    if not path:
        raise HTTPException(404, "No signature")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=60"})


@app.get("/employees/{emp_id}/photo")
def employee_photo(emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.view")
    emp = _emp_or_404(db, user, emp_id)
    path = photo_path(emp.photo)
    if not path:
        return RedirectResponse("/static/img/no-photo.png", status_code=303)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


@app.post("/employees/{emp_id}/assets")
async def employee_asset_add(request: Request, emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.edit")
    emp = _emp_or_404(db, user, emp_id)
    f = await request.form()
    v = lambda k: str(f.get(k) or "").strip()  # noqa: E731
    back = f"/employees/{emp.id}#assets"
    try:
        if not v("name"):
            raise ValueError("Give the item name (e.g. Dell Latitude 5420, Grameenphone SIM)")
        issued = parse_day(v("issued_on")) if v("issued_on") else now_local().date()
        qty = int(v("quantity") or 1)
        if not 1 <= qty <= 10000:
            raise ValueError("Quantity must be 1 or more")
        value = dec(v("value"), "Value")
        serial = v("serial_no")[:120] or None
        if serial:
            other = db.scalar(select(EmployeeAsset).where(EmployeeAsset.serial_no == serial,
                                                          EmployeeAsset.status == "ISSUED"))
            if other:
                raise ValueError(f"Serial {serial} is still with {other.employee.employee_code} "
                                 f"{other.employee.full_name} - take it back there first")
    except ValueError as exc:
        return go(back, err=str(exc))
    db.add(EmployeeAsset(employee_id=emp.id, category=v("category") if v("category") in asset_svc.CATEGORIES else "OTHER",
                         name=v("name")[:150], brand_model=v("brand_model")[:120] or None, serial_no=serial,
                         asset_tag=v("asset_tag")[:60] or None, quantity=qty, value=value, issued_on=issued,
                         issued_by=user["username"], condition_issued=v("condition")[:30] or None,
                         note=v("note")[:300] or None))
    audit(db, user, "ASSET_ISSUE", f"{emp.employee_code}: {v('name')} {serial or ''} x{qty}", commit=False)
    db.commit()
    return go(back, msg=f"{v('name')} given to {emp.full_name}")


@app.post("/employees/{emp_id}/assets/{asset_id}/return")
async def employee_asset_return(request: Request, emp_id: int, asset_id: int, user=Depends(page_user),
                                db: Session = Depends(get_db)):
    need(user, "employees.edit")
    emp = _emp_or_404(db, user, emp_id)
    a = db.get(EmployeeAsset, asset_id)
    back = f"/employees/{emp.id}#assets"
    if not a or a.employee_id != emp.id or a.status != "ISSUED":
        return go(back, err="That item is not with this employee")
    f = await request.form()
    status = str(f.get("status") or "RETURNED")
    try:
        a.returned_on = parse_day(str(f.get("returned_on"))) if f.get("returned_on") else now_local().date()
    except ValueError:
        return go(back, err="Give a valid return date")
    a.status = status if status in ("RETURNED", "LOST", "DAMAGED") else "RETURNED"
    a.returned_to = user["username"]
    a.condition_returned = str(f.get("condition") or "").strip()[:30] or None
    note = str(f.get("note") or "").strip()
    if note:
        a.note = ((a.note + " | ") if a.note else "") + f"Return: {note}"[:200]
    audit(db, user, "ASSET_" + a.status, f"{emp.employee_code}: {a.name} {a.serial_no or ''}", commit=False)
    db.commit()
    return go(back, msg=f"{a.name}: {asset_svc.STATUS[a.status].lower()}")


@app.post("/employees/{emp_id}/assets/{asset_id}/edit")
async def employee_asset_edit(request: Request, emp_id: int, asset_id: int, user=Depends(page_user),
                              db: Session = Depends(get_db)):
    """Admin: correct an asset record (name, model, serial, tag, quantity, value, dates, status, note)."""
    need(user, "assets.manage")
    emp = _emp_or_404(db, user, emp_id)
    a = db.get(EmployeeAsset, asset_id)
    back = f"/employees/{emp.id}#assets"
    if not a or a.employee_id != emp.id:
        return go(back, err="Asset not found")
    f = await request.form()
    v = lambda k: str(f.get(k) or "").strip()  # noqa: E731
    try:
        if not v("name"):
            raise ValueError("Item name is needed")
        a.category = v("category") if v("category") in asset_svc.CATEGORIES else a.category
        a.name, a.brand_model = v("name")[:150], v("brand_model")[:120] or None
        serial = v("serial_no")[:120] or None
        if serial and serial != a.serial_no:
            other = db.scalar(select(EmployeeAsset).where(EmployeeAsset.serial_no == serial, EmployeeAsset.status == "ISSUED",
                                                          EmployeeAsset.id != a.id))
            if other:
                raise ValueError(f"Serial {serial} is with {other.employee.employee_code} {other.employee.full_name}")
        a.serial_no, a.asset_tag = serial, v("asset_tag")[:60] or None
        a.quantity = max(1, int(v("quantity") or 1))
        a.value = dec(v("value"), "Value")
        a.issued_on = parse_day(v("issued_on")) if v("issued_on") else a.issued_on
        a.condition_issued = v("condition")[:30] or None
        st = v("status")
        if st in asset_svc.STATUS:
            a.status = st
            if st == "ISSUED":
                a.returned_on = a.returned_to = a.condition_returned = None
            elif not a.returned_on:
                a.returned_on, a.returned_to = now_local().date(), user["username"]
        a.note = v("note")[:300] or None
    except ValueError as exc:
        db.rollback()
        return go(back, err=str(exc))
    audit(db, user, "ASSET_EDIT", f"{emp.employee_code}: {a.name} {a.serial_no or ''} -> {a.status}", commit=False)
    db.commit()
    return go(back, msg=f"{a.name} updated")


@app.post("/employees/{emp_id}/assets/{asset_id}/transfer")
async def employee_asset_transfer(request: Request, emp_id: int, asset_id: int, user=Depends(page_user),
                                  db: Session = Depends(get_db)):
    """Admin: move an item from this employee to another one (history is kept on both)."""
    need(user, "assets.manage")
    emp = _emp_or_404(db, user, emp_id)
    a = db.get(EmployeeAsset, asset_id)
    back = f"/employees/{emp.id}#assets"
    if not a or a.employee_id != emp.id or a.status != "ISSUED":
        return go(back, err="Only an item that is with this employee can be transferred")
    f = await request.form()
    code = str(f.get("to_code") or "").strip()
    to = db.scalar(select(Employee).where(or_(Employee.employee_code == code, Employee.card_no == code)))
    if not to or to.id == emp.id:
        return go(back, err=f"Choose another employee (code {code or '-'} not found)")
    check_company(user, to.company_id)
    if to.employment_status != "ACTIVE":
        return go(back, err=f"{to.employee_code} {to.full_name} is not active")
    try:
        when = parse_day(str(f.get("date"))) if f.get("date") else now_local().date()
    except ValueError:
        return go(back, err="Give a valid date")
    note = str(f.get("note") or "").strip()[:200]
    a.status, a.returned_on, a.returned_to = "RETURNED", when, user["username"]
    a.condition_returned = str(f.get("condition") or "").strip()[:30] or None
    a.note = ((a.note + " | ") if a.note else "") + f"Transferred to {to.employee_code} {to.full_name}"[:200]
    db.add(EmployeeAsset(employee_id=to.id, category=a.category, name=a.name, brand_model=a.brand_model,
                         serial_no=a.serial_no, asset_tag=a.asset_tag, quantity=a.quantity, value=a.value,
                         issued_on=when, issued_by=user["username"], condition_issued=a.condition_returned,
                         note=(f"From {emp.employee_code} {emp.full_name}" + (f" - {note}" if note else ""))[:300]))
    audit(db, user, "ASSET_TRANSFER", f"{a.name} {a.serial_no or ''}: {emp.employee_code} -> {to.employee_code}",
          commit=False)
    db.commit()
    return go(back, msg=f"{a.name} transferred to {to.employee_code} {to.full_name}")


@app.post("/employees/{emp_id}/assets/{asset_id}/delete")
def employee_asset_delete(emp_id: int, asset_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "assets.manage")
    emp = _emp_or_404(db, user, emp_id)
    a = db.get(EmployeeAsset, asset_id)
    if a and a.employee_id == emp.id:
        audit(db, user, "ASSET_DELETE", f"{emp.employee_code}: {a.name} {a.serial_no or ''} (entered by mistake)",
              commit=False)
        db.delete(a)
        db.commit()
    return go(f"/employees/{emp.id}#assets", msg="Entry removed")


# ---------------------------------------------------------------- increments & promotions (Admin)
@app.post("/employees/{emp_id}/changes")
async def employee_change_add(request: Request, emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "salary.edit")
    emp = _emp_or_404(db, user, emp_id)
    f = await request.form()
    v = lambda k: str(f.get(k) or "").strip()  # noqa: E731
    back = f"/employees/{emp.id}#changes"
    try:
        eff = parse_day(v("effective_date"))
        kind = v("kind") if v("kind") in change_svc.KINDS else "INCREMENT"
        how = v("how") if v("how") in ("AMOUNT", "PERCENT", "GROSS") else "NONE"
        amount = dec(v("value"), "Increase") if how != "NONE" else Decimal(0)
        if amount < 0:
            raise ValueError("The increase cannot be negative")
        old_g = Decimal(emp.gross or 0)
        new_g = change_svc.new_gross(old_g, how, amount) if how != "NONE" else old_g
        new_des, new_grade, new_dept = v("new_designation")[:120], v("new_grade")[:30], v("new_department")[:120]
        cur_dept = emp.department.name if emp.department else ""
        changed = (new_g != old_g or (new_des and new_des != (emp.designation or "")) or
                   (new_grade and new_grade != (emp.grade or "")) or (new_dept and new_dept != cur_dept))
        if not changed:
            raise ValueError("Nothing changes - give a new designation, grade, department or a salary increase")
        if kind == "PROMOTION" and not (new_des and new_des != (emp.designation or "")) and \
                not (new_grade and new_grade != (emp.grade or "")):
            raise ValueError("A promotion needs a new designation or grade")
        if emp.joined_on and eff < emp.joined_on:
            raise ValueError("Effective date is before the joining date")
    except ValueError as exc:
        return go(back, err=str(exc))
    ch = EmployeeChange(employee_id=emp.id, kind=kind, effective_date=eff, old_designation=emp.designation,
                        new_designation=new_des or None, old_grade=emp.grade, new_grade=new_grade or None,
                        old_department=cur_dept or None, new_department=(new_dept if new_dept != cur_dept else None) or None,
                        old_gross=old_g, new_gross=new_g, remarks=v("remarks")[:2000] or None, created_by=user["username"],
                        notify=bool(f.get("notify")))
    db.add(ch)
    db.flush()
    ch.employee = emp
    applied = eff <= now_local().date()
    if applied:
        change_svc.apply(db, ch)
    amt, pct = change_svc.increase(ch)
    audit(db, user, kind, f"{emp.employee_code}: {ch.old_designation} -> {ch.new_designation or ch.old_designation}, "
                          f"gross {old_g} -> {new_g} (+{amt}, {pct}%) from {eff}", commit=False)
    db.commit()
    return go(back, msg=f"{change_svc.KINDS[kind]} saved - " + ("applied now. Re-process this month's payroll if "
                                                                 "it is already made." if applied else
                                                                 f"it will apply automatically on {eff:%d/%m/%Y}."))


@app.get("/increments/due", response_class=HTMLResponse)
def increments_due(request: Request, days: int = 30, user=Depends(page_user), db: Session = Depends(get_db)):
    """Notification list: yearly increments that are due soon or overdue, and changes waiting for their date."""
    need(user, "salary.view")
    days = max(0, min(days, 365))
    rows = change_svc.due_list(db, days, [c.id for c in visible_companies(db, user)])
    return render(request, "increments_due.html", user, rows=rows, days=days, today=now_local().date())


@app.post("/employees/{emp_id}/changes/{change_id}/cancel")
def employee_change_cancel(emp_id: int, change_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "salary.edit")
    emp = _emp_or_404(db, user, emp_id)
    ch = db.get(EmployeeChange, change_id)
    if not ch or ch.employee_id != emp.id or ch.status != "PENDING":
        return go(f"/employees/{emp.id}#changes", err="Only a change that is not applied yet can be cancelled")
    ch.status = "CANCELLED"
    audit(db, user, "CHANGE_CANCEL", f"{emp.employee_code} {ch.kind} {ch.effective_date}", commit=False)
    db.commit()
    return go(f"/employees/{emp.id}#changes", msg="Cancelled")


class _WithSalary:
    """The employee as the letter should show them: new salary parts, everything else unchanged."""
    def __init__(self, e, parts):
        self._e, self._p = e, parts

    def __getattr__(self, k):
        if k in ("gross", "basic", "house_rent", "medical", "transport", "food") and self._p is not None:
            return getattr(self._p, k)
        return getattr(self._e, k)


@app.post("/employees/{emp_id}/changes/{change_id}/letter")
async def employee_change_letter(request: Request, emp_id: int, change_id: int, user=Depends(page_user),
                                 db: Session = Depends(get_db)):
    """Official promotion / increment letter for a recorded change (Admin)."""
    need(user, "employees.view", "appointment.letter")
    from services import appointment as apt
    from payroll.engine import split_gross
    emp = _emp_or_404(db, user, emp_id)
    ch = db.get(EmployeeChange, change_id)
    if not ch or ch.employee_id != emp.id or ch.status == "CANCELLED":
        return go(f"/employees/{emp.id}#changes", err="Change not found")
    f = await request.form()
    try:
        letter_date = parse_day(str(f.get("letter_date"))) if f.get("letter_date") else now_local().date()
    except ValueError:
        letter_date = now_local().date()
    kind = "promotion" if ch.kind == "PROMOTION" else "increment"
    remarks = str(f.get("remarks") or ch.remarks or "").strip()[:1500]
    raise_pay = Decimal(ch.new_gross or 0) != Decimal(ch.old_gross or 0)
    parts = split_gross(Decimal(ch.new_gross), load_policy(db, emp.company_id)) if raise_pay else None
    shown = _WithSalary(emp, parts)
    code = "PRM" if kind == "promotion" else "INC"
    ref = f"{emp.company.code if emp.company else 'HO'}/HR/{code}/{ch.effective_date:%Y}/{emp.employee_code}-{ch.id}"
    base = apt.load_template(db)
    tpl = letter_svc.template(db, kind)
    body = letter_svc.drop_empty_remarks(tpl["body"], remarks)
    tpl["body"] = body if raise_pay else letter_svc.drop_salary_part(body)
    values = apt.values_for(db, shown, letter_date, emp.joined_on, ref, base)
    amt, pct = change_svc.increase(ch)
    money = lambda x: f"{Decimal(x or 0):,.0f}"  # noqa: E731
    values.update(remarks=remarks, effective_date=f"{ch.effective_date:%d %B %Y}",
                  old_designation=ch.old_designation or "-", new_designation=ch.new_designation or ch.old_designation or "-",
                  old_grade=ch.old_grade or "-", new_grade=ch.new_grade or ch.old_grade or "-",
                  new_department=ch.new_department or ch.old_department or "-", designation=ch.new_designation or
                  ch.old_designation or "-", old_gross=money(ch.old_gross), new_gross=money(ch.new_gross),
                  new_gross_words=apt.taka_words(ch.new_gross), increment_amount=money(amt), increment_percent=f"{pct:g}")
    title = letter_svc.KINDS[kind][0]
    pdf = apt.letter_pdf(shown, values, {**base, **tpl}, acceptance=False, title=title)
    if f.get("save_copy"):
        stored, size = docs.save_file(f"emp_{emp.id}", f"{kind}-{emp.employee_code}-{ch.id}.pdf", pdf)
        db.add(EmployeeDocument(employee_id=emp.id, doc_type=kind.upper(), title=f"{title} {ref}", stored_name=stored,
                                original_name=f"{kind}-{emp.employee_code}.pdf", size=size, uploaded_by=user["username"]))
    audit(db, user, f"LETTER_{kind.upper()}", f"{emp.employee_code} ref {ref}", commit=False)
    db.commit()
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{kind}-{emp.employee_code}.pdf"'})


# ---------------------------------------------------------------- leaving the company (separation)
def _open_sep(db, emp_id):
    return db.scalar(select(EmployeeSeparation).where(EmployeeSeparation.employee_id == emp_id,
                                                      EmployeeSeparation.status != "CANCELLED")
                     .order_by(EmployeeSeparation.id.desc()).limit(1))


@app.post("/employees/{emp_id}/separation")
async def separation_save(request: Request, emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    """Step 1: record the resignation / termination, notice date, last working day, reason and remarks."""
    need(user, "employees.edit")
    emp = _emp_or_404(db, user, emp_id)
    f = await request.form()
    back = f"/employees/{emp.id}#separation"
    try:
        lwd = parse_day(str(f.get("last_working_day")))
        notice = parse_day(str(f.get("notice_date"))) if f.get("notice_date") else None
    except ValueError:
        return go(back, err="Give the last working day")
    if emp.joined_on and lwd < emp.joined_on:
        return go(back, err="Last working day is before the joining date")
    sep = _open_sep(db, emp.id)
    if sep and sep.status == "COMPLETED":
        return go(back, err="This separation is already completed")
    if not sep:
        sep = EmployeeSeparation(employee_id=emp.id, created_by=user["username"], last_working_day=lwd)
        db.add(sep)
    t = str(f.get("sep_type") or "RESIGNATION")
    sep.sep_type = t if t in letter_svc.SEP_TYPES else "RESIGNATION"
    sep.notice_date, sep.last_working_day = notice, lwd
    sep.reason = str(f.get("reason") or "").strip()[:300] or None
    sep.remarks = str(f.get("remarks") or "").strip()[:2000] or None
    audit(db, user, "SEPARATION_SAVE", f"{emp.employee_code} {sep.sep_type} last day {lwd}", commit=False)
    db.commit()
    return go(back, msg=f"Separation recorded - last working day {lwd:%d/%m/%Y}. Next: clearance")


@app.post("/employees/{emp_id}/separation/checklist")
async def separation_checklist(request: Request, emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.edit")
    emp = _emp_or_404(db, user, emp_id)
    sep = _open_sep(db, emp.id)
    if not sep or sep.status != "IN_PROGRESS":
        return go(f"/employees/{emp.id}#separation", err="Start the separation first")
    f = await request.form()
    done = {k: {"by": user["username"], "on": f"{now_local():%d/%m/%Y}"} for k in letter_svc.CHECKLIST if f.get(k)}
    old = letter_svc.checklist(sep)
    sep.checklist_json = json.dumps({k: old.get(k, v) for k, v in done.items()})
    audit(db, user, "SEPARATION_CLEARANCE", f"{emp.employee_code}: {', '.join(done) or 'none'}", commit=False)
    db.commit()
    return go(f"/employees/{emp.id}#separation", msg="Clearance saved")


@app.post("/employees/{emp_id}/separation/complete")
def separation_complete(emp_id: int, confirm: str = Form(""), user=Depends(page_user), db: Session = Depends(get_db)):
    """Step 3: employee becomes INACTIVE from the last working day; login closed; history kept."""
    need(user, "employees.edit")
    emp = _emp_or_404(db, user, emp_id)
    back = f"/employees/{emp.id}#separation"
    sep = _open_sep(db, emp.id)
    if not sep or sep.status != "IN_PROGRESS":
        return go(back, err="Start the separation first")
    out = db.scalar(select(func.count(EmployeeAsset.id)).where(EmployeeAsset.employee_id == emp.id,
                                                               EmployeeAsset.status == "ISSUED")) or 0
    if out and confirm != "yes":
        return go(back, err=f"{out} company item(s) are still with {emp.full_name}. Take them back (or mark lost) "
                            "in Assets, or tick 'complete anyway'")
    emp.employment_status, emp.left_on = "INACTIVE", sep.last_working_day
    sep.status, sep.completed_by, sep.completed_at = "COMPLETED", user["username"], now_local()
    for u in db.scalars(select(User).where(User.employee_id == emp.id)):
        u.active = False if u.role == "STAFF" else u.active
    audit(db, user, "SEPARATION_COMPLETE", f"{emp.employee_code} {sep.sep_type} left {sep.last_working_day}"
          + (f", {out} asset(s) still out" if out else ""), commit=False)
    db.commit()
    return go(back, msg=f"{emp.full_name} has left (last working day {sep.last_working_day:%d/%m/%Y}). "
                        "Process the month's payroll for the final salary.")


@app.post("/employees/{emp_id}/separation/cancel")
def separation_cancel(emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    """Resignation withdrawn / decision changed (only before it is completed)."""
    need(user, "employees.edit")
    emp = _emp_or_404(db, user, emp_id)
    sep = _open_sep(db, emp.id)
    if sep and sep.status == "IN_PROGRESS":
        sep.status = "CANCELLED"
        audit(db, user, "SEPARATION_CANCEL", emp.employee_code, commit=False)
        db.commit()
    return go(f"/employees/{emp.id}#separation", msg="Separation cancelled")


@app.post("/employees/{emp_id}/letter/{kind}")
async def employee_letter(request: Request, emp_id: int, kind: str, user=Depends(page_user),
                          db: Session = Depends(get_db)):
    """Confirmation letter, release letter, experience certificate (with remarks) - Admin."""
    need(user, "employees.view", "appointment.letter")
    from services import appointment as apt
    if kind not in letter_svc.KINDS:
        raise HTTPException(404, "Unknown letter")
    emp = _emp_or_404(db, user, emp_id)
    back = f"/employees/{emp.id}#letters"
    f = await request.form()
    try:
        letter_date = parse_day(str(f.get("letter_date"))) if f.get("letter_date") else now_local().date()
        conf = parse_day(str(f.get("confirmation_date"))) if f.get("confirmation_date") else None
    except ValueError:
        return go(back, err="Give valid dates")
    remarks = str(f.get("remarks") or "").strip()[:1500]
    sep = _open_sep(db, emp.id)
    if kind in ("release", "experience") and not sep:
        return go(back, err="Record the separation first (Leaving the company)")
    if kind == "release":
        out = db.scalar(select(func.count(EmployeeAsset.id)).where(EmployeeAsset.employee_id == emp.id,
                                                                   EmployeeAsset.status == "ISSUED")) or 0
        if out and not f.get("override"):
            return go(back, err=f"{out} company item(s) are still with {emp.full_name} - take them back first, or "
                                "tick 'issue anyway'")
    if kind == "confirmation":
        conf = conf or now_local().date()
    code = {"confirmation": "CNF", "release": "REL", "experience": "EXP"}[kind]
    ref = str(f.get("ref") or "").strip()[:60] or \
        f"{emp.company.code if emp.company else 'HO'}/HR/{code}/{letter_date:%Y}/{emp.employee_code}"
    base = apt.load_template(db)
    tpl = letter_svc.template(db, kind)
    tpl["body"] = letter_svc.drop_empty_remarks(tpl["body"], remarks)
    values = apt.values_for(db, emp, letter_date, emp.joined_on, ref, base)
    lwd = sep.last_working_day if sep else emp.left_on
    values.update(remarks=remarks, confirmation_date=f"{conf:%d %B %Y}" if conf else "",
                  last_working_day=f"{lwd:%d %B %Y}" if lwd else "-", notice_date=(f"{sep.notice_date:%d %B %Y}"
                                                                                    if sep and sep.notice_date else "-"),
                  separation_type=letter_svc.SEP_TYPES.get(sep.sep_type, "separation") if sep else "-",
                  reason=(sep.reason or "") if sep else "", service_length=letter_svc.service_length(emp.joined_on, lwd))
    title = letter_svc.KINDS[kind][0]
    pdf = apt.letter_pdf(emp, values, {**base, **tpl}, acceptance=False, title=title, to_block=kind != "experience")
    if kind == "confirmation" and f.get("save_copy") != "no":
        emp.confirmed_on = conf
    if f.get("save_copy"):
        stored, size = docs.save_file(f"emp_{emp.id}", f"{kind}-{emp.employee_code}.pdf", pdf)
        db.add(EmployeeDocument(employee_id=emp.id, doc_type=kind.upper(), title=f"{title} {ref}", stored_name=stored,
                                original_name=f"{kind}-{emp.employee_code}.pdf", size=size, uploaded_by=user["username"]))
    audit(db, user, f"LETTER_{kind.upper()}", f"{emp.employee_code} ref {ref}" + (f" remarks: {remarks[:200]}" if remarks else ""),
          commit=False)
    db.commit()
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{kind}-{emp.employee_code}.pdf"'})


@app.get("/letters/{kind}", response_class=HTMLResponse)
def letter_template_page(request: Request, kind: str, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "appointment.letter")
    if kind not in letter_svc.KINDS:
        return go("/appointment-letter")
    from services import appointment as apt
    fields = dict(apt.FIELDS, **letter_svc.CHANGE_FIELDS, remarks="Remarks typed when making the letter",
                  confirmation_date="Confirmation date",
                  last_working_day="Last working day", notice_date="Resignation / notice date",
                  separation_type="resignation / termination ...", reason="Reason for leaving",
                  service_length="Length of service, e.g. 2 years 3 months")
    return render(request, "letter_template.html", user, kind=kind, kinds=letter_svc.KINDS,
                  tpl=letter_svc.template(db, kind), fields=fields)


@app.post("/letters/{kind}")
async def letter_template_save(request: Request, kind: str, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "appointment.letter")
    if kind not in letter_svc.KINDS:
        raise HTTPException(404, "Unknown letter")
    f = await request.form()
    if f.get("reset"):
        set_setting(db, f"letter.{kind}.subject", "", commit=False)
        set_setting(db, f"letter.{kind}.body", "")
        return go(f"/letters/{kind}", msg="Standard text restored")
    body = str(f.get("body") or "").strip()
    if len(body) < 20:
        return go(f"/letters/{kind}", err="The letter text is too short")
    set_setting(db, f"letter.{kind}.subject", str(f.get("subject") or "").strip()[:200], commit=False)
    set_setting(db, f"letter.{kind}.body", body[:20000], commit=False)
    audit(db, user, "LETTER_TEMPLATE", kind, commit=False)
    db.commit()
    return go(f"/letters/{kind}", msg=f"{letter_svc.KINDS[kind][0]} text saved")


@app.post("/employees/{emp_id}/delete")
def employee_delete(emp_id: int, confirm: str = Form(""), with_punches: str = Form(""), user=Depends(page_user),
                    db: Session = Depends(get_db)):
    """Admin: remove an employee completely (e.g. entered by mistake). People who really worked here should be
    marked as left instead, so their history stays. Finalized payroll is never deleted."""
    need(user, "data.delete")
    emp = _emp_or_404(db, user, emp_id)
    back = f"/employees/{emp.id}#delete"
    if confirm.strip() != emp.employee_code:
        return go(back, err=f"Type the employee code {emp.employee_code} to confirm")
    res = _wipe_employee(db, emp, with_punches=bool(with_punches))
    if res["final"]:
        return go(back, err=f"{emp.full_name} is on {res['final']} finalized payroll(s), which are kept as financial "
                            "records. Use 'Leaving the company' to mark them as left instead")
    audit(db, user, "EMPLOYEE_DELETE", f"{res['code']} {res['name']}: {res['files']} document(s), "
                                       f"{res['punches']} punch(es) deleted", commit=False)
    db.commit()
    return go("/employees", msg=f"{res['code']} {res['name']} deleted" +
              (f" with {res['punches']} punch records" if res["punches"] else ""))


@app.post("/employees/{emp_id}/documents")
async def employee_docs_upload(request: Request, emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "employees.edit")
    emp = _emp_or_404(db, user, emp_id)
    f = await request.form()
    doc_type = str(f.get("doc_type") or "OTHER")
    if doc_type in LETTER_DOCS and "appointment.letter" not in user["perms"]:
        doc_type = "OTHER"
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
    if d and d.doc_type in LETTER_DOCS and "appointment.letter" not in user["perms"]:
        raise HTTPException(403, "Appointment letters can only be opened by an Admin")
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
    documents = [d for d in db.scalars(select(EmployeeDocument).where(EmployeeDocument.employee_id == emp.id)
                                       .order_by(EmployeeDocument.uploaded_at))
                 if d.doc_type not in LETTER_DOCS or "appointment.letter" in user["perms"]]
    pdf = profile_pdf(emp, photo_path(emp.photo), leave_svc.balances(db, emp), documents,
                      show_salary="salary.view" in user["perms"],
                      components=comp_svc.for_employee(emp, *comp_svc.load_all(db)),
                      assets=db.scalars(select(EmployeeAsset).where(EmployeeAsset.employee_id == emp.id)
                                        .order_by(EmployeeAsset.issued_on)).all())
    audit(db, user, "PROFILE_PDF", emp.employee_code)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="profile-{emp.employee_code}.pdf"'})


@app.post("/employees/{emp_id}/appointment")
async def employee_appointment(request: Request, emp_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    """Appointment letter PDF from the template (Settings > Appointment letter); optional copy to Documents."""
    need(user, "employees.view", "appointment.letter")
    from services import appointment as apt
    emp = _emp_or_404(db, user, emp_id)
    f = await request.form()
    try:
        letter_date = parse_day(str(f.get("letter_date"))) if f.get("letter_date") else now_local().date()
        joining = parse_day(str(f.get("joining_date"))) if f.get("joining_date") else emp.joined_on
    except ValueError:
        return go(f"/employees/{emp.id}", err="Give valid dates for the letter")
    ref = str(f.get("ref") or "").strip()[:60] or \
        f"{emp.company.code if emp.company else 'HO'}/HR/APT/{letter_date:%Y}/{emp.employee_code}"
    tpl = apt.load_template(db)
    values = apt.values_for(db, emp, letter_date, joining, ref, tpl)
    pdf = apt.letter_pdf(emp, values, tpl, comp_svc.for_employee(emp, *comp_svc.load_all(db)))
    if f.get("save_copy") and "employees.edit" in user["perms"]:
        stored, size = docs.save_file(f"emp_{emp.id}", f"appointment-{emp.employee_code}.pdf", pdf)
        db.add(EmployeeDocument(employee_id=emp.id, doc_type="APPOINTMENT", title=f"Appointment letter {ref}",
                                stored_name=stored, original_name=f"appointment-{emp.employee_code}.pdf", size=size,
                                uploaded_by=user["username"]))
    audit(db, user, "APPOINTMENT_LETTER", f"{emp.employee_code} ref {ref}" + (" (saved)" if f.get("save_copy") else ""))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="appointment-{emp.employee_code}.pdf"'})


@app.get("/appointment-letter", response_class=HTMLResponse)
def appointment_template_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "appointment.letter")
    from services import appointment as apt
    return render(request, "appointment_template.html", user, tpl=apt.load_template(db), fields=apt.FIELDS,
                  default_body=apt.DEFAULT_BODY)


@app.post("/appointment-letter")
async def appointment_template_save(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "appointment.letter")
    from services import appointment as apt
    f = await request.form()
    if f.get("reset"):
        for k in apt.SETTING_KEYS:
            set_setting(db, f"appointment.{k}", "", commit=False)
        audit(db, user, "APPOINTMENT_TEMPLATE", "reset to default", commit=False)
        db.commit()
        return go("/appointment-letter", msg="Template set back to the standard letter")
    body = str(f.get("body") or "").strip()
    if len(body) < 20:
        return go("/appointment-letter", err="The letter text is too short")
    pm = str(f.get("probation_months") or "6").strip()
    if not pm.isdigit() or not 0 <= int(pm) <= 24:
        return go("/appointment-letter", err="Probation months must be 0-24")
    vals = {"subject": str(f.get("subject") or "").strip()[:200], "body": body[:20000], "probation_months": pm,
            "signatory_name": str(f.get("signatory_name") or "").strip()[:100],
            "signatory_title": str(f.get("signatory_title") or "").strip()[:100]}
    for k, v in vals.items():
        set_setting(db, f"appointment.{k}", v, commit=False)
    audit(db, user, "APPOINTMENT_TEMPLATE", "saved", commit=False)
    db.commit()
    return go("/appointment-letter", msg="Appointment letter template saved")


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
        can_mobile = "mobile.manage" in user["perms"]
        pin = val("mobile_pin") if can_mobile else ""
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
        e.joined_on = parse_day(val("joined_on")) if val("joined_on") else None
        e.left_on = parse_day(val("left_on")) if val("left_on") else None
        e.date_of_birth = parse_day(val("date_of_birth")) if val("date_of_birth") else None
        e.employment_status = val("employment_status") or "ACTIVE"
        if can_salary:          # salary, pay mode and OT eligibility only for roles with "salary.edit"
            e.gross = g
            e.basic, e.house_rent, e.medical, e.transport, e.food = parts
            e.payment_mode = val("payment_mode") or "CASH"
            e.bank_name, e.account_no = val("bank_name") or None, val("account_no") or None
            sm = val("split_mode") if val("split_mode") in ("FIXED", "PERCENT") else ""
            sv = dec(val("split_value"), "Bank part") if sm else Decimal(0)
            if sm == "PERCENT" and not 0 < sv < 100:
                raise ValueError("Bank part % must be between 1 and 99")
            if sm == "FIXED" and sv <= 0:
                raise ValueError("Give the bank / wallet amount (Tk) for the split")
            if sm and e.payment_mode == "CASH":
                raise ValueError("Split pay needs pay mode BANK or MOBILE (that part goes there, the rest is cash)")
            e.split_mode, e.split_value = sm, sv
            mt = dec(val("monthly_tax"), "Monthly tax")
            if mt < 0:
                raise ValueError("Monthly tax cannot be negative")
            e.monthly_tax = mt
        if "shifts.manage" in user["perms"] and f.get("ew_mode"):
            e.weekly_off = weekly_off_from_form(f, "ew")
        if can_salary:
            if f.get("ot_field"):       # the form has the OT tick box (other callers leave OT unchanged)
                e.ot_eligible = bool(f.get("ot_eligible"))
            elif not emp_id:
                e.ot_eligible = True
        elif not emp_id:
            e.gross = e.basic = e.house_rent = e.medical = e.transport = e.food = Decimal(0)
            e.payment_mode, e.ot_eligible = "CASH", True
        for k in PROFILE_FIELDS:
            setattr(e, k, val(k)[:300] or None if k != "notes" else val(k) or None)
        if can_mobile:          # mobile punch and PIN: Admin only
            e.allow_mobile = bool(f.get("allow_mobile"))
        elif not emp_id:
            e.allow_mobile = False
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
        sig = f.get("signature")
        if sig is not None and getattr(sig, "filename", ""):
            e.signature = save_signature(e.id, await sig.read())
        if val("remove_signature"):
            e.signature = None
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
@app.get("/attendance/today")
def attendance_today(company: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    """'Today' button: refresh today's attendance, then list only the people present today."""
    need(user, "attendance.view")
    t = now_local().date()
    if "attendance.process" in user["perms"]:
        ids = [c.id for c in visible_companies(db, user)]
        process_month(db, t.year, t.month, company_ids=ids)
    q = f"/attendance?day={t}&att_status=CAME" + (f"&company={company}" if company else "")
    return go(q)


@app.get("/attendance", response_class=HTMLResponse)
def attendance_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "attendance.view")
    p = dict(request.query_params)
    d = parse_day(p["day"]) if p.get("day") else now_local().date()
    p["day"] = str(d)
    r = report_ctx(db, user, "attendance", p)
    return render(request, "attendance.html", user, r=r, day=d, status=p.get("att_status", ""),
                  company=company_filter(user, p.get("company")) or "", companies=visible_companies(db, user))


@app.post("/attendance/process")
def attendance_process(month: str = Form(...), company: str = Form(""), pull_first: str = Form(""),
                       user=Depends(page_user), db: Session = Depends(get_db)):
    """Processes every punch stored in the program for that month (all machines, files, mobile, manual)."""
    need(user, "attendance.process")
    y, m = parse_month(month)
    pulled, err = "", ""
    if pull_first and "punches.edit" in user["perms"]:
        results = devices.pull_all()      # punches still only on the machines are fetched first
        bad = [r for r in results if "ERROR" in r]
        pulled = f"Downloaded from {len(results) - len(bad)} machine(s) first. " if results else ""
        err = ("Could not reach: " + "; ".join(bad)) if bad else ""
        db.expire_all()
    cid = company_filter(user, company)
    ids = [cid] if cid else [c.id for c in visible_companies(db, user)]
    first, last = month_bounds(y, m)
    punches = db.scalar(select(func.count(AttendanceLog.id)).where(
        AttendanceLog.punch_time >= datetime.combine(first, time.min),
        AttendanceLog.punch_time < datetime.combine(last + timedelta(days=1), time.min)))
    n = len(process_month(db, y, m, company_ids=ids))
    audit(db, user, "ATTENDANCE_PROCESS", f"{y}-{m:02d}: {n} employees, {punches} punches in the month"
                                          + (f" (companies {ids})" if cid else ""))
    return go(f"/attendance?day={first if first <= now_local().date() else now_local().date()}",
              msg=f"{pulled}Processed {month_name[m]} {y}: {punches:,} punches, {n} employees "
                  "(companies with a FINAL payroll for that month are locked and skipped)", err=err)


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
    d = parse_day(day)
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
    s_ = parse_day(start) if start else today
    e_ = parse_day(end) if end else s_
    if e_ < s_:
        s_, e_ = e_, s_
    if (e_ - s_).days > 92:
        raise HTTPException(400, "Choose at most 3 months at a time")
    return s_, e_


def _month_summary(db, emp: Employee, y: int, m: int) -> dict:
    """One employee's month: present, late, absent, weekly holidays, public holidays, leave by type."""
    first, last = month_bounds(y, m)
    rows = db.scalars(select(DailyAttendance).where(DailyAttendance.employee_id == emp.id,
                                                    DailyAttendance.work_date >= first,
                                                    DailyAttendance.work_date <= last)).all()
    present_like = ("PRESENT", "LATE", "MISSING_OUT", "OFFDAY_WORK")
    s = {"days": (last - first).days + 1, "processed": len(rows),
         "present": sum(r.status in present_like for r in rows),
         "on_time": sum(r.status == "PRESENT" for r in rows), "late": sum(r.late_minutes > 0 for r in rows),
         "single": sum(r.status == "MISSING_OUT" for r in rows), "absent": sum(r.status == "ABSENT" for r in rows),
         "weekend": sum(r.day_type == "WEEKEND" for r in rows),
         "weekend_worked": sum(r.day_type == "WEEKEND" and r.status == "OFFDAY_WORK" for r in rows),
         "holiday": sum(r.day_type == "HOLIDAY" for r in rows),
         "holiday_worked": sum(r.day_type == "HOLIDAY" and r.status == "OFFDAY_WORK" for r in rows),
         "leave_paid": sum(r.status == "LEAVE_PAID" for r in rows),
         "leave_unpaid": sum(r.status == "LEAVE_UNPAID" for r in rows),
         "not_employed": sum(r.status == "NOT_EMPLOYED" for r in rows),
         "hours": round(sum(r.worked_minutes or 0 for r in rows) / 60, 1),
         "ot": round(sum((r.ot_minutes or 0) + (r.extra_ot_minutes or 0) for r in rows) / 60, 1),
         "late_min": sum(r.late_minutes or 0 for r in rows)}
    hol_days = {r.work_date for r in rows if r.day_type == "HOLIDAY"}
    names = {h.day: h.name for h in db.scalars(select(Holiday).where(Holiday.day >= first, Holiday.day <= last))}
    from services.processing import holiday_applies
    for h in db.scalars(select(SpecialHoliday).where(SpecialHoliday.day >= first, SpecialHoliday.day <= last)):
        if holiday_applies(h, emp):
            names.setdefault(h.day, h.name)
    s["holiday_list"] = [(d, names.get(d, "Holiday")) for d in sorted(hol_days)]
    by_type: dict[str, int] = {}
    tnames = {t.code: t.name for t in leave_svc.types(db, active_only=False)}
    for lv in db.scalars(select(Leave).where(Leave.employee_id == emp.id, Leave.status == "APPROVED",
                                             Leave.start_date <= last, Leave.end_date >= first)):
        n = (min(lv.end_date, last) - max(lv.start_date, first)).days + 1
        k = tnames.get(lv.leave_type, lv.leave_type)
        by_type[k] = by_type.get(k, 0) + n
    s["leave_types"] = sorted(by_type.items())
    return s


def _one_employee(db, user, q: str) -> Employee | None:
    q = (q or "").strip()
    if not q:
        return None
    stmt = select(Employee).where(or_(Employee.employee_code == q, Employee.card_no == q))
    e = db.scalar(stmt)
    if not e:
        found = db.scalars(select(Employee).where(Employee.full_name.ilike(f"%{q}%")).limit(2)).all()
        e = found[0] if len(found) == 1 else None
    if e and user.get("company_id") and e.company_id != user["company_id"]:
        return None
    return e


@app.get("/punches", response_class=HTMLResponse)
def punches_page(request: Request, page: int = 1, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "punches.view")
    p = dict(request.query_params)
    r = report_ctx(db, user, "punches", p)
    s_, e_ = _dates(p.get("start", ""), p.get("end", ""))
    emp = _one_employee(db, user, p.get("q", ""))
    sm = None
    if emp and "attendance.view" in user["perms"]:
        sy, smn = _parse_month(p["sum_month"]) if p.get("sum_month") else (s_.year, s_.month)
        sm = {"emp": emp, "y": sy, "m": smn, **_month_summary(db, emp, sy, smn),
              "prev": f"{(date(sy, smn, 1) - timedelta(days=1)):%Y-%m}",
              "next": f"{(date(sy, smn, 28) + timedelta(days=5)):%Y-%m}"}
    return render(request, "punches.html", user, r=r, start=s_, end=e_, company=company_filter(user, p.get("company")) or "",
                  sm=sm,
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
        s, e = parse_day(str(f.get("start_date"))), parse_day(str(f.get("end_date")))
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
    so = select(Employee).where(Employee.weekly_off.is_not(None), Employee.weekly_off != "",
                                Employee.employment_status == "ACTIVE").order_by(Employee.employee_code).limit(300)
    if user.get("company_id"):
        so = so.where(Employee.company_id == user["company_id"])
    return render(request, "shifts.html", user, r=r, shifts=r["rows"], companies=visible_companies(db, user),
                  staff_off=db.scalars(so).all(),
                  e=db.get(Shift, edit) if edit else None)


@app.post("/shifts/save")
async def shift_save(request: Request, shift_id: str = Form(""), code: str = Form(...), name: str = Form(...), start: str = Form(...),
               end: str = Form(...), grace_minutes: int = Form(10), break_minutes: int = Form(60),
               break_after_minutes: int = Form(300), early_window_minutes: int = Form(180),
               ot_round_minutes: int = Form(30), max_ot_minutes: int = Form(120), offday_work_is_ot: str = Form(""),
               late_count_from: str = Form("START"), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "shifts.manage")
    try:
        weekly = weekly_off_from_form(await request.form(), "wo")
    except ValueError as exc:
        return go("/shifts", err=str(exc))
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
    s.weekly_off = weekly
    if not shift_id:
        db.add(s)
    audit(db, user, "SHIFT_SAVE", f"{code} {start}-{end}", commit=False)
    db.commit()
    return go("/shifts", msg=f"Shift {code} saved - process the month again to apply changes")


@app.post("/shifts/weekly-off")
async def weekly_off_assign(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    """Give employees their own weekly off day(s), e.g. some Friday, others Saturday or Sunday."""
    need(user, "shifts.manage")
    f = await request.form()
    try:
        weekly = weekly_off_from_form(f, "ew")
    except ValueError as exc:
        return go("/shifts", err=str(exc))
    codes = [c.strip() for c in str(f.get("employee_codes") or "").replace(",", "\n").splitlines() if c.strip()]
    conds = []
    if codes:
        conds.append(or_(Employee.employee_code.in_(codes), Employee.card_no.in_(codes)))
    dept = str(f.get("department") or "").strip()
    if dept:
        conds.append(Employee.department.has(Department.name == dept))
    if not conds:
        return go("/shifts", err="Give employee codes or a department")
    stmt = select(Employee).where(or_(*conds))
    if user.get("company_id"):
        stmt = stmt.where(Employee.company_id == user["company_id"])
    emps = db.scalars(stmt).all()
    for e in emps:
        e.weekly_off = weekly
    audit(db, user, "WEEKLY_OFF", f"{weekly_off_text(weekly)} -> {len(emps)} employees", commit=False)
    db.commit()
    return go("/shifts", msg=f"Weekly off '{weekly_off_text(weekly)}' set for {len(emps)} employees - "
                             "process the month again to apply it")


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


def _all_mode(user, company: str, comps) -> bool:
    """One payroll for all companies (they all sit at the Head Office): default for Head Office users."""
    if user.get("company_id") or len(comps) < 2:
        return False
    return company in ("all", "") or company is None


def _all_runs(db, user, y, m):
    comps = visible_companies(db, user)
    runs = [r for r in db.scalars(select(PayrollRun).where(PayrollRun.year == y, PayrollRun.month == m,
                                                         PayrollRun.company_id.in_([c.id for c in comps] or [0])))]
    return comps, sorted(runs, key=lambda r: r.company.code)


def _all_status(comps, runs) -> str | None:
    if not runs:
        return None
    st = {r.status for r in runs}
    if st == {"FINAL"}:
        return "FINAL" if len(runs) == len(comps) else "PARTLY FINAL"
    return "PARTLY FINAL" if "FINAL" in st else "DRAFT"


@app.get("/payroll", response_class=HTMLResponse)
def payroll_page(request: Request, month: str = "", company: str = "", user=Depends(page_user),
                 db: Session = Depends(get_db)):
    need(user, "payroll.view")
    from types import SimpleNamespace as NS
    y, m = parse_month(month)
    comps = visible_companies(db, user)
    runs = {r.company_id: r for r in db.scalars(select(PayrollRun).where(PayrollRun.year == y, PayrollRun.month == m))}
    all_mode = _all_mode(user, company, comps)
    if all_mode:
        cid = "all"
        rl = [r for r in runs.values() if r.company_id in {c.id for c in comps}]
        lines = sorted((l for r in rl for l in r.lines), key=lambda l: (l.department, l.employee_code))
        status = _all_status(comps, rl)
        run = NS(id=None, status=status, created_by=", ".join(sorted({r.created_by for r in rl})),
                 created_at=max(r.created_at for r in rl), finalized_by=None,
                 finalized_at=None) if rl else None
        company = NS(id=0, code="ALL", name=f"all companies ({', '.join(c.code for c in comps)})")
        run_base = f"/payroll/all/{y:04d}-{m:02d}"
        bank = disb.company_bank(db, 0)
    else:
        cid = company_filter(user, company) or (comps[0].id if comps else None)
        check_company(user, cid)
        run = runs.get(cid)
        lines = sorted(run.lines, key=lambda l: (l.department, l.employee_code)) if run else []
        company = db.get(Company, cid) if cid else None
        run_base = f"/payroll/run/{run.id}" if run else ""
        bank = disb.company_bank(db, cid) if cid else {}
    totals = {k: sum((getattr(l, k) or 0) for l in lines) for k in
              ("earned_gross", "ot_amount", "absent_deduction", "total_deductions", "net_pay", "custom_additions",
               "late_deduction")} if lines else {}
    pay_tot = disb.totals(lines)
    by_mode = pay_tot["amount"]
    r = report_ctx(db, user, "payroll", {"company": "" if all_mode else str(cid or ""), "month": f"{y}-{m:02d}"})
    return render(request, "payroll.html", user, y=y, m=m, run=run, lines=lines, totals=totals, by_mode=by_mode,
                  companies=comps, cid=cid, company=company, runs=runs, all_mode=all_mode, run_base=run_base,
                  warn_count=sum(1 for l in lines if l.warnings), r=r,
                  bank=bank, pay_problems=disb.problems(lines),
                  count_mode=pay_tot["people"],
                  modes=disb.MODES, by_company={c.code: (len(runs[c.id].lines), runs[c.id].status) if c.id in runs else None
                                                 for c in comps})


@app.get("/payroll/all/{ym}/{what}")
def payroll_all_files(ym: str, what: str, employee_code: str = "", user=Depends(page_user),
                      db: Session = Depends(get_db)):
    """All companies together: salary.xlsx, payslips.pdf, payment.pdf, transfer.xlsx."""
    need(user, "payroll.view", "reports.export")
    from types import SimpleNamespace as NS
    from services.branding import BRAND
    y, m = parse_month(ym)
    comps, runs = _all_runs(db, user, y, m)
    if not runs:
        return go(f"/payroll?company=all&month={y:04d}-{m:02d}", err="Process the payroll first")
    lines = [l for r in runs for l in r.lines]
    tag = f"{y}-{m:02d}"
    audit(db, user, "PAYROLL_EXPORT", f"all companies {tag} {what}" + (f" {employee_code}" if employee_code else ""))
    if what == "salary.xlsx":
        return Response(combined_salary(runs, y, m), media_type=XLSX,
                        headers={"Content-Disposition": f"attachment; filename=salary-all-companies-{tag}.xlsx"})
    if what == "payslips.pdf":
        sel = [l for l in lines if not employee_code or l.employee_code == employee_code]
        return Response(payslips_pdf(None, sel), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="payslips-{employee_code or "all"}-{tag}.pdf"'})
    status = _all_status(comps, runs)
    group = NS(company=NS(name=BRAND["name"], code="ALL", address=BRAND.get("address") or ""), year=y, month=m,
               status="FINAL" if status == "FINAL" else "DRAFT", lines=lines)
    if what == "payment.pdf":
        return Response(disb.payment_pdf(group, disb.company_bank(db, 0), now_local().date()),
                        media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="payment-all-companies-{tag}.pdf"'})
    if what == "transfer.xlsx":
        return Response(disb.transfer_xlsx(group), media_type=XLSX,
                        headers={"Content-Disposition": f"attachment; filename=bank-transfer-all-companies-{tag}.xlsx"})
    raise HTTPException(404, "Unknown file")


@app.post("/payroll/process")
def payroll_process(month: str = Form(...), company_id: str = Form(...), user=Depends(page_user),
                    db: Session = Depends(get_db)):
    need(user, "payroll.process")
    y, m = parse_month(month)
    if company_id == "all":
        back = f"/payroll?company=all&month={y:04d}-{m:02d}"
        done, locked, errors = [], [], []
        for c in visible_companies(db, user):
            try:
                run = run_payroll(db, c.id, y, m, user)
                done.append(f"{c.code} {len(run.lines)}")
                errors += [f"{c.code}: {e}" for e in run.errors]
            except PayrollLocked:
                locked.append(c.code)
            except ValueError as exc:
                errors.append(f"{c.code}: {exc}")
        if errors:
            log_error("PAYROLL", f"all companies {y}-{m:02d}: {len(errors)} employees skipped",
                      detail="\n".join(errors), username=user["username"])
        n = sum(int(x.split()[-1]) for x in done)
        return go(back, msg=f"Draft payroll ready for {n} employees ({', '.join(done)})"
                            + (f"; already final, not changed: {', '.join(locked)}" if locked else ""),
                  err=("Skipped: " + "; ".join(errors)[:1200]) if errors else "")
    company_id = int(company_id)
    check_company(user, company_id)
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
def payroll_finalize(month: str = Form(...), company_id: str = Form(...), confirm: str = Form(""),
                     user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "payroll.finalize")
    y, m = parse_month(month)
    if company_id == "all":
        back = f"/payroll?company=all&month={y:04d}-{m:02d}"
        if confirm.strip().upper() != "FINAL":
            return go(back, err="Type FINAL to confirm")
        comps, runs = _all_runs(db, user, y, m)
        have = {r.company_id for r in runs}
        missing = [c.code for c in comps if c.id not in have and c.active]
        if missing:
            return go(back, err=f"Process the payroll first - not processed yet: {', '.join(missing)}")
        done = []
        for r in runs:
            if r.status != "FINAL":
                finalize_payroll(db, r.company_id, y, m, user)
                done.append(r.company.code)
        return go(back, msg=f"Payroll finalized and locked: {', '.join(done) or 'already final'}")
    company_id = int(company_id)
    check_company(user, company_id)
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


@app.get("/payroll/run/{run_id}/payment.pdf")
def payroll_payment_pdf(run_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    """Bank advice letter(s), mobile banking list and cash payment sheet."""
    need(user, "payroll.view", "reports.export")
    run = _run_for(db, user, run_id)
    audit(db, user, "PAYMENT_ADVICE", f"{run.company.code} {run.year}-{run.month:02d}")
    return Response(disb.payment_pdf(run, disb.company_bank(db, run.company_id), now_local().date()),
                    media_type="application/pdf", headers={
                        "Content-Disposition": f'inline; filename="payment-{run.company.code}-{run.year}-{run.month:02d}.pdf"'})


@app.get("/payroll/run/{run_id}/transfer.xlsx")
def payroll_transfer_xlsx(run_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "payroll.view", "reports.export")
    run = _run_for(db, user, run_id)
    audit(db, user, "PAYMENT_FILE", f"{run.company.code} {run.year}-{run.month:02d}")
    return Response(disb.transfer_xlsx(run), media_type=XLSX, headers={
        "Content-Disposition": f"attachment; filename=bank-transfer-{run.company.code}-{run.year}-{run.month:02d}.xlsx"})


@app.post("/payroll/bank-settings")
async def payroll_bank_settings(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "payroll.finalize")
    f = await request.form()
    cid = 0 if f.get("company_id") == "all" else int(f.get("company_id"))
    if cid or user.get("company_id"):
        check_company(user, cid)
    for k in disb.BANK_KEYS:
        set_setting(db, f"salary_bank.{k}", str(f.get(k) or "").strip()[:120], cid, commit=False)
    audit(db, user, "SALARY_BANK", f"company {cid}: {f.get('bank_name')} {f.get('account_no')}", commit=False)
    db.commit()
    return go(f"/payroll?company={cid or 'all'}&month={f.get('month')}#payment", msg="Company salary account saved")


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
async def payroll_adjustments(month: str = Form(...), company_id: str = Form(...), file: UploadFile = File(...),
                              tab: str = Form(""), user=Depends(page_user), db: Session = Depends(get_db)):
    """Excel: employee_code, other_addition, advance, other_deduction, tax, note (one company or all)."""
    need(user, "payroll.process")
    from openpyxl import load_workbook
    ids, company_id, _, _ = _scope(db, user, company_id)
    y, m = parse_month(month)
    back = (f"/payroll?company={company_id}&month={y:04d}-{m:02d}" if tab not in INPUT_TABS else
            f"/payroll/inputs?company={company_id}&month={y:04d}-{m:02d}&tab={tab}")
    locked = _locked(db, ids, y, m)
    if ids and set(ids) <= locked:
        return go(back, err="Payroll is final - adjustments are locked")
    try:
        ws = load_workbook(io.BytesIO(await file.read()), data_only=True).worksheets[0]
        it = ws.iter_rows(values_only=True)
        head = [str(h or "").strip().lower() for h in next(it)]
        if "employee_code" not in head:
            raise ValueError("first row must contain employee_code")
        emps = {e.employee_code: e for e in db.scalars(select(Employee).where(Employee.company_id.in_(ids or [0])))}
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
            if e.company_id in locked:
                raise ValueError(f"{code}: that company's payroll for this month is final")
            a = db.scalar(select(PayrollAdjustment).where(PayrollAdjustment.employee_id == e.id,
                                                          PayrollAdjustment.year == y, PayrollAdjustment.month == m))
            if not a:
                a = PayrollAdjustment(employee_id=e.id, year=y, month=m)
                db.add(a)
            for k in ("other_addition", "advance", "other_deduction", "tax"):
                if k in head:          # columns not in the file are left as they are
                    setattr(a, k, dec(str(r.get(k) or 0), k))
            if "note" in head:
                a.note = (str(r.get("note") or "").strip() or None)
            n += 1
        audit(db, user, "ADJUSTMENT_IMPORT", f"company {company_id} {y}-{m:02d}: {n} rows", commit=False)
        db.commit()
    except Exception as exc:
        db.rollback()
        return go(back, err=f"Nothing saved: {exc}")
    return go(back, msg=f"{n} adjustments saved - process the payroll again to apply them")


@app.get("/payroll/adjustments-template.xlsx")
def adjustments_template(tab: str = "manual", company: str = "", month: str = "", user=Depends(page_user),
                         db: Session = Depends(get_db)):
    """Excel for the chosen input (manual / advances / tax / other); with company + month it lists the staff
    and the values already entered, ready to fill in and upload."""
    need(user, "payroll.process")
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    cols = INPUT_TABS.get(tab, INPUT_TABS["manual"])["cols"]
    wb = Workbook()
    ws = wb.active
    ws.title = INPUT_TABS.get(tab, INPUT_TABS["manual"])["title"][:30]
    ws.append(["employee_code", "name", "department"] + [c for c, _ in cols])
    for cell in ws[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="E8332F")
    ids = _scope(db, user, company)[0] if company else []
    if ids:
        y, m = parse_month(month)
        emps, adj = _input_rows(db, ids, y, m, "", "")
        for e in emps:
            a = adj.get(e.id)
            ws.append([e.employee_code, e.full_name, e.department.name if e.department else ""]
                      + [(getattr(a, c) or "") if a else ("" if c == "note" else 0) for c, _ in cols])
    else:
        ws.append(["LG0001", "Example name", "Accounts"] + [("" if c == "note" else 0) for c, _ in cols])
    for col, w in zip("ABCDEFGH", (14, 28, 18, 14, 14, 14, 14, 30)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type=XLSX,
                    headers={"Content-Disposition": f"attachment; filename=payroll-{tab}-template.xlsx"})


# ---------------------------------------------------------------- payroll inputs (manual, advances, tax, other)
INPUT_TABS = {
    "manual": {"title": "Employee-wise (manual)", "cols": [("other_addition", "Other addition"), ("advance", "Advance"),
                                                           ("other_deduction", "Deduction"), ("tax", "Tax"),
                                                           ("note", "Note")],
               "help": "Type each employee's amounts for this month in one place."},
    "advances": {"title": "Advances & deductions", "cols": [("advance", "Advance (Tk)"), ("other_deduction",
                                                                                         "Other deduction (Tk)"),
                                                            ("note", "Note")],
                 "help": "Salary advances to recover and deductions like fines, loss or canteen bills."},
    "tax": {"title": "Tax", "cols": [("tax", "Income tax / TDS (Tk)")],
            "help": "Income tax deducted at source this month. Empty = the employee's monthly tax from their profile "
                    "(shown in grey); type an amount to use a different figure this month."},
    "other": {"title": "Other", "cols": [("other_addition", "Other addition (Tk)"), ("note", "Note")],
              "help": "Arrears, night allowance, special bonus or any other one-time payment."},
}


def _scope(db, user, company):
    """(company ids, cid value for links, display company). 'all' = every company the user may see."""
    from types import SimpleNamespace as NS
    comps = visible_companies(db, user)
    if _all_mode(user, company, comps):
        return [c.id for c in comps], "all", NS(id=0, code="ALL", name="All companies"), comps
    cid = company_filter(user, company) or (comps[0].id if comps else None)
    if cid:
        check_company(user, cid)
    return ([cid] if cid else []), cid, db.get(Company, cid) if cid else None, comps


def _scope_run(db, ids, y, m):
    """The payroll run to show (one company) or a combined status (all companies)."""
    from types import SimpleNamespace as NS
    if len(ids) == 1:
        return get_run(db, ids[0], y, m)
    runs = [r for r in db.scalars(select(PayrollRun).where(PayrollRun.year == y, PayrollRun.month == m,
                                                         PayrollRun.company_id.in_(ids or [0])))]
    st = _all_status([0] * len(ids), runs)
    return NS(status=st) if st else None


def _locked(db, ids, y, m) -> set[int]:
    return set(db.scalars(select(PayrollRun.company_id).where(PayrollRun.year == y, PayrollRun.month == m,
                                                             PayrollRun.status == "FINAL",
                                                             PayrollRun.company_id.in_(ids or [0]))))


def _process_scope(db, user, ids, y, m) -> tuple[int, list[str]]:
    n, errors = 0, []
    for cid in ids:
        try:
            run = run_payroll(db, cid, y, m, user)
            n += len(run.lines)
            errors += run.errors
        except PayrollLocked:
            continue
        except ValueError as exc:
            errors.append(str(exc))
    return n, errors


def _input_rows(db: Session, cid, y: int, m: int, q: str, dept: str):
    from services.processing import employees_for_month
    first, last = month_bounds(y, m)
    emps = [e for e in employees_for_month(db, first, last, cid if isinstance(cid, list) else [cid])]
    if dept:
        emps = [e for e in emps if e.department and str(e.department.id) == dept]
    if q:
        ql = q.lower()
        emps = [e for e in emps if ql in e.employee_code.lower() or ql in (e.card_no or "").lower()
                or ql in e.full_name.lower()]
    emps.sort(key=lambda e: ((e.department.name if e.department else ""), e.employee_code))
    ids = [e.id for e in emps] or [0]
    adj = {}
    for i in range(0, len(ids), 500):
        for a in db.scalars(select(PayrollAdjustment).where(PayrollAdjustment.year == y, PayrollAdjustment.month == m,
                                                            PayrollAdjustment.employee_id.in_(ids[i:i + 500]))):
            adj[a.employee_id] = a
    return emps, adj


@app.get("/payroll/bonus", response_class=HTMLResponse)
def payroll_bonus(request: Request, company: str = "", month: str = "", name: str = "", basis: str = "basic",
                  percent: str = "100", min_months: str = "6", prorate: str = "", religion: str = "", as_of: str = "",
                  calc: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    """Festival bonus: calculate by rule, check / change amounts, save into this month's salary."""
    need(user, "payroll.process")
    from services import bonus as bonus_svc
    y, m = parse_month(month)
    ids, cid, company_obj, comps = _scope(db, user, company)
    if not ids:
        return go("/settings", err="Add a company first")
    first, last = month_bounds(y, m)
    try:
        as_of_d = parse_day(as_of) if as_of else last
        pct = dec(percent, "Percent")
        minm = int(min_months or 0)
    except ValueError as exc:
        return go(f"/payroll/bonus?company={cid}&month={y:04d}-{m:02d}", err=str(exc))
    emps, adj = _input_rows(db, ids, y, m, "", "")
    name = name.strip()[:60] or bonus_svc.NAMES[0]
    rows = bonus_svc.compute(emps, as_of_d, "gross" if basis == "gross" else "basic", pct, minm, bool(prorate),
                             religion) if calc else []
    saved = bonus_svc.existing(adj.values(), name)
    if not calc:      # show what is saved for this bonus name
        rows = [bonus_svc.BonusRow(e, bonus_svc.service_months(e.joined_on, as_of_d), saved.get(e.id, Decimal(0)),
                                   "saved" if e.id in saved else "") for e in emps]
    return render(request, "payroll_bonus.html", user, rows=rows, calc=bool(calc), saved=saved, bonus_name=name,
                  names=bonus_svc.NAMES, basis=basis, percent=percent, min_months=min_months, prorate=prorate,
                  religion=religion, as_of=as_of_d, y=y, m=m, mval=f"{y:04d}-{m:02d}", cid=cid, companies=comps,
                  company=company_obj, run=_scope_run(db, ids, y, m), tabs=INPUT_TABS, multi=len(ids) > 1,
                  existing_names=bonus_svc.names_in(adj.values()),
                  total=sum((r.amount for r in rows), Decimal(0)))


@app.post("/payroll/bonus")
async def payroll_bonus_save(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "payroll.process")
    from services import bonus as bonus_svc
    f = await request.form(max_fields=50000)
    scope, cid, _, _ = _scope(db, user, str(f.get("company_id") or ""))
    y, m = parse_month(str(f.get("month")))
    name = str(f.get("name") or "").strip()[:60]
    back = f"/payroll/bonus?company={cid}&month={y:04d}-{m:02d}&name={quote(name)}"
    if not name:
        return go(back, err="Give the bonus a name")
    locked = _locked(db, scope, y, m)
    if scope and set(scope) <= locked:
        return go(back, err="This month's payroll is final (locked)")
    remove = bool(f.get("remove"))
    ids = [int(x) for x in f.getlist("emp") if str(x).isdigit()]
    valid = {e.id: e for e in db.scalars(select(Employee).where(Employee.id.in_(ids or [0]),
                                                                Employee.company_id.in_(scope or [0])))
             if e.company_id not in locked}
    adjs = {a.employee_id: a for a in db.scalars(select(PayrollAdjustment).where(
        PayrollAdjustment.year == y, PayrollAdjustment.month == m, PayrollAdjustment.employee_id.in_(list(valid) or [0])))}
    n, total = 0, Decimal(0)
    try:
        for eid, e in valid.items():
            amt = Decimal(0) if remove else dec(str(f.get(f"amt_{eid}") or "0"), f"{e.employee_code} bonus")
            if amt < 0:
                raise ValueError(f"{e.employee_code}: bonus cannot be negative")
            a = adjs.get(eid)
            if a is None:
                if not amt:
                    continue
                a = PayrollAdjustment(employee_id=eid, year=y, month=m)
                db.add(a)
            bonus_svc.set_item(a, name, amt)
            a.edited_by = user["username"]
            if amt:
                n += 1
                total += amt
    except ValueError as exc:
        db.rollback()
        return go(back, err=str(exc))
    audit(db, user, "BONUS_REMOVE" if remove else "BONUS_SAVE",
          f"company {cid} {y}-{m:02d} {name}: {n} employees, Tk {total}", commit=False)
    db.commit()
    if remove:
        return go(back, msg=f"{name} removed from {month_name[m]} {y}")
    if f.get("process"):
        cnt, errs = _process_scope(db, user, scope, y, m)
        return go(f"/payroll?company={cid}&month={y:04d}-{m:02d}",
                  msg=f"{name} saved for {n} employees (Tk {total:,.0f}). Draft payroll ready for {cnt} employees",
                  err=("Skipped: " + "; ".join(errs)[:1200]) if errs else "")
    return go(back, msg=f"{name} saved for {n} employees, total Tk {total:,.0f} - process the payroll to apply it")


@app.get("/payroll/inputs", response_class=HTMLResponse)
def payroll_inputs(request: Request, tab: str = "manual", company: str = "", month: str = "", q: str = "",
                   department: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "payroll.process")
    tab = tab if tab in INPUT_TABS else "manual"
    y, m = parse_month(month)
    ids, cid, company_obj, comps = _scope(db, user, company)
    if not ids:
        return go("/settings", err="Add a company first")
    emps, adj = _input_rows(db, ids, y, m, q.strip(), department)
    run = _scope_run(db, ids, y, m)
    locked = _locked(db, ids, y, m)
    cols = INPUT_TABS[tab]["cols"]
    totals = {c: sum((getattr(adj[e.id], c) or 0) for e in emps if e.id in adj) for c, _ in cols if c != "note"}
    return render(request, "payroll_inputs.html", user, tab=tab, tabs=INPUT_TABS, cols=cols, emps=emps, adj=adj,
                  y=y, m=m, mval=f"{y:04d}-{m:02d}", cid=cid, companies=comps, company=company_obj, run=run,
                  q=q, department=department, totals=totals, locked=locked, multi=len(ids) > 1,
                  depts=db.scalars(select(Department).order_by(Department.name)).all())


@app.post("/payroll/inputs")
async def payroll_inputs_save(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "payroll.process")
    f = await request.form(max_fields=50000)
    tab = str(f.get("tab") or "manual")
    tab = tab if tab in INPUT_TABS else "manual"
    scope, cid, _, _ = _scope(db, user, str(f.get("company_id") or ""))
    y, m = parse_month(str(f.get("month")))
    back = f"/payroll/inputs?company={cid}&month={y:04d}-{m:02d}&tab={tab}"
    for k in ("q", "department"):
        if f.get(k):
            back += f"&{k}={quote(str(f.get(k)))}"
    locked = _locked(db, scope, y, m)
    if scope and set(scope) <= locked:
        return go(back, err="This month's payroll is final (locked) - nothing can be changed")
    cols = [c for c, _ in INPUT_TABS[tab]["cols"]]
    ids = [int(x) for x in f.getlist("emp") if str(x).isdigit()]
    valid = {e.id: e for e in db.scalars(select(Employee).where(Employee.id.in_(ids or [0]),
                                                                Employee.company_id.in_(scope or [0])))
             if e.company_id not in locked}
    existing = {a.employee_id: a for a in db.scalars(select(PayrollAdjustment).where(
        PayrollAdjustment.year == y, PayrollAdjustment.month == m, PayrollAdjustment.employee_id.in_(list(valid) or [0])))}
    changed, errors = 0, []
    for eid, e in valid.items():
        try:
            new = {}
            for c in cols:
                raw = str(f.get(f"{c}_{eid}") or "").strip()
                if c == "note":
                    new[c] = raw[:250] or None
                else:
                    v = dec(raw, f"{e.employee_code} {c.replace('_', ' ')}")
                    if v < 0:
                        raise ValueError(f"{e.employee_code}: amounts cannot be negative")
                    new[c] = v
        except ValueError as exc:
            errors.append(str(exc))
            continue
        a = existing.get(eid)
        if a is None:
            if not any(v for v in new.values()):
                continue          # nothing typed for this person
            a = PayrollAdjustment(employee_id=eid, year=y, month=m)
            db.add(a)
        if any((getattr(a, c) or (None if c == "note" else Decimal(0))) != v for c, v in new.items()):
            for c, v in new.items():
                setattr(a, c, v)
            a.edited_by = user["username"]
            changed += 1
    if errors:
        db.rollback()
        return go(back, err="Nothing saved: " + "; ".join(errors[:10]))
    audit(db, user, "PAYROLL_INPUTS", f"company {cid} {y}-{m:02d} {tab}: {changed} employees changed", commit=False)
    db.commit()
    if f.get("process"):
        n, errs = _process_scope(db, user, scope, y, m)
        return go(f"/payroll?company={cid}&month={y:04d}-{m:02d}", msg=f"Saved {changed} employee(s). Draft payroll "
                                                                      f"ready for {n} employees",
                  err=("Skipped: " + "; ".join(errs)[:1200]) if errs else "")
    return go(back, msg=f"Saved - {changed} employee(s) changed. Process the payroll to apply.")


# ---------------------------------------------------------------- settings
SETTINGS_PERMS = {"company.manage", "policy.edit", "holidays.manage", "payroll.view", "attendance.view"}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, policy_company: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    if not SETTINGS_PERMS & user["perms"]:
        raise HTTPException(403, "Not allowed")
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
                  geo_mode=mobile.geo_mode(db),
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
    mode = str(f.get("geofence_mode") or "ANYWHERE").upper()
    mode = mode if mode in mobile.MODES else "ANYWHERE"
    if mode == "STRICT" and not (lat or lon):
        return go("/settings", err="'Only inside the office' needs the office latitude and longitude")
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
    d = parse_day(day)
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
        d1 = parse_day(day)
        d2 = parse_day(day_to) if day_to else d1
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
def users_page(request: Request, role: str = "", edit: str = "", user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "users.manage")
    roles = db.scalars(select(Role).order_by(Role.builtin.desc(), Role.name)).all()
    return render(request, "users.html", user, users=db.scalars(select(User).order_by(User.username)).all(),
                  eu=(eu := db.scalar(select(User).where(User.username == edit)) if edit else None),
                  linked_code=(db.get(Employee, eu.employee_id).employee_code
                               if eu and eu.employee_id and db.get(Employee, eu.employee_id) else ""),
                  staff_on=staff_login_on(db), staff_pw=staff_default_password(db),
                  staff_count=db.scalar(select(func.count(User.id)).where(User.role == "STAFF")) or 0,
                  roles=roles, edit_role=db.scalar(select(Role).where(Role.name == role)) if role else None,
                  companies=db.scalars(select(Company).order_by(Company.code)).all(), perms=PERMS, groups=GROUPS,
                  role_perms={r.name: (set(PERMS) if r.name == "ADMIN" else parse_perms(r.permissions)) for r in roles},
                  counts=dict(db.execute(select(User.role, func.count()).group_by(User.role)).all()))


@app.post("/users/save")
def user_save(username: str = Form(...), role: str = Form(...), password: str = Form(""), active: str = Form(""),
              company_id: str = Form(""), full_name: str = Form(""), employee_code: str = Form(""),
              super_admin: str = Form(""), user=Depends(page_user),
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
    if u.super_admin and not user.get("super_admin"):
        db.rollback()
        return go("/users", err="Only a Super Admin can change a Super Admin")
    if u.role == "ADMIN" and user["role"] != "ADMIN":
        db.rollback()
        return go("/users", err="Only an administrator can change an administrator")
    if u.username == user["username"] and (role != user["role"] or not active or company_id):
        db.rollback()
        return go("/users", err="You cannot change your own role, company limit or active status")
    u.role, u.active, u.full_name = role, bool(active), full_name.strip() or u.full_name
    if user.get("super_admin"):                  # only a Super Admin can make / remove another Super Admin
        want = bool(super_admin) and role == "ADMIN"
        if u.username == user["username"] and not want:
            db.rollback()
            return go("/users", err="You cannot remove your own Super Admin right - ask another Super Admin")
        u.super_admin = want
    elif role != "ADMIN":
        u.super_admin = False
    u.company_id = int(company_id) if company_id else None
    code = employee_code.strip()
    if code:      # this login is also an employee: gives them My page (own attendance, leave, payslips)
        emp = db.scalar(select(Employee).where(or_(Employee.employee_code == code, Employee.card_no == code)))
        if not emp:
            db.rollback()
            return go("/users", err=f"No employee with code {code}")
        other = db.scalar(select(User).where(User.employee_id == emp.id, User.username != u.username))
        if other:
            db.rollback()
            return go("/users", err=f"{emp.employee_code} is already linked to the login '{other.username}'")
        u.employee_id = emp.id
    elif role == "STAFF" and not u.employee_id:      # Staff made by hand: username = employee code
        emp = db.scalar(select(Employee).where(Employee.employee_code == u.username))
        if emp and not db.scalar(select(User).where(User.employee_id == emp.id, User.username != u.username)):
            u.employee_id = emp.id
    elif role != "STAFF":
        u.employee_id = None
    audit(db, user, "USER_SAVE", f"{u.username} role={role} company={company_id or 'ALL'} active={bool(active)}"
          + (" SUPER ADMIN" if u.super_admin else "")
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


# ---------------------------------------------------------------- My page (Staff self-service)
class NotLinked(Exception):
    pass


def _me(db, user) -> Employee:
    need(user, "self.service")
    emp = db.get(Employee, user.get("employee_id") or 0)
    if not emp:      # e.g. a Staff user made by hand: link it when the username is an employee code
        emp = db.scalar(select(Employee).where(Employee.employee_code == user["username"]))
        u = db.scalar(select(User).where(User.username == user["username"]))
        taken = emp and db.scalar(select(User).where(User.employee_id == emp.id, User.username != user["username"]))
        if not emp or not u or taken:
            raise NotLinked()
        u.employee_id = emp.id
        audit(db, user, "STAFF_LINK", f"login {u.username} linked to employee {emp.employee_code}", commit=False)
        db.commit()
    return emp


@app.exception_handler(NotLinked)
async def _not_linked(request: Request, exc: NotLinked):
    """A login that is not an employee (e.g. admin) opened My page: explain instead of an error."""
    try:
        user = page_user(request)
    except Exception:  # noqa: BLE001
        return RedirectResponse("/login", status_code=303)
    if "users.manage" in user["perms"]:
        return go(f"/users?edit={quote(user['username'])}#userform",
                  msg="My page shows an employee's own data. Your login is not linked to an employee - "
                      "type your Employee ID in 'Linked employee' below if you have one.")
    return go("/", err="My page is only for logins linked to an employee - ask HR to link yours")


@app.get("/me", response_class=HTMLResponse)
def me_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    emp = _me(db, user)
    today = now_local().date()
    first, last = month_bounds(today.year, today.month)
    days = db.scalars(select(DailyAttendance).where(DailyAttendance.employee_id == emp.id,
                                                    DailyAttendance.work_date >= first, DailyAttendance.work_date <= last)
                      .order_by(DailyAttendance.work_date.desc())).all()
    summary = {"present": sum(d.status in ("PRESENT", "LATE", "MISSING_OUT", "OFFDAY_WORK") for d in days),
               "late": sum(d.late_minutes > 0 for d in days), "absent": sum(d.status == "ABSENT" for d in days),
               "leave": sum(d.status in ("LEAVE_PAID", "LEAVE_UNPAID") for d in days)}
    return render(request, "me.html", user, e=emp, days=days, summary=summary, today=today,
                  bal=leave_svc.balances(db, emp),
                  ltypes=[t for t in leave_svc.types(db) if t.self_request and (not t.gender or t.gender == emp.gender)],
                  my_leave=db.scalars(select(Leave).where(Leave.employee_id == emp.id)
                                      .order_by(Leave.start_date.desc()).limit(15)).all(),
                  labels=leave_svc.STATUS_LABEL,
                  slips=db.execute(select(PayrollLine, PayrollRun).join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
                                   .where(PayrollLine.employee_id == emp.id, PayrollRun.status == "FINAL")
                                   .order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).limit(12)).all(),
                  assets=db.scalars(select(EmployeeAsset).where(EmployeeAsset.employee_id == emp.id,
                                                               EmployeeAsset.status == "ISSUED")).all(),
                  asset_cats=asset_svc.CATEGORIES, notices=change_svc.notices_for(db, emp))


@app.get("/me/photo")
def me_photo(user=Depends(page_user), db: Session = Depends(get_db)):
    emp = _me(db, user)
    path = photo_path(emp.photo)
    if not path:
        return RedirectResponse("/static/img/no-photo.png", status_code=303)
    return FileResponse(path, media_type="image/jpeg")


@app.post("/me/leave")
async def me_leave(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    emp = _me(db, user)
    ok_, msg = await _request_leave(db, emp, await request.form(), user["username"])
    return go("/me#leave", msg=msg if ok_ else "", err="" if ok_ else msg)


@app.post("/me/leave/{leave_id}/cancel")
def me_leave_cancel(leave_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    emp = _me(db, user)
    lv = db.get(Leave, leave_id)
    if not lv or lv.employee_id != emp.id or lv.status not in leave_svc.OPEN:
        return go("/me#leave", err="This request cannot be cancelled")
    lv.status = "CANCELLED"
    audit(db, user, "LEAVE_CANCEL", f"{emp.employee_code} {lv.leave_type} {lv.start_date}..{lv.end_date}", commit=False)
    db.commit()
    return go("/me#leave", msg="Request cancelled")


@app.get("/me/payslip/{run_id}.pdf")
def me_payslip(run_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    emp = _me(db, user)
    run = db.get(PayrollRun, run_id)
    line = next((l for l in run.lines if l.employee_id == emp.id), None) if run and run.status == "FINAL" else None
    if not line:
        return go("/me#pay", err="Payslip not available")
    audit(db, user, "PAYSLIP_SELF", f"{run.year}-{run.month:02d}")
    return Response(payslips_pdf(run, [line]), media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="payslip-{emp.employee_code}-{run.year}-{run.month:02d}.pdf"'})


@app.get("/me/password", response_class=HTMLResponse)
def me_password_page(request: Request, user=Depends(page_user)):
    return render(request, "me_password.html", user)


@app.post("/me/password")
def me_password_save(request: Request, current: str = Form(...), new: str = Form(...), new2: str = Form(...),
                     user=Depends(page_user), db: Session = Depends(get_db)):
    """Any logged-in user changes their own password (Staff must do this after the first login)."""
    u = db.scalar(select(User).where(User.username == user["username"]))
    if not u or not verify_password(current, u.password_hash):
        return go("/me/password", err="Current password is wrong")
    if new != new2:
        return go("/me/password", err="The two new passwords are not the same")
    if len(new) < 8 or new == staff_default_password(db) or new.lower() in (u.username.lower(), "password"):
        return go("/me/password", err="Choose a new password of 8+ characters, not the default and not your ID")
    u.password_hash, u.must_change = hash_password(new), False
    audit(db, user, "PASSWORD_CHANGE", "own password", commit=False)
    db.commit()
    return go("/me" if "self.service" in user["perms"] else "/", msg="Password changed")


@app.post("/users/staff-login")
def staff_login_settings(enabled: str = Form(""), default_password: str = Form(""), reset_code: str = Form(""),
                         user=Depends(page_user), db: Session = Depends(get_db)):
    """Admin: staff login on/off, the default password, and resetting one employee back to the default."""
    need(user, "users.manage")
    if reset_code.strip():
        u = db.scalar(select(User).where(User.username == reset_code.strip()))
        if not u or u.role != "STAFF":
            return go("/users#staff", err=f"No staff login for {reset_code.strip()} yet - they can log in with the "
                                           "default password")
        u.password_hash, u.must_change, u.active = hash_password(staff_default_password(db)), True, True
        audit(db, user, "STAFF_PASSWORD_RESET", u.username, commit=False)
        db.commit()
        return go("/users#staff", msg=f"{u.username}: password set back to the default - they must change it at login")
    pw = default_password.strip() or staff_default_password(db)
    if len(pw) < 6:
        return go("/users#staff", err="Default password needs 6+ characters")
    set_setting(db, "staff_login", "1" if enabled else "0", commit=False)
    set_setting(db, "staff_default_password", pw, commit=False)
    audit(db, user, "STAFF_LOGIN_SETTINGS", f"enabled={bool(enabled)}", commit=False)
    db.commit()
    return go("/users#staff", msg="Staff login settings saved")


@app.get("/punches/photos", response_class=HTMLResponse)
def punch_photo_gallery(request: Request, start: str = "", end: str = "", q: str = "", company: str = "",
                        outside: str = "", page: int = 1, user=Depends(page_user), db: Session = Depends(get_db)):
    """All camera photos taken with mobile punches: who, when, where."""
    need(user, "punches.view")
    s_, e_ = _dates(start or str(now_local().date() - timedelta(days=6)), end or str(now_local().date()))
    stmt = (select(AttendanceLog, Employee).outerjoin(Employee, Employee.card_no == AttendanceLog.card_no)
            .where(AttendanceLog.photo.is_not(None),
                   AttendanceLog.punch_time >= datetime.combine(s_, time.min),
                   AttendanceLog.punch_time < datetime.combine(e_ + timedelta(days=1), time.min)))
    cid = company_filter(user, company)
    if cid:
        stmt = stmt.where(Employee.company_id == cid)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(AttendanceLog.card_no == q.strip(), Employee.employee_code == q.strip(),
                              Employee.full_name.ilike(like)))
    if outside:
        stmt = stmt.where(AttendanceLog.note.ilike("%OUTSIDE%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    per = 60
    rows = db.execute(stmt.order_by(AttendanceLog.punch_time.desc()).offset((max(page, 1) - 1) * per).limit(per)).all()
    return render(request, "punch_photos.html", user, rows=rows, total=total, page=max(page, 1),
                  pages=max(1, -(-total // per)), start=s_, end=e_, q=q, company=cid or "", outside=outside,
                  companies=visible_companies(db, user), data_dir=str(DATA_DIR))


@app.get("/punches/{log_id}/photo")
def punch_photo(log_id: int, user=Depends(page_user), db: Session = Depends(get_db)):
    """The camera photo taken with a mobile punch."""
    need(user, "punches.view")
    log = db.get(AttendanceLog, log_id)
    if not log or not log.photo or ".." in log.photo:
        raise HTTPException(404, "No photo")
    if user.get("company_id"):
        e = db.scalar(select(Employee).where(Employee.card_no == log.card_no))
        check_company(user, e.company_id if e else None)
    path = DATA_DIR / "punch_photos" / log.photo
    if not path.exists():
        raise HTTPException(404, "The photo was deleted")
    return FileResponse(path, media_type="image/jpeg")


# ---------------------------------------------------------------- storage & cleanup (Admin)
def _dir_size(path) -> tuple[int, int]:
    n = size = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                n += 1
                size += f.stat().st_size
    return n, size


@app.get("/cleanup", response_class=HTMLResponse)
def cleanup_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "backup.manage")
    folders = [("punch_photos", "Mobile punch photos"), ("photos", "Employee photos & signatures"),
               ("documents", "Employee documents & letters"), ("backups", "Backups")]
    usage = [(label, *_dir_size(DATA_DIR / d)) for d, label in folders]
    first = db.scalar(select(func.min(AttendanceLog.punch_time)).where(AttendanceLog.photo.is_not(None)))
    left_photos = db.scalar(select(func.count(Employee.id)).where(Employee.employment_status != "ACTIVE",
                                                                  or_(Employee.photo.is_not(None),
                                                                      Employee.signature.is_not(None)))) or 0
    return render(request, "cleanup.html", user, usage=usage, first=first, left_photos=left_photos,
                  photo_required=mobile.photo_required(db), today=now_local().date())


@app.post("/cleanup/punch-photos")
def cleanup_punch_photos(start: str = Form(""), end: str = Form(...), employee_code: str = Form(""),
                         confirm: str = Form(""), user=Depends(page_user), db: Session = Depends(get_db)):
    """Delete mobile punch photos (the punches themselves stay)."""
    need(user, "data.delete")
    if confirm != "DELETE":
        return go("/cleanup", err="Type DELETE to confirm")
    try:
        s = parse_day(start) if start else date(2000, 1, 1)
        e = parse_day(end)
    except ValueError:
        return go("/cleanup", err="Give valid dates")
    stmt = select(AttendanceLog).where(AttendanceLog.photo.is_not(None),
                                       AttendanceLog.punch_time >= datetime.combine(s, time.min),
                                       AttendanceLog.punch_time < datetime.combine(e + timedelta(days=1), time.min))
    if employee_code.strip():
        emp = db.scalar(select(Employee).where(Employee.employee_code == employee_code.strip()))
        if not emp:
            return go("/cleanup", err=f"No employee {employee_code.strip()}")
        stmt = stmt.where(AttendanceLog.card_no == emp.card_no)
    n = 0
    for lg in db.scalars(stmt):
        if ".." not in lg.photo:
            (DATA_DIR / "punch_photos" / lg.photo).unlink(missing_ok=True)
        lg.photo = None
        n += 1
    audit(db, user, "PUNCH_PHOTOS_DELETE", f"{s}..{e} {employee_code.strip() or 'all'}: {n} photo(s)", commit=False)
    db.commit()
    return go("/cleanup", msg=f"{n} punch photo(s) deleted - the punches are kept")


@app.post("/cleanup/left-photos")
def cleanup_left_photos(confirm: str = Form(""), user=Depends(page_user), db: Session = Depends(get_db)):
    """Delete photos and signatures of employees who have left."""
    need(user, "data.delete")
    if confirm != "DELETE":
        return go("/cleanup", err="Type DELETE to confirm")
    from services.photos import PHOTO_DIR
    n = 0
    for e in db.scalars(select(Employee).where(Employee.employment_status != "ACTIVE")):
        for attr in ("photo", "signature"):
            name = getattr(e, attr)
            if name:
                if "/" not in name and ".." not in name:
                    (PHOTO_DIR / name).unlink(missing_ok=True)
                setattr(e, attr, None)
                n += 1
    audit(db, user, "LEFT_PHOTOS_DELETE", f"{n} file(s)", commit=False)
    db.commit()
    return go("/cleanup", msg=f"{n} photo / signature file(s) of former employees deleted")


@app.post("/cleanup/settings")
def cleanup_settings(photo_required: str = Form(""), user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "backup.manage")
    set_setting(db, "mobile_photo_required", "1" if photo_required else "0")
    audit(db, user, "SETTINGS_SAVE", f"mobile punch photo required={bool(photo_required)}")
    return go("/cleanup", msg="Saved")


# ------------------------------------------------------------ HTTPS
@app.get("/https", response_class=HTMLResponse)
def https_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "backup.manage")
    info = maintenance.cert_info()
    host = request.url.hostname or maintenance.lan_ips()[0]
    return render(request, "https.html", user, info=info, host=host, port=settings.port, https_port=settings.https_port,
                  on_https=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
                  force=force_https(db), names="\n".join(maintenance.extra_names()), ips=maintenance.lan_ips())


@app.get("/https/ca.crt")
def https_ca():
    """The server's own certificate authority - install it once on each PC / phone (public, no login)."""
    return Response(maintenance.ca_der(), media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="LailaPayroll-CA.crt"'})


@app.get("/https/install-ca.bat")
def https_ca_bat(request: Request):
    """One double-click for an office PC: downloads and trusts the CA (asks for Administrator)."""
    host = request.url.hostname or maintenance.lan_ips()[0]
    bat = ("@echo off\r\n:: SPhere Payroll - trust this server's HTTPS certificate\r\n"
           "net session >nul 2>&1 || (powershell -Command \"Start-Process '%~f0' -Verb RunAs\" & exit /b)\r\n"
           "powershell -Command \""
           f"(New-Object Net.WebClient).DownloadFile('http://{host}:{settings.port}/https/ca.crt', '%TEMP%\\LailaPayroll-CA.crt')\"\r\n"
           "certutil -addstore -f Root \"%TEMP%\\LailaPayroll-CA.crt\"\r\n"
           f"echo.\r\necho Done. Close the browser and open https://{host}:{settings.https_port}/\r\npause\r\n")
    return Response(bat.encode(), media_type="application/octet-stream",
                    headers={"Content-Disposition": 'attachment; filename="Install-LailaPayroll-Certificate.bat"'})


@app.post("/https/settings")
def https_settings(request: Request, force: str = Form(""), names: str = Form(""), user=Depends(page_user),
                   db: Session = Depends(get_db)):
    need(user, "backup.manage")
    on_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    if force and not on_https:
        return go("/https", err=f"Open this page once over HTTPS (https://{request.url.hostname}:{settings.https_port}/https) "
                                "and check it works before switching everyone to HTTPS")
    clean = []
    for n in re.split(r"[\s,;]+", names.lower()):
        n = n.strip().strip("/").removeprefix("https://").removeprefix("http://").split(":")[0]
        if n and re.fullmatch(r"[a-z0-9.-]{1,120}", n) and n not in clean:
            clean.append(n)
    old_names = maintenance.extra_names()
    maintenance.set_extra_names(clean[:20])
    set_setting(db, "force_https", "1" if force else "0")
    _state["force_https"] = bool(force)
    audit(db, user, "HTTPS_SETTINGS", f"force={bool(force)} names={','.join(clean) or '-'}")
    if clean != old_names:
        return go("/https", msg="Saved. The new names need a new server certificate - click 'Make new certificate & "
                                "restart' below")
    return go("/https", msg="Saved" + (" - office PCs are now sent to HTTPS" if force else ""))


@app.post("/https/renew")
def https_renew(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "backup.manage")
    maintenance.ensure_certificate(force=True)
    audit(db, user, "HTTPS_CERT_RENEW", ", ".join(maintenance.cert_info().get("ips", [])))
    page = render(request, "restarting.html", None, heading="Restarting with the new certificate…",
                  note="The new HTTPS certificate is used after the restart.", next_url="/https")
    backups.restart_program()
    return page


# ------------------------------------------------------------ Delete centre (Super Admin)
def _dd_allowed(db: Session, user: dict):
    return None if not user.get("company_id") else [c.id for c in visible_companies(db, user)]


def _dd_params(src) -> dict:
    return {k: str(src.get(k) or "").strip() for k in ("q", "start", "end", "company") if src.get(k)}


@app.get("/delete", response_class=HTMLResponse)
def delete_centre(request: Request, s: str = "", page: int = 1, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "data.delete")
    allowed = _dd_allowed(db, user)
    counts = {sec.key: dd.count(db, sec, None, allowed) for sec in dd.SECTIONS}
    sec = dd.BY_KEY.get(s)
    p = _dd_params(request.query_params)
    ctx = {}
    if sec:
        try:
            total = dd.count(db, sec, p, allowed)
            per = 100
            pages = max(1, -(-total // per))
            page = min(max(page, 1), pages)
            ctx = dict(rows=dd.rows(db, sec, p, allowed, page, per), total=total, page=page, pages=pages)
        except ValueError as exc:
            return go(f"/delete?s={sec.key}", err=str(exc))
    qs = urlencode({**p, "s": s})
    return render(request, "delete_centre.html", user, sections=dd.SECTIONS, groups=dd.GROUPS, counts=counts, sec=sec,
                  p=p, qs=qs, companies=visible_companies(db, user), **ctx)


@app.post("/delete/{key}")
async def delete_centre_run(request: Request, key: str, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "data.delete")
    sec = dd.BY_KEY.get(key)
    if not sec:
        raise HTTPException(404, "Unknown section")
    f = await request.form()
    p = _dd_params(f)
    back = f"/delete?{urlencode({**p, 's': key})}"
    scope = str(f.get("scope") or "selected")
    opts = {"with_final": bool(f.get("with_final")), "with_punches": bool(f.get("with_punches")), "me": user["username"]}
    if scope == "all":
        if str(f.get("confirm") or "").strip() != "DELETE":
            return go(back, err="To delete everything that matches, type DELETE in the box")
        ids = None
    else:
        ids = [int(x) for x in f.getlist("ids") if str(x).isdigit()]
        if not ids:
            return go(back, err="Tick the items to delete first")
    done, kept = dd.run_delete(db, sec, ids, p, opts, _dd_allowed(db, user))
    word = "cleared" if sec.clears else "deleted"
    msg = f"{sec.label}: {done} {word}"
    if kept:
        msg += f". {len(kept)} kept - " + "; ".join(kept[:8]) + (" ..." if len(kept) > 8 else "")
    audit(db, user, "DELETE_" + key.upper(), f"{'all matching ' + str(p) if ids is None else str(len(ids)) + ' ticked'}"
          f": {msg}", commit=False)
    db.commit()
    if key in ("users", "companies"):
        _state.pop("setup_done", None)
    return go(back, msg=msg) if not kept or done else go(back, err=msg)


# ------------------------------------------------------------ bulk actions (Admin)
BULK_ACTIONS = {
    "mark_left": "Mark as left (inactive)",
    "photos": "Delete profile photos & signatures",
    "punch_photos": "Delete mobile punch photos (punches stay)",
    "documents": "Delete uploaded documents & saved letters",
    "payslips": "Delete payslips (salary lines) of a month range",
    "punches": "Delete punch records & processed attendance",
    "delete": "Delete the employees completely",
}


def _bulk_emps(db: Session, user: dict, p) -> list[Employee]:
    stmt = select(Employee).order_by(Employee.company_id, Employee.employee_code)
    cids = [c.id for c in visible_companies(db, user)]
    stmt = stmt.where(Employee.company_id.in_(cids or [0]))
    if str(p.get("company") or "").isdigit():
        stmt = stmt.where(Employee.company_id == int(p["company"]))
    if p.get("status") in ("ACTIVE", "INACTIVE"):
        stmt = stmt.where(Employee.employment_status == p["status"]) if p["status"] == "ACTIVE" else \
            stmt.where(Employee.employment_status != "ACTIVE")
    if str(p.get("department") or "").isdigit():
        stmt = stmt.where(Employee.department_id == int(p["department"]))
    if p.get("left_before"):
        stmt = stmt.where(Employee.employment_status != "ACTIVE", Employee.left_on.is_not(None),
                          Employee.left_on < parse_day(p["left_before"]))
    codes = [c for c in re.split(r"[\s,;]+", str(p.get("codes") or "")) if c]
    if codes:
        stmt = stmt.where(or_(Employee.employee_code.in_(codes), Employee.card_no.in_(codes)))
    if p.get("q"):
        q = f"%{str(p['q']).strip()}%"
        stmt = stmt.where(or_(Employee.employee_code.ilike(q), Employee.full_name.ilike(q), Employee.card_no.ilike(q)))
    return list(db.scalars(stmt.limit(3000)))


@app.get("/bulk", response_class=HTMLResponse)
def bulk_page(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    need(user, "data.delete")
    p = dict(request.query_params)
    searched = any(p.get(k) for k in ("company", "status", "department", "left_before", "codes", "q"))
    emps = _bulk_emps(db, user, p) if searched else []
    final_ids = set()
    if emps:
        final_ids = set(db.scalars(select(PayrollLine.employee_id).join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
                                   .where(PayrollRun.status == "FINAL",
                                          PayrollLine.employee_id.in_([e.id for e in emps]))))
    first_punch = db.scalar(select(func.min(AttendanceLog.punch_time)))
    runs = db.execute(select(PayrollRun.year, PayrollRun.month, func.count(PayrollRun.id))
                      .group_by(PayrollRun.year, PayrollRun.month)
                      .order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).limit(36)).all()
    return render(request, "bulk.html", user, p=p, searched=searched, emps=emps, final_ids=final_ids,
                  actions=BULK_ACTIONS, companies=visible_companies(db, user),
                  departments=db.scalars(select(Department).order_by(Department.name)).all(),
                  first_punch=first_punch, runs=runs, today=now_local().date())


def _month_range(f) -> tuple[int, int]:
    y1, m1 = parse_month(str(f.get("month_from") or ""))
    y2, m2 = parse_month(str(f.get("month_to") or "")) if f.get("month_to") else (y1, m1)
    a, b = sorted((y1 * 12 + m1, y2 * 12 + m2))
    return a, b


@app.post("/bulk/employees")
async def bulk_employees(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    """Admin: one action on many selected employees."""
    need(user, "data.delete")
    f = await request.form()
    back = "/bulk?" + str(f.get("back_qs") or "")
    action = str(f.get("action") or "")
    if action not in BULK_ACTIONS:
        return go(back, err="Choose what to do")
    ids = [int(x) for x in f.getlist("ids") if str(x).isdigit()]
    emps = [e for e in db.scalars(select(Employee).where(Employee.id.in_(ids or [0])))]
    allowed = {c.id for c in visible_companies(db, user)}
    emps = [e for e in emps if e.company_id in allowed]
    if not emps:
        return go(back, err="Tick at least one employee")
    if action != "mark_left" and str(f.get("confirm") or "").strip() != "DELETE":
        return go(back, err="Type DELETE to confirm")
    with_final = bool(f.get("with_final"))
    if action in ("payslips", "delete") and with_final:
        need(user, "payroll.edit")
    n, skipped = 0, []
    if action == "mark_left":
        when = parse_day(str(f.get("left_on"))) if f.get("left_on") else now_local().date()
        for e in emps:
            if e.employment_status == "ACTIVE":
                e.employment_status, e.left_on, n = "INACTIVE", e.left_on or when, n + 1
        msg = f"{n} employee(s) marked as left on {when:%d/%m/%Y}"
    elif action == "photos":
        for e in emps:
            n += _unlink_photo(e.photo) + _unlink_photo(e.signature)
            e.photo = e.signature = None
        msg = f"{n} photo / signature file(s) deleted"
    elif action == "punch_photos":
        cards = [e.card_no for e in emps if e.card_no]
        for lg in db.scalars(select(AttendanceLog).where(AttendanceLog.card_no.in_(cards or ["-"]),
                                                         AttendanceLog.photo.is_not(None))):
            n += _unlink_punch_photo(lg.photo)
            lg.photo = None
        msg = f"{n} mobile punch photo(s) deleted - the punches are kept"
    elif action == "documents":
        for d in db.scalars(select(EmployeeDocument).where(EmployeeDocument.employee_id.in_([e.id for e in emps]))):
            docs.delete_file(d.stored_name)
            db.delete(d)
            n += 1
        msg = f"{n} document(s) deleted"
    elif action == "payslips":
        a, b = _month_range(f)
        runs = {r.id: r for r in db.scalars(select(PayrollRun).where(
            (PayrollRun.year * 12 + PayrollRun.month).between(a, b)))}
        for ln in db.scalars(select(PayrollLine).where(PayrollLine.run_id.in_(list(runs) or [0]),
                                                       PayrollLine.employee_id.in_([e.id for e in emps]))):
            if runs[ln.run_id].status == "FINAL" and not with_final:
                skipped.append(ln.employee_id)
                continue
            db.delete(ln)
            n += 1
        db.flush()
        for r in runs.values():                         # a payroll with nobody left on it goes too
            if not db.scalar(select(func.count(PayrollLine.id)).where(PayrollLine.run_id == r.id)):
                db.delete(r)
        msg = f"{n} payslip(s) deleted ({_ym_txt(a)} - {_ym_txt(b)})"
        if skipped:
            msg += f"; {len(skipped)} finalized payslip(s) kept - tick 'including finalized' to delete them too"
    elif action == "punches":
        cards = [e.card_no for e in emps if e.card_no]
        for lg in db.scalars(select(AttendanceLog).where(AttendanceLog.card_no.in_(cards or ["-"]))):
            _unlink_punch_photo(lg.photo)
            db.delete(lg)
            n += 1
        db.execute(delete(DailyAttendance).where(DailyAttendance.employee_id.in_([e.id for e in emps])))
        msg = f"{n} punch record(s) and the processed attendance deleted"
    else:                                               # delete
        punches = bool(f.get("with_punches"))
        for e in emps:
            res = _wipe_employee(db, e, with_punches=punches, with_final=with_final)
            if res["final"]:
                skipped.append(f"{res['code']}")
            else:
                n += 1
        msg = f"{n} employee(s) deleted"
        if skipped:
            msg += (f"; {len(skipped)} kept because they are on a finalized payroll ({', '.join(skipped[:15])}"
                    f"{' ...' if len(skipped) > 15 else ''}) - mark them as left, or tick 'including finalized payslips'")
    audit(db, user, "BULK_" + action.upper(), f"{len(emps)} selected: {msg}", commit=False)
    db.commit()
    return go(back, msg=msg)


def _ym_txt(k: int) -> str:
    y, m = divmod(k - 1, 12)
    return f"{month_abbr[m + 1]} {y}"


@app.post("/bulk/payroll")
async def bulk_payroll(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    """Admin: delete whole payrolls (all payslips) of a month range, one or all companies."""
    need(user, "data.delete")
    f = await request.form()
    if str(f.get("confirm") or "").strip() != "DELETE":
        return go("/bulk#payroll", err="Type DELETE to confirm")
    a, b = _month_range(f)
    allowed = [c.id for c in visible_companies(db, user)]
    stmt = select(PayrollRun).where((PayrollRun.year * 12 + PayrollRun.month).between(a, b),
                                    PayrollRun.company_id.in_(allowed or [0]))
    if str(f.get("company") or "").isdigit():
        stmt = stmt.where(PayrollRun.company_id == int(f["company"]))
    n = kept = lines = 0
    for r in db.scalars(stmt):
        if r.status == "FINAL" and not f.get("with_final"):
            kept += 1
            continue
        lines += len(r.lines)
        if f.get("with_inputs"):
            db.execute(delete(PayrollAdjustment).where(PayrollAdjustment.year == r.year,
                                                       PayrollAdjustment.month == r.month,
                                                       PayrollAdjustment.employee_id.in_(
                                                           select(Employee.id).where(Employee.company_id == r.company_id))))
        db.delete(r)
        n += 1
    msg = f"{n} payroll(s) with {lines} payslip(s) deleted ({_ym_txt(a)} - {_ym_txt(b)})"
    if kept:
        msg += f"; {kept} finalized payroll(s) kept - tick 'including finalized' to delete them"
    audit(db, user, "BULK_PAYROLL_DELETE", msg, commit=False)
    db.commit()
    return go("/bulk#payroll", msg=msg)


@app.post("/bulk/old-data")
async def bulk_old_data(request: Request, user=Depends(page_user), db: Session = Depends(get_db)):
    """Admin: delete old records before a date to keep the database small (make a backup first)."""
    need(user, "data.delete")
    f = await request.form()
    if str(f.get("confirm") or "").strip() != "DELETE":
        return go("/bulk#old", err="Type DELETE to confirm")
    try:
        before = parse_day(str(f.get("before") or ""))
    except ValueError as exc:
        return go("/bulk#old", err=str(exc))
    if before > now_local().date() - timedelta(days=31):
        return go("/bulk#old", err="Choose a date at least one month back")
    cut = datetime.combine(before, time.min)
    done = []
    if f.get("punches"):
        n = 0
        for lg in db.scalars(select(AttendanceLog).where(AttendanceLog.punch_time < cut)):
            _unlink_punch_photo(lg.photo)
            db.delete(lg)
            n += 1
        done.append(f"{n} punch record(s)")
    if f.get("attendance"):
        n = db.execute(delete(DailyAttendance).where(DailyAttendance.work_date < before)).rowcount
        done.append(f"{n} processed attendance day(s)")
    if f.get("audit"):
        n = db.execute(delete(AuditLog).where(AuditLog.created_at < cut)).rowcount
        done.append(f"{n} activity log line(s)")
    if f.get("errors"):
        n = db.execute(delete(ErrorLog).where(ErrorLog.created_at < cut)).rowcount
        done.append(f"{n} error log line(s)")
    if not done:
        return go("/bulk#old", err="Tick what to delete")
    msg = f"Before {before:%d/%m/%Y}: " + ", ".join(done) + " deleted"
    audit(db, user, "BULK_OLD_DATA_DELETE", msg, commit=False)
    db.commit()
    return go("/bulk#old", msg=msg)


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
                   labels=leave_svc.STATUS_LABEL, today=now_local().date(), tab=request.query_params.get("tab", ""),
                   notices=change_svc.notices_for(db, emp),
                   slips=db.execute(select(PayrollLine, PayrollRun).join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
                                    .where(PayrollLine.employee_id == emp.id, PayrollRun.status == "FINAL")
                                    .order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).limit(12)).all())
    return render(request, "mobile.html", None, emp=emp, punches=mobile.today_punches(db, emp) if emp else [], **ctx)


@app.post("/m/login")
def mobile_login(request: Request, employee_code: str = Form(...), pin: str = Form(...),
                 db: Session = Depends(get_db)):
    try:
        emp = mobile.login(db, employee_code, pin)
    except mobile.MobileError as exc:
        # also the employee's own website password (only after they changed it from the default)
        u = db.scalar(select(User).where(User.username == employee_code.strip(), User.role == "STAFF",
                                         User.active == True, User.must_change == False))  # noqa: E712
        emp = db.get(Employee, u.employee_id) if u and verify_password(pin, u.password_hash) else None
        if not emp or emp.employment_status != "ACTIVE":
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
async def mobile_punch(request: Request, latitude: float = Form(...), longitude: float = Form(...),
                       accuracy: float = Form(...), photo: UploadFile | None = File(None),
                       db: Session = Depends(get_db)):
    emp = mobile_employee(request, db)
    if not emp:
        return JSONResponse({"ok": False, "message": "Please log in again"}, status_code=401)
    try:
        log = mobile.punch(db, emp, latitude, longitude, accuracy, client_ip=request.client.host if request.client else None,
                           photo=await photo.read() if photo is not None else None)
    except mobile.MobileError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    return {"ok": True, "message": f"Punch recorded at {log.punch_time:%I:%M %p}", "time": f"{log.punch_time:%H:%M}",
            "location": f"{log.latitude:.6f}, {log.longitude:.6f} (±{log.accuracy_m} m) · {log.note}",
            "map": f"https://maps.google.com/?q={log.latitude:.6f},{log.longitude:.6f}"}


@app.get("/m/payslip/{run_id}.pdf")
def mobile_payslip(request: Request, run_id: int, db: Session = Depends(get_db)):
    """An employee's own payslip (finalized months only) from the phone."""
    emp = mobile_employee(request, db)
    if not emp:
        return go("/m", err="Please log in again")
    run = db.get(PayrollRun, run_id)
    line = next((l for l in run.lines if l.employee_id == emp.id), None) if run and run.status == "FINAL" else None
    if not line:
        return go("/m?tab=pay", err="Payslip not available")
    audit(db, f"mobile:{emp.employee_code}", "PAYSLIP_SELF", f"{run.year}-{run.month:02d}")
    return Response(payslips_pdf(run, [line]), media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="payslip-{emp.employee_code}-{run.year}-{run.month:02d}.pdf"'})


async def _request_leave(db, emp: Employee, f, actor: str) -> tuple[bool, str]:
    """Employee's own leave request (phone or My page): checks the rules, then starts the approval workflow."""
    lt = db.scalar(select(LeaveType).where(LeaveType.code == str(f.get("leave_type") or "")))
    try:
        if not lt or not lt.self_request:
            raise ValueError("this leave type cannot be requested here - ask HR")
        s, e = parse_day(str(f.get("start_date"))), parse_day(str(f.get("end_date")))
        if e < now_local().date() - timedelta(days=30):
            raise ValueError("leave more than 30 days in the past must be entered by HR")
        reason = str(f.get("reason") or "").strip()
        if not reason:
            raise ValueError("write a reason")
        has_doc = bool(getattr(f.get("document"), "filename", ""))
        days = leave_svc.validate(db, emp, lt, s, e, has_doc)
        stored = await _leave_document(f, emp)
    except ValueError as exc:
        return False, str(exc)[:300]
    status = leave_svc.first_status(db, emp)
    db.add(Leave(employee_id=emp.id, start_date=s, end_date=e, leave_type=lt.code, paid=lt.paid, days=days,
                 status=status, source="SELF", reason=reason[:300], note=reason[:250], document=stored,
                 requested_by=emp.employee_code, requested_at=now_local()))
    audit(db, actor, "LEAVE_REQUEST", f"{emp.employee_code} {lt.code} {s}..{e} ({days} days) -> {status}", commit=False)
    db.commit()
    return True, f"Leave request sent - {leave_svc.STATUS_LABEL[status].lower()}"


@app.post("/m/leave")
async def mobile_leave(request: Request, db: Session = Depends(get_db)):
    """Employee requests leave from the phone; it goes to the approval workflow."""
    emp = mobile_employee(request, db)
    if not emp or emp.employment_status != "ACTIVE":
        return go("/m", err="Please log in again")
    ok_, msg = await _request_leave(db, emp, await request.form(), f"mobile:{emp.employee_code}")
    return go("/m?tab=leave", msg=msg if ok_ else "", err="" if ok_ else msg)


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

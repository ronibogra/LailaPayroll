"""Backup & restore.

A backup is one ZIP: the database, employee photos, Head Office logo, the secret key (needed for logins and
machine passwords), optional settings.env and the HTTPS certificate, plus a manifest.
Restore: the ZIP is checked, saved as restore_pending.zip and the program restarts; on start (run.py) the
current data is moved to a 'before_restore_...' folder and the backup is put in place."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time as _time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from config import DATA_DIR, settings

DB_FILE = DATA_DIR / "laila_payroll.db"
PENDING = DATA_DIR / "restore_pending.zip"
EXTRA_FILES = ("secret.key", "settings.env", "https-cert.pem", "https-key.pem")
EXTRA_DIRS = ("photos", "branding", "documents", "punch_photos")
PREFIX = "LailaPayroll_backup_"
APP_VERSION = "3.2.0"


def backup_dir(db=None) -> Path:
    from services.policy import get_setting
    custom = ""
    if db is not None:
        custom = get_setting(db, "backup_dir", default="") or ""
    d = Path(custom) if custom else DATA_DIR / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db_copy(dest: Path) -> None:
    with sqlite3.connect(DB_FILE) as a, sqlite3.connect(dest) as b:
        a.backup(b)


def create_backup(db, reason: str = "manual", username: str = "system") -> Path:
    if not settings.db_url.startswith("sqlite"):
        raise RuntimeError("The program uses SQL Server - back up that database with SQL Server tools. "
                           "Photos and settings are still included in this ZIP.")
    out_dir = backup_dir(db)
    name = f"{PREFIX}{datetime.now():%Y%m%d_%H%M%S}_{reason}.zip"
    tmp = Path(tempfile.mkdtemp(prefix="laila_bk_"))
    try:
        _db_copy(tmp / "laila_payroll.db")
        counts = {}
        with sqlite3.connect(tmp / "laila_payroll.db") as c:
            for t in ("employees", "attendance_logs", "payroll_runs", "companies", "users", "devices"):
                try:
                    counts[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.Error:
                    counts[t] = 0
        from services.branding import BRAND
        manifest = {"app": "LailaPayroll", "version": APP_VERSION, "created": datetime.now().isoformat(sep=" "),
                    "reason": reason, "by": username, "head_office": BRAND.get("name"), "counts": counts}
        part = out_dir / (name + ".part")
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
            z.write(tmp / "laila_payroll.db", "laila_payroll.db")
            for f in EXTRA_FILES:
                if (DATA_DIR / f).exists():
                    z.write(DATA_DIR / f, f)
            for d in EXTRA_DIRS:
                for f in (DATA_DIR / d).rglob("*") if (DATA_DIR / d).exists() else []:
                    if f.is_file():
                        z.write(f, f.relative_to(DATA_DIR).as_posix())
        part.replace(out_dir / name)
        return out_dir / name
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def list_backups(db) -> list[dict]:
    out = []
    for f in sorted(backup_dir(db).glob(f"{PREFIX}*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        info = {"name": f.name, "size": f.stat().st_size, "time": datetime.fromtimestamp(f.stat().st_mtime),
                "manifest": {}}
        try:
            with zipfile.ZipFile(f) as z:
                info["manifest"] = json.loads(z.read("manifest.json"))
        except Exception:  # noqa: BLE001
            info["manifest"] = {"error": "not readable"}
        out.append(info)
    return out


def safe_path(db, name: str) -> Path:
    if "/" in name or "\\" in name or ".." in name or not name.startswith(PREFIX) or not name.endswith(".zip"):
        raise ValueError("invalid backup name")
    p = backup_dir(db) / name
    if not p.exists():
        raise ValueError("backup not found")
    return p


def validate(path: Path) -> dict:
    """Checks the ZIP really is a LailaPayroll backup with a healthy database. Returns its manifest."""
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ValueError("this is not a ZIP file")
    with z:
        names = set(z.namelist())
        if "manifest.json" not in names or "laila_payroll.db" not in names:
            raise ValueError("this ZIP is not a LAILA GROUP Payroll backup")
        for n in names:
            if n.startswith("/") or ".." in n.split("/"):
                raise ValueError("unsafe file name inside the backup")
        manifest = json.loads(z.read("manifest.json"))
        tmp = Path(tempfile.mkdtemp(prefix="laila_chk_"))
        try:
            z.extract("laila_payroll.db", tmp)
            with sqlite3.connect(tmp / "laila_payroll.db") as c:
                ok = c.execute("PRAGMA integrity_check").fetchone()[0]
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if ok != "ok":
                raise ValueError(f"the database in this backup is damaged ({ok})")
            if not {"employees", "users", "companies"} <= tables:
                raise ValueError("the database in this backup is incomplete")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return manifest


def cleanup(db) -> int:
    from services.policy import get_setting
    keep = int(get_setting(db, "backup_keep_days", default=str(settings.backup_keep_days)) or 30)
    cutoff = datetime.now() - timedelta(days=keep)
    n = 0
    for f in backup_dir(db).glob(f"{PREFIX}*_auto.zip"):     # only automatic backups are deleted
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink(missing_ok=True)
            n += 1
    return n


# ------------------------------------------------------------ restore (applied at program start)
def schedule_restore(path: Path) -> None:
    shutil.copy2(path, PENDING)


def apply_pending_restore(log=print) -> bool:
    """Called by run.py BEFORE the database is opened."""
    if not PENDING.exists():
        return False
    try:
        validate(PENDING)
    except Exception as exc:  # noqa: BLE001
        log(f"Restore cancelled - backup not valid: {exc}")
        PENDING.rename(DATA_DIR / f"restore_failed_{datetime.now():%Y%m%d_%H%M%S}.zip")
        return False
    safety = DATA_DIR / f"before_restore_{datetime.now():%Y%m%d_%H%M%S}"
    safety.mkdir()
    for f in ("laila_payroll.db", "laila_payroll.db-wal", "laila_payroll.db-shm") + EXTRA_FILES:
        if (DATA_DIR / f).exists():
            shutil.move(str(DATA_DIR / f), safety / f)
    for d in EXTRA_DIRS:
        if (DATA_DIR / d).exists():
            shutil.move(str(DATA_DIR / d), safety / d)
    with zipfile.ZipFile(PENDING) as z:
        for n in z.namelist():
            if n == "manifest.json":
                continue
            target = DATA_DIR / n
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    # keep the current HTTPS certificate if the backup had none
    for f in ("https-cert.pem", "https-key.pem"):
        if not (DATA_DIR / f).exists() and (safety / f).exists():
            shutil.copy2(safety / f, DATA_DIR / f)
    PENDING.unlink()
    log(f"Backup restored. Previous data kept in {safety}")
    return True


def restart_program(delay: float = 1.5) -> None:
    """Start a fresh copy of the program (it waits for the port) and end this one."""
    import subprocess

    def go():
        _time.sleep(delay)
        args = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable] + sys.argv
        env = dict(os.environ, LAILA_WAIT_FOR_PORT="1")
        flags = 0
        if sys.platform == "win32":
            flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, env=env, cwd=os.getcwd(), creationflags=flags, close_fds=True)
        os._exit(0)
    threading.Thread(target=go, daemon=True).start()


# ------------------------------------------------------------ automatic daily backup
def start_scheduler() -> None:
    from database import SessionLocal
    from services.errors import log_error
    from services.policy import get_setting, set_setting

    def loop():
        _time.sleep(60)
        while True:
            db = SessionLocal()
            try:
                at = get_setting(db, "backup_time", default="02:00") or "02:00"
                enabled = (get_setting(db, "backup_auto", default="1") or "1") == "1"
                last = get_setting(db, "backup_last_auto", default="") or ""
                now = datetime.now()
                hh, mm = (int(x) for x in at.split(":")[:2])
                due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if enabled and now >= due and last != now.strftime("%Y-%m-%d"):
                    create_backup(db, "auto")
                    set_setting(db, "backup_last_auto", now.strftime("%Y-%m-%d"))
                    cleanup(db)
            except Exception as exc:  # noqa: BLE001
                log_error("BACKUP", f"automatic backup failed: {exc}", exc=exc)
                try:   # report once a day, not every minute
                    set_setting(db, "backup_last_auto", datetime.now().strftime("%Y-%m-%d"))
                except Exception:  # noqa: BLE001
                    db.rollback()
            finally:
                db.close()
            _time.sleep(60)
    threading.Thread(target=loop, name="backup-scheduler", daemon=True).start()

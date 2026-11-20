"""Starts the server: HTTP for office PCs and ZKTeco machines, HTTPS for mobile punch.
Used by the installed LailaPayroll.exe (started by Windows at boot) and by `python run.py`.
Any startup crash is written to <data>\\logs\\startup-error.txt."""
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
os.chdir(HERE)


def _crash_file() -> Path:
    if os.environ.get("LAILA_DATA_DIR"):
        d = Path(os.environ["LAILA_DATA_DIR"]) / "logs"
    elif sys.platform == "win32":
        d = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "LailaPayroll" / "logs"
    else:
        d = HERE / "data" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "startup-error.txt"


def main() -> None:
    import asyncio
    import logging
    import logging.handlers

    from config import DATA_DIR, settings

    # a restore chosen on the Backup page is applied here, before the database is opened
    from services.backup import apply_pending_restore
    restore_msgs = []
    apply_pending_restore(restore_msgs.append)

    if os.environ.get("LAILA_WAIT_FOR_PORT"):      # restarted by the program itself: wait for the old copy to exit
        import socket
        for _ in range(90):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
                try:
                    sk.bind(("0.0.0.0", settings.port))
                    break
                except OSError:
                    import time
                    time.sleep(1)

    handler = logging.handlers.RotatingFileHandler(DATA_DIR / "logs" / "server.log", maxBytes=5_000_000,
                                                   backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handlers = [handler] + ([logging.StreamHandler(sys.stdout)] if sys.stdout else [])
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    log = logging.getLogger("laila")
    log.info("Starting %s (%s) from %s", settings.app_name, settings.developer_credit, HERE)
    for m in restore_msgs:
        log.info(m)

    import uvicorn
    from main import app
    from services.maintenance import ensure_certificate, lan_ips

    async def serve() -> None:
        servers = [uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_config=None))]
        try:
            cert, key = ensure_certificate()
            servers.append(uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.https_port,
                                                         ssl_certfile=str(cert), ssl_keyfile=str(key),
                                                         log_config=None)))
        except Exception:
            log.exception("HTTPS (mobile punch) not started")
        for ip in lan_ips():
            log.info("Office PCs: http://%s:%s   Mobile: https://%s:%s/m", ip, settings.port, ip, settings.https_port)
        log.info("Data folder: %s", DATA_DIR)
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(serve())


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - must record every startup failure
        if not isinstance(exc, (KeyboardInterrupt, SystemExit)) or (isinstance(exc, SystemExit) and exc.code):
            try:
                _crash_file().write_text(f"{datetime.now():%Y-%m-%d %H:%M:%S}\n{traceback.format_exc()}",
                                         encoding="utf-8")
            except Exception:
                pass
        raise

"""Central error log: everything that goes wrong is stored and shown on the Error log page."""
import logging
import traceback

log = logging.getLogger("laila.errors")


def log_error(source: str, message: str, detail: str | None = None, username: str | None = None,
              exc: BaseException | None = None) -> None:
    if exc is not None and detail is None:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-6000:]
    log.error("[%s] %s%s", source, message, f"\n{detail}" if detail else "")
    try:
        from database import SessionLocal
        from models import ErrorLog
        db = SessionLocal()
        try:
            db.add(ErrorLog(source=source[:40], message=str(message)[:500], detail=detail, username=username))
            db.commit()
        finally:
            db.close()
    except Exception:  # never let logging break the caller
        log.exception("could not store error")

from sqlalchemy.orm import Session
from models import AuditLog


def audit(db: Session, user: dict | str, action: str, detail: str = "", commit: bool = True) -> None:
    name = user["username"] if isinstance(user, dict) else user
    db.add(AuditLog(username=name, action=action, detail=detail[:4000]))
    if commit:
        db.commit()

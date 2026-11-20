"""Settings. Zero-config by default: everything lives in one data folder
(C:\\ProgramData\\LailaPayroll on Windows) and is created on first start.
An optional settings.env in that folder can override any value."""
import os
import secrets
import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _data_dir() -> Path:
    if os.environ.get("LAILA_DATA_DIR"):
        d = Path(os.environ["LAILA_DATA_DIR"])
    elif sys.platform == "win32":
        d = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "LailaPayroll"
    else:
        d = Path(__file__).resolve().parent / "data"
    (d / "logs").mkdir(parents=True, exist_ok=True)
    (d / "backups").mkdir(parents=True, exist_ok=True)
    return d


DATA_DIR = _data_dir()


def _secret() -> str:
    f = DATA_DIR / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(48))
    return f.read_text().strip()


class Settings(BaseSettings):
    app_name: str = "LAILA GROUP Attendance & Payroll"
    group_name: str = "LAILA GROUP"
    developer_credit: str = "Developed by: SPHere – 01617144755"
    host: str = "0.0.0.0"
    port: int = 8585                      # HTTP: office PCs + ZKTeco machines (ADMS push)
    https_port: int = 8443                # HTTPS: mobile attendance (self-signed cert made on first start)
    timezone_offset_hours: int = 6        # Asia/Dhaka
    session_hours: int = 10

    # Default: SQLite file in the data folder (no database server needed).
    # SQL Server option: mssql+pyodbc://user:pass@HOST/LailaPayroll?driver=ODBC+Driver+17+for+SQL+Server
    database_url: str = ""

    device_poll_seconds: int = 60         # Hikvision polling interval
    backup_keep_days: int = 30            # daily automatic backup of the SQLite database

    # Mobile attendance
    mobile_enabled: bool = True
    max_gps_accuracy_m: int = 100

    model_config = SettingsConfigDict(env_file=str(DATA_DIR / "settings.env"), extra="ignore")

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(DATA_DIR / 'laila_payroll.db').as_posix()}"

    @property
    def secret_key(self) -> str:
        return _SECRET


_SECRET = _secret()
settings = Settings()

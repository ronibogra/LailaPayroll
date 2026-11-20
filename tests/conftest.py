"""Test setup: a temporary data folder, and a stand-in for config.py where pydantic-settings is not installed."""
import os
import sys
import tempfile
import types
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="laila_test_"))
os.environ["LAILA_DATA_DIR"] = str(_tmp)
try:
    import config  # noqa: F401
except ImportError:
    (_tmp / "logs").mkdir(exist_ok=True)
    (_tmp / "backups").mkdir(exist_ok=True)
    cfg = types.ModuleType("config")
    cfg.DATA_DIR = _tmp
    cfg.settings = types.SimpleNamespace(timezone_offset_hours=6, group_name="LAILA GROUP",
                                         developer_credit="Developed by: SPHere – 01617144755",
                                         db_url=f"sqlite:///{_tmp / 'laila_payroll.db'}", backup_keep_days=30)
    sys.modules["config"] = cfg

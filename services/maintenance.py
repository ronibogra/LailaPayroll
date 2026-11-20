"""First-start helpers: self-signed HTTPS certificate for mobile punch, daily SQLite backup."""
import ipaddress
import socket
import sqlite3
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import DATA_DIR, settings

CERT = DATA_DIR / "https-cert.pem"
KEY = DATA_DIR / "https-key.pem"


def lan_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # the address used for the default route
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips) or ["127.0.0.1"]


def ensure_certificate() -> tuple[Path, Path]:
    """Self-signed certificate for this server's name and LAN IPs, valid 10 years. Phones show a warning
    once; after accepting it the browser shares GPS with the mobile punch page."""
    if CERT.exists() and KEY.exists():
        return CERT, KEY
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    host = socket.gethostname()
    alt = [x509.DNSName(host), x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    alt += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in lan_ips() if ip != "127.0.0.1"]
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{settings.group_name} Payroll ({host})"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, settings.group_name)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    KEY.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                      serialization.NoEncryption()))
    CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return CERT, KEY


def backup_now() -> Path | None:
    """Consistent copy of the SQLite database (safe while the app is running)."""
    if not settings.db_url.startswith("sqlite"):
        return None
    src = DATA_DIR / "laila_payroll.db"
    if not src.exists():
        return None
    dest = DATA_DIR / "backups" / f"laila_payroll_{datetime.now():%Y%m%d_%H%M}.db"
    with sqlite3.connect(src) as a, sqlite3.connect(dest) as b:
        a.backup(b)
    cutoff = datetime.now() - timedelta(days=settings.backup_keep_days)
    for f in (DATA_DIR / "backups").glob("laila_payroll_*.db"):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink(missing_ok=True)
    return dest


def start_daily_backup() -> None:
    def loop():
        _time.sleep(120)
        while True:
            try:
                backup_now()
            except Exception:
                pass
            _time.sleep(24 * 3600)
    threading.Thread(target=loop, name="daily-backup", daemon=True).start()

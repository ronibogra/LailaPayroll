"""First-start helpers: HTTPS certificate (local CA + server certificate), daily SQLite backup."""
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


CA_CERT = DATA_DIR / "https-ca.pem"          # this server's own small certificate authority (install it once)
CA_KEY = DATA_DIR / "https-ca-key.pem"
NAMES = DATA_DIR / "https-names.txt"          # extra names, e.g. payroll.lailagroup.local (one per line)
LEAF_DAYS = 820                               # Apple / Chrome accept up to 825 days for locally trusted certs


def extra_names() -> list[str]:
    try:
        return [n.strip().lower() for n in NAMES.read_text(encoding="utf-8").splitlines() if n.strip()]
    except OSError:
        return []


def set_extra_names(names: list[str]) -> None:
    NAMES.write_text("\n".join(names), encoding="utf-8")


def _wanted() -> tuple[set[str], set[str]]:
    host = socket.gethostname().lower()
    dns = {host, "localhost"} | {n for n in extra_names() if not _is_ip(n)}
    ips = {"127.0.0.1"} | {ip for ip in lan_ips()} | {n for n in extra_names() if _is_ip(n)}
    return dns, ips


def _is_ip(v: str) -> bool:
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


def _ensure_ca():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    if CA_CERT.exists() and CA_KEY.exists():
        return (x509.load_pem_x509_certificate(CA_CERT.read_bytes()),
                serialization.load_pem_private_key(CA_KEY.read_bytes(), password=None))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{settings.group_name} Payroll Local CA"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, settings.group_name)])
    now = datetime.now(timezone.utc)
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
          .not_valid_after(now + timedelta(days=3650 * 2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
          .sign(key, hashes.SHA256()))
    CA_KEY.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                         serialization.NoEncryption()))
    CA_CERT.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    return ca, key


def cert_problem() -> str:
    """Why the server certificate must be made again ('' = it is fine)."""
    from cryptography import x509
    if not (CERT.exists() and KEY.exists() and CA_CERT.exists()):
        return "no certificate yet"
    try:
        leaf = x509.load_pem_x509_certificate(CERT.read_bytes())
        ca = x509.load_pem_x509_certificate(CA_CERT.read_bytes())
    except Exception:  # noqa: BLE001
        return "certificate file damaged"
    if leaf.issuer != ca.subject:
        return "old self-signed certificate"
    if leaf.not_valid_after_utc - datetime.now(timezone.utc) < timedelta(days=30):
        return "certificate expires soon"
    try:
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return "certificate has no names"
    have_dns = {n.lower() for n in san.get_values_for_type(x509.DNSName)}
    have_ip = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
    dns, ips = _wanted()
    missing = (dns - have_dns) | (ips - have_ip)
    return f"new name / address: {', '.join(sorted(missing))}" if missing else ""


def ensure_certificate(force: bool = False) -> tuple[Path, Path]:
    """HTTPS for the whole program. A local CA (made once, 20 years) signs the server certificate, which is made
    again automatically when the server gets a new IP address or name, or before it expires. Install the CA once on
    each PC / phone and the browser trusts the server without warnings - also after a new server certificate."""
    if not force and not cert_problem():
        return CERT, KEY
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ca, ca_key = _ensure_ca()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    dns, ips = _wanted()
    alt = [x509.DNSName(d) for d in sorted(dns)] + [x509.IPAddress(ipaddress.ip_address(i)) for i in sorted(ips)]
    host = socket.gethostname()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{settings.group_name} Payroll ({host})"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, settings.group_name)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=LEAF_DAYS))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    KEY.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                      serialization.NoEncryption()))
    CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
    return CERT, KEY


def cert_info() -> dict:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    out = {"ok": False}
    try:
        leaf = x509.load_pem_x509_certificate(CERT.read_bytes())
        ca = x509.load_pem_x509_certificate(CA_CERT.read_bytes())
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        out.update(ok=True, names=sorted(san.get_values_for_type(x509.DNSName)),
                   ips=sorted(str(i) for i in san.get_values_for_type(x509.IPAddress)),
                   until=leaf.not_valid_after_utc.date(), ca_until=ca.not_valid_after_utc.date(),
                   ca_name=ca.subject.rfc4514_string(),
                   thumb=ca.fingerprint(hashes.SHA1()).hex(":").upper(), problem=cert_problem())
    except Exception as exc:  # noqa: BLE001
        out["problem"] = str(exc)
    return out


def ca_der() -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    _ensure_ca()
    return x509.load_pem_x509_certificate(CA_CERT.read_bytes()).public_bytes(serialization.Encoding.DER)


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

"""Self-signed TLS certificate and access-token management.

The certificate is generated once and reused so the phone's one-time
certificate exception keeps working across runs. It is only regenerated when
the LAN IP is no longer listed in its SubjectAltName or it is about to expire.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import os
import secrets
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .paths import default_config_dir  # noqa: F401  (re-exported for callers)

log = logging.getLogger(__name__)

CERT_FILE = "cert.pem"
KEY_FILE = "key.pem"
TOKEN_FILE = "token.txt"
# Apple platforms reject TLS server certificates valid for more than 825 days.
VALIDITY_DAYS = 825
RENEW_BEFORE = dt.timedelta(days=14)


def _restrict_permissions(path: Path) -> None:
    if os.name == "posix":
        os.chmod(path, 0o600)


def _load_or_create_key(key_path: Path) -> rsa.RSAPrivateKey:
    if key_path.exists():
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        if isinstance(key, rsa.RSAPrivateKey):
            return key
        log.warning("Unexpected key type in %s; generating a new key", key_path)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    _restrict_permissions(key_path)
    return key


def _cert_is_usable(cert_path: Path, ip: str, hostname: str) -> bool:
    if not cert_path.exists():
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (ValueError, x509.ExtensionNotFound):
        return False
    ips = {str(a) for a in san.get_values_for_type(x509.IPAddress)}
    names = {n.lower() for n in san.get_values_for_type(x509.DNSName)}
    expires = cert.not_valid_after_utc
    fresh = expires - dt.datetime.now(dt.timezone.utc) > RENEW_BEFORE
    return ip in ips and hostname.lower() in names and fresh


def _build_cert(key: rsa.RSAPrivateKey, ip: str, hostname: str) -> x509.Certificate:
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, f"phonemic {hostname}"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "phonemic (self-signed)"),
        ]
    )
    san_entries: list[x509.GeneralName] = [
        x509.DNSName(hostname),
        x509.DNSName(f"{hostname}.local"),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address(ip)),
    ]
    if ip != "127.0.0.1":
        san_entries.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    now = dt.datetime.now(dt.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )


def ensure_certificate(config_dir: Path, ip: str, hostname: str) -> tuple[Path, Path]:
    """Return (cert_path, key_path), creating or refreshing them if needed."""
    config_dir.mkdir(parents=True, exist_ok=True)
    cert_path = config_dir / CERT_FILE
    key_path = config_dir / KEY_FILE
    if key_path.exists() and _cert_is_usable(cert_path, ip, hostname):
        log.debug("Reusing certificate %s", cert_path)
        return cert_path, key_path
    if cert_path.exists():
        log.warning(
            "Certificate does not cover %s (IP changed or expiring); generating a new one. "
            "The phone will show the certificate warning once more.",
            ip,
        )
    key = _load_or_create_key(key_path)
    cert = _build_cert(key, ip, hostname)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("Created self-signed certificate %s for %s / %s", cert_path, ip, hostname)
    return cert_path, key_path


def load_or_create_token(config_dir: Path, regenerate: bool = False) -> str:
    """Return the persistent URL access token (so a bookmarked URL keeps working)."""
    config_dir.mkdir(parents=True, exist_ok=True)
    token_path = config_dir / TOKEN_FILE
    if not regenerate and token_path.exists():
        token = token_path.read_text(encoding="ascii").strip()
        if len(token) >= 16:
            return token
    token = secrets.token_urlsafe(16)
    token_path.write_text(token, encoding="ascii")
    _restrict_permissions(token_path)
    return token


def make_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    """Server-side TLS context for aiohttp."""
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx

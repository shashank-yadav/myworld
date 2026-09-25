"""A private certificate authority for the gateway: one CA, a leaf certificate per intercepted host.

Agents trust the CA (``SSL_CERT_FILE``, ``REQUESTS_CA_BUNDLE``, ``NODE_EXTRA_CA_CERTS``...), so
their real clients talk TLS to ``gmail.googleapis.com`` and ``api.github.com`` as usual while
toolsim answers.
"""

from __future__ import annotations

import datetime as dt
import ssl
import threading
from pathlib import Path


class CA:
    def __init__(self, directory: Path):
        try:
            from cryptography import x509  # noqa: F401
        except ImportError:  # pragma: no cover
            raise RuntimeError("the gateway needs the 'cryptography' package: pip install 'toolsim[gateway]'") from None
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cert_path = self.dir / "toolsim-ca.pem"
        self.key_path = self.dir / "toolsim-ca.key"
        self._lock = threading.Lock()
        self._contexts: dict[str, ssl.SSLContext] = {}
        if not (self.cert_path.exists() and self.key_path.exists()):
            self._create()
        self._load()

    def _create(self) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "toolsim gateway CA"),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "toolsim")])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                             content_commitment=False, key_encipherment=False, data_encipherment=False,
                                             key_agreement=False, encipher_only=False, decipher_only=False),
                               critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256()))
        self.key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
        self.key_path.chmod(0o600)
        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    def _load(self) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        self._cert = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
        self._key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)

    @property
    def pem(self) -> str:
        return self.cert_path.read_text()

    def context_for(self, host: str) -> ssl.SSLContext:
        """A server-side TLS context presenting a certificate for ``host`` (made once, then cached)."""
        host = host.lower()
        with self._lock:
            ctx = self._contexts.get(host)
            if ctx is None:
                cert, key = self._leaf(host)
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                ctx.set_alpn_protocols(["http/1.1"])  # no HTTP/2: clients fall back cleanly
                ctx.load_cert_chain(cert, key)
                self._contexts[host] = ctx
            return ctx

    def _leaf(self, host: str) -> tuple[Path, Path]:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        safe = "".join(c if c.isalnum() or c in ".-" else "_" for c in host)
        cert_path, key_path = self.dir / f"leaf-{safe}.pem", self.dir / f"leaf-{safe}.key"
        if cert_path.exists() and key_path.exists():
            return cert_path, key_path
        key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
                .issuer_name(self._cert.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=365))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()),
                               critical=False)
                .sign(self._key, hashes.SHA256()))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        key_path.chmod(0o600)
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + self.cert_path.read_bytes())
        return cert_path, key_path

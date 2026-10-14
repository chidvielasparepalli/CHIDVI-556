"""Generate a private LAN TLS certificate only when explicitly run by the owner."""
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def create_certificate(address: str, directory: Path) -> str:
    ip = ipaddress.IPv4Address(address)
    if not any(ip in ipaddress.ip_network(network) for network in
               ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise ValueError("Choose the laptop's private LAN IPv4 address.")
    paths = [directory / name for name in ("jarvis.key", "jarvis.crt", "controller-ca.crt")]
    if any(path.exists() for path in paths):
        raise FileExistsError("Certificate files already exist; keep them or back them up explicitly before replacing them.")
    now = datetime.now(timezone.utc)
    authority_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    authority_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CHIDVI personal controller CA")])
    authority = (x509.CertificateBuilder().subject_name(authority_name).issuer_name(authority_name)
                 .public_key(authority_key.public_key()).serial_number(x509.random_serial_number())
                 .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=30))
                 .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                 .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
                 .sign(authority_key, hashes.SHA256()))
    certificate = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(ip))]))
                   .issuer_name(authority_name).public_key(server_key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
                   .not_valid_after(now + timedelta(days=30))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip)]), critical=False)
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
                   .sign(authority_key, hashes.SHA256()))
    contents = [server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()),
                certificate.public_bytes(serialization.Encoding.PEM), authority.public_bytes(serialization.Encoding.PEM)]
    directory.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        for path, content in zip(paths, contents):
            with path.open("xb") as output:
                created.append(path)
                output.write(content)
        paths[0].chmod(0o600)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return authority.fingerprint(hashes.SHA256()).hex(":")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m dashboard.certificates <laptop-private-ipv4>")
    fingerprint = create_certificate(sys.argv[1], Path(__file__).resolve().parents[1] / "config" / "certs")
    print("Created a 30-day certificate. Copy only config/certs/controller-ca.crt to your phone.")
    print("CA SHA-256 fingerprint:", fingerprint)
    print("Keep jarvis.key private. No network service or firewall rule was started.")

"""Create a dedicated environment without reading any main runtime state."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/v040/compose.yaml"


def write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def certificate_files(state: Path, public_ip: str) -> None:
    now = datetime.now(timezone.utc)
    pki = state / "pki"
    tls = state / "tls"
    for directory in (pki, tls):
        directory.mkdir(mode=0o700, exist_ok=True)
    ca_path = pki / "ca.crt"
    key_path = pki / "ca.key"
    if ca_path.exists() != key_path.exists():
        raise ValueError("Incomplete CA; restore it instead of replacing trust silently")
    if ca_path.exists():
        ca = x509.load_pem_x509_certificate(ca_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "W2F v0.4.0 Prototype CA")])
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
              .public_key(key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1825))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
              .sign(key, hashes.SHA256()))
        write_private(key_path, key.private_bytes(serialization.Encoding.PEM,
                      serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        write_private(ca_path, ca.public_bytes(serialization.Encoding.PEM), 0o644)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ip = ipaddress.ip_address(public_ip)
    ips = sorted({str(ip), "127.0.0.1", "::1"})
    certificate = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(ip))]))
                   .issuer_name(ca.subject).public_key(server_key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=180))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName(
                       [x509.IPAddress(ipaddress.ip_address(value)) for value in ips]), critical=False)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .sign(key, hashes.SHA256()))
    write_private(tls / "server.key", server_key.private_bytes(serialization.Encoding.PEM,
                  serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    write_private(tls / "server.crt", certificate.public_bytes(serialization.Encoding.PEM), 0o644)


def initialize(state: Path, environment: str, project: str, public_ip: str,
               bind_ip: str, port: int, image: str) -> Path:
    if not re.fullmatch(r"w2f-v040-[a-z0-9-]+", project):
        raise ValueError("Project must have a w2f-v040- prefix")
    if environment not in {"v040-local", "v040-ci", "v040-prototype"}:
        raise ValueError("Unknown v0.4.0 environment")
    if not 1024 <= port <= 65535 or port == 13000:
        raise ValueError("Use a dedicated unprivileged port, excluding 13000")
    ipaddress.ip_address(public_ip)
    ipaddress.ip_address(bind_ip)
    state = state.expanduser().resolve()
    for protected in (Path.home() / ".local/share/w2f-cli", Path.home() / ".config/w2f-cli"):
        if state == protected.resolve() or protected.resolve() in state.parents:
            raise ValueError("Refusing the main runtime directory")
    if state in {Path("/"), Path.home()} or not re.fullmatch(r"[A-Za-z0-9_./-]+", str(state)):
        raise ValueError("State requires a dedicated path without shell/dotenv metacharacters")
    if not re.fullmatch(r"w2f-v040-cloud:(?:[a-f0-9]{40}|local|test)", image):
        raise ValueError("Use a v0.4.0 image with a commit tag")
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    marker = state / ".w2f-v040"
    identity = f"{environment}\n{project}\n"
    if marker.exists() and marker.read_text() != identity:
        raise ValueError("Refusing to reuse another environment's state")
    env_file = state / "compose.env"
    if marker.exists() and env_file.exists():
        delivery_secret = state / "secrets/feishu_apps.json"
        if not delivery_secret.exists():
            write_private(delivery_secret, b"{}\n", 0o444)
        return env_file
    if not marker.exists():
        if any(state.iterdir()):
            raise ValueError("Initial state directory must be empty")
        write_private(marker, identity.encode())
    secret_dir = state / "secrets"
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    for name in ("admin_password", "database_password"):
        path = secret_dir / name
        if not path.exists():
            # Container users can read individual secret mounts; the host parent is private.
            write_private(path, (secrets.token_urlsafe(36) + "\n").encode(), 0o444)
    if not (secret_dir / "feishu_apps.json").exists():
        write_private(secret_dir / "feishu_apps.json", b"{}\n", 0o444)
    certificate_files(state, public_ip)
    values = {
        "W2F_PROJECT": project, "W2F_STATE": str(state), "W2F_ENVIRONMENT": environment,
        "W2F_BIND_IP": bind_ip, "W2F_PORT": str(port), "W2F_IMAGE": image,
    }
    write_private(env_file, "".join(f"{key}={value}\n" for key, value in values.items()).encode())
    return env_file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "renew", "up", "stop", "status"])
    parser.add_argument("--state", type=Path, default=ROOT / ".v040-local")
    parser.add_argument("--environment", default="v040-local")
    parser.add_argument("--project", default="w2f-v040-local")
    parser.add_argument("--public-ip", default="127.0.0.1")
    parser.add_argument("--bind-ip", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=13040)
    parser.add_argument("--image", default="w2f-v040-cloud:local")
    args = parser.parse_args()
    env_file = args.state.resolve() / "compose.env"
    if args.command == "init":
        initialize(args.state, args.environment, args.project, args.public_ip,
                   args.bind_ip, args.port, args.image)
        print(f"Created dedicated environment: {env_file}")
        print("Admin user: admin. Password is in the state's secrets/admin_password file.")
        print("Trust pki/ca.crt explicitly; CA private keys must never leave this state directory.")
        return
    if not env_file.is_file():
        parser.error("Initialize this dedicated environment first")
    if args.command == "renew":
        if not (args.state / "pki/ca.key").is_file() or not (args.state / "pki/ca.crt").is_file():
            parser.error("Restore the existing CA before renewing certificates")
        certificate_files(args.state.resolve(), args.public_ip)
        print("Renewed server certificate; explicitly recreate gateway to activate it")
        return
    command = ["docker", "compose", "--env-file", str(env_file), "-f", str(COMPOSE)]
    if args.command == "up":
        subprocess.run(["docker", "build", "-f", str(ROOT / "cloud/Dockerfile"),
                        "-t", args.image, str(ROOT)], check=True)
        command += ["up", "-d", "--wait", "--wait-timeout", "120"]
    elif args.command == "stop":
        command += ["stop"]
    else:
        command += ["ps"]
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()

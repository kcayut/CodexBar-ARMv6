"""Create the local HTTPS identity as the unprivileged display account."""
import ipaddress
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tempfile


def openssl(*args):
    return subprocess.run(["openssl", *args], check=True, capture_output=True, text=True).stdout


def prepare(directory, host, addresses):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    cert, key = directory / "tls.crt", directory / "tls.key"
    if cert.is_symlink() or key.is_symlink() or (cert.exists() and not key.exists()):
        raise ValueError("請檢查既有的 HTTPS 憑證與金鑰檔")
    host = host.lower().split(".")[0]
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", host):
        raise ValueError("Pi 主機名稱格式不符")
    ips = {"127.0.0.1", *(str(ipaddress.IPv4Address(ip)) for ip in addresses)}
    names = {host + ".local", "quota-desk.local", "localhost"}
    expected = {"DNS:" + name for name in names} | {"IP Address:" + ip for ip in ips}
    if key.exists():
        key.chmod(0o600)
    if cert.exists():
        cert.chmod(0o600)
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
        details = openssl("x509", "-in", cert, "-text", "-noout")
        san = re.search(r"X509v3 Subject Alternative Name:[^\n]*\n([^\n]+)", details)
        actual = {word.strip() for word in san[1].split(",")} if san else set()
        if expected <= actual and subprocess.run(
                ["openssl", "x509", "-in", cert, "-noout", "-checkend", "604800"],
                capture_output=True).returncode == 0:
            return

    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        tmp = Path(tmp)
        if not key.exists():
            openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", tmp / "key")
            (tmp / "key").chmod(0o600)
            os.replace(tmp / "key", key)
        san = ["DNS:" + name for name in sorted(names)] + ["IP:" + ip for ip in sorted(ips)]
        openssl("req", "-x509", "-new", "-key", key, "-sha256", "-days", "365",
                "-subj", "/CN=" + host + ".local", "-addext", "subjectAltName=" + ",".join(san),
                "-addext", "basicConstraints=critical,CA:FALSE", "-addext", "extendedKeyUsage=serverAuth",
                "-out", tmp / "cert")
        (tmp / "cert").chmod(0o600)
        os.replace(tmp / "cert", cert)


if __name__ == "__main__":
    os.umask(0o077)
    output = subprocess.check_output(["ip", "-4", "-o", "addr", "show", "scope", "global"], text=True)
    addresses = re.findall(r"\binet ([0-9.]+)/", output)
    prepare(sys.argv[1], socket.gethostname(), addresses)

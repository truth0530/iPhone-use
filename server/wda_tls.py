"""Loopback mutual TLS material for the USB forwarder.

The listener stays on 127.0.0.1. A client certificate is required because TLS
alone does not stop another local process. Keys stay mode 0600. The same
macOS user can still read them. This module never logs key bytes.
"""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

OPENSSL = "/usr/bin/openssl"
NAMES = ("ca.pem", "ca.key", "server.pem", "server.key", "client.pem", "client.key")
CLIENT_NAMES = ("ca.pem", "client.pem", "client.key")
_POPEN = None


def _popen():
    """The real Popen. Setup tests replace subprocess.Popen while a worker runs."""
    global _POPEN
    if _POPEN is not None:
        return _POPEN
    current = subprocess.Popen
    if isinstance(current, type):
        _POPEN = current
        return _POPEN
    saved = sys.modules.get("subprocess")
    sys.modules.pop("subprocess", None)
    try:
        import subprocess as fresh
        _POPEN = fresh.Popen
    finally:
        if saved is not None:
            sys.modules["subprocess"] = saved
    return _POPEN


def _openssl(directory, args):
    child = _popen()([OPENSSL, *args], cwd=directory, stdin=subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, _stderr = child.communicate()
    if child.returncode != 0:
        raise ValueError("Could not create loopback TLS material.")
    return stdout


def _write(directory, name, text):
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def _safe_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ValueError("TLS material is incomplete.") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("TLS material must be one regular file.")
    if info.st_mode & 0o077:
        raise ValueError("TLS material must not be group or world accessible.")


def _require(directory, names=NAMES):
    info = directory.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or (info.st_mode & 0o777) != 0o700:
        raise ValueError("TLS directory must be a real directory with mode 0700.")
    for name in names:
        _safe_file(directory / name)


def material_dir(state, *, client_only=False):
    """Return state/tls when its files are private. Missing means unset, not broken."""
    directory = Path(state) / "tls"
    if directory.is_symlink():
        raise ValueError("TLS directory must not contain a symlink.")
    try:
        directory.lstat()
    except FileNotFoundError:
        return None
    _require(directory, CLIENT_NAMES if client_only else NAMES)
    return directory


def client_context(directory):
    import ssl
    directory = Path(directory)
    _require(directory, CLIENT_NAMES)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_verify_locations(cafile=str(directory / "ca.pem"))
    context.load_cert_chain(str(directory / "client.pem"), str(directory / "client.key"))
    return context


def _certificate_text(directory, name):
    return _openssl(directory, ["x509", "-in", name, "-noout", "-text"])


def _verify(directory):
    for name in ("server.pem", "client.pem"):
        _openssl(directory, ["verify", "-CAfile", "ca.pem", name])
    server = _certificate_text(directory, "server.pem")
    client = _certificate_text(directory, "client.pem")
    server_ok = ("IP Address:127.0.0.1" in server and server.count("IP Address:") == 1
                 and "DNS:" not in server and "TLS Web Server Authentication" in server
                 and "TLS Web Client Authentication" not in server)
    client_ok = ("TLS Web Client Authentication" in client and "TLS Web Server Authentication" not in client
                 and "IP Address:" not in client and "DNS:" not in client)
    if not server_ok or not client_ok:
        raise ValueError("Loopback certificates must be client-auth and server-auth for 127.0.0.1 only.")


def _generate(directory):
    _write(directory, "ca.cnf", """
[req]
distinguished_name=req_dn
x509_extensions=v3_ca
prompt=no
[req_dn]
CN=iphone-use-loopback-ca
[v3_ca]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
""")
    _write(directory, "server.ext", """
basicConstraints=CA:FALSE
subjectAltName=IP:127.0.0.1
extendedKeyUsage=serverAuth
keyUsage=critical,digitalSignature
""")
    _write(directory, "client.ext", """
basicConstraints=CA:FALSE
extendedKeyUsage=clientAuth
keyUsage=critical,digitalSignature
""")
    _openssl(directory, ["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "ca.key"])
    _openssl(directory, ["req", "-new", "-x509", "-key", "ca.key", "-sha256", "-days", "825",
                         "-config", "ca.cnf", "-out", "ca.pem"])
    _openssl(directory, ["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "server.key"])
    _openssl(directory, ["req", "-new", "-key", "server.key", "-subj", "/CN=127.0.0.1", "-out", "server.csr"])
    _openssl(directory, ["x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                         "-CAcreateserial", "-out", "server.pem", "-days", "825", "-sha256", "-extfile", "server.ext"])
    _openssl(directory, ["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "client.key"])
    _openssl(directory, ["req", "-new", "-key", "client.key", "-subj", "/CN=iphone-use-client", "-out", "client.csr"])
    _openssl(directory, ["x509", "-req", "-in", "client.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                         "-CAcreateserial", "-out", "client.pem", "-days", "825", "-sha256", "-extfile", "client.ext"])
    for name in ("ca.cnf", "server.ext", "client.ext", "server.csr", "client.csr", "ca.srl"):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("TLS staging contained an unexpected entry.")
        path.unlink()
    for child in directory.iterdir():
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("TLS staging contained an unexpected entry.")
        os.chmod(child, 0o600)


def ensure(state):
    """Create state/tls once. An incomplete directory is left in place and rejected."""
    state = Path(state)
    target = state / "tls"
    if target.exists() or target.is_symlink():
        _require(target)
        _verify(target)
        return target
    previous = os.umask(0o077)
    staging = Path(tempfile.mkdtemp(prefix=".tls-", dir=state))
    try:
        os.chmod(staging, 0o700)
        _generate(staging)
        _verify(staging)
        _require(staging)
        os.rename(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if target.exists() or target.is_symlink():
            _require(target)
            _verify(target)
            return target
        raise
    finally:
        os.umask(previous)
    return target

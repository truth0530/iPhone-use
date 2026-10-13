"""Mutual TLS on the loopback forwarder. No USB, WDA, or phone access."""
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import isolated
from wda_client import WDAClient, WDAError
from wda_tls import ensure, material_dir

NODE = shutil.which("node")
OPENSSL = Path("/usr/bin/openssl")


def node_env(tls=None, **extra):
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(Path.home()),
           "IPHONE_USE_ANALYTICS": "0", "DO_NOT_TRACK": "1"}
    if tls is not None:
        env["IPHONE_USE_TLS_DIR"] = str(tls)
    env.update(extra)
    return env


@unittest.skipUnless(NODE and OPENSSL.is_file(), "node or openssl unavailable")
class ForwardTLSTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.script = self.root / "forward.mjs"
        shutil.copyfile(ROOT / "tooling" / "forward.mjs", self.script)

    def test_incomplete_material_is_rejected_and_kept(self):
        tls = self.state / "tls"
        tls.mkdir(mode=0o700)
        os.chmod(tls, 0o700)
        partial = tls / "ca.pem"
        partial.write_bytes(b"partial")
        os.chmod(partial, 0o600)
        with self.assertRaises(ValueError):
            ensure(self.state)
        self.assertEqual(partial.read_bytes(), b"partial")
        self.assertIsNone(next(self.state.glob(".tls-*"), None))

    def test_private_material_and_profile_split(self):
        tls = ensure(self.state)
        self.assertEqual(tls.stat().st_mode & 0o777, 0o700)
        for name in ("ca.pem", "ca.key", "server.pem", "server.key", "client.pem", "client.key"):
            info = (tls / name).stat()
            self.assertEqual(info.st_nlink, 1)
            self.assertEqual(info.st_mode & 0o077, 0)
        self.assertEqual(material_dir(self.state), tls)
        secret = self.root / "secret"
        secret.write_bytes(b"other")
        os.chmod(secret, 0o644)
        key = tls / "client.key"
        key.unlink()
        key.symlink_to(secret)
        with self.assertRaises(ValueError):
            material_dir(self.state)
        self.assertEqual(secret.stat().st_mode & 0o777, 0o644)
        key.unlink()
        mcp = isolated.sandbox_profile(self.state)
        broker = isolated.sandbox_profile(self.state, 18100)
        self.assertNotIn("client.key", mcp)
        self.assertIn(str(tls / "client.key"), broker)
        self.assertNotIn("server.key", broker)

    def test_missing_or_unsafe_directory_never_listens(self):
        port = self.free_port()
        result = subprocess.run([NODE, str(self.script), "TESTDEVICE", str(port), "8100"],
                                env=node_env(), capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("IPHONE_USE_TLS_DIR", result.stderr)
        self.assertNotIn("listening", result.stderr + result.stdout)
        self.assertNotIn("appium", result.stderr + result.stdout)
        tls = ensure(self.state)
        linked = self.root / "linked"
        linked.symlink_to(tls, target_is_directory=True)
        for label, env in (
            ("relative", node_env(IPHONE_USE_TLS_DIR="tls")),
            ("symlink", node_env(linked)),
            ("usbmux", node_env(tls, USBMUXD_SOCKET_ADDRESS="127.0.0.1:27015")),
        ):
            with self.subTest(label=label):
                result = subprocess.run([NODE, str(self.script), "TESTDEVICE", str(port), "8100"],
                                        env=env, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("listening", result.stderr + result.stdout)
        os.chmod(tls / "client.key", 0o644)
        result = subprocess.run([NODE, str(self.script), "TESTDEVICE", str(port), "8100"],
                                env=node_env(tls), capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("listening", result.stderr + result.stdout)
        self.assertFalse(self.listening(port))

    def test_client_certificate_is_required_and_loopback_only(self):
        tls = ensure(self.state)
        other_state = self.root / "other"
        other_state.mkdir(mode=0o700)
        other = ensure(other_state)
        port = self.free_port()
        log = self.root / "forward.log"
        log_stream = log.open("w")
        self.addCleanup(log_stream.close)
        proc = subprocess.Popen([NODE, str(self.script), "TESTDEVICE", str(port), "8100"],
                                cwd=self.root, env=node_env(tls), stdout=log_stream, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop, proc)
        self.wait_until(lambda: "listening" in log.read_text(), proc, log)
        self.assertIn("127.0.0.1:%s" % port, self.listeners(port))
        self.assertNotRegex(self.listeners(port), r"0\.0\.0\.0:|\*:|\[::\]:")
        self.plain_http(port)
        self.assertFalse(self.application_data(port, ca=tls / "ca.pem"))
        self.assertNotIn("appium", log.read_text())
        self.assertIn("TLS client rejected", log.read_text())
        self.assertFalse(self.application_data(port, ca=tls / "ca.pem", cert=tls / "server.pem", key=tls / "server.key"))
        self.assertNotIn("appium", log.read_text())
        with self.assertRaises(ssl.SSLError):
            self.handshake(port, ca=other / "ca.pem", cert=other / "client.pem", key=other / "client.key")
        wrapped = self.handshake(port, ca=tls / "ca.pem", cert=tls / "client.pem", key=tls / "client.key")
        self.assertEqual(wrapped.version(), "TLSv1.3")
        wrapped.close()
        client = WDAClient("http://127.0.0.1:%s" % port, timeout=2, tls_dir=tls)
        with self.assertRaises(WDAError) as caught:
            client.request("GET", "/status")
        self.assertEqual(caught.exception.code, "pua_unreachable")
        self.wait_until(lambda: "USB connect failed" in log.read_text(), proc, log)
        text = log.read_text()
        self.assertNotIn((tls / "client.key").read_text(), text)
        self.assertNotIn((tls / "server.key").read_text(), text)
        if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
            code = """
import socket, sys
sys.path.insert(0, %r)
from wda_tls import client_context
ctx = client_context(%r)
sock = socket.create_connection(("127.0.0.1", %d), 2)
tls = ctx.wrap_socket(sock, server_hostname="127.0.0.1")
assert tls.version() == "TLSv1.3"
tls.close()
print("sandbox-ok")
""" % (str(ROOT / "server"), str(tls), port)
            sandboxed = subprocess.run(["/usr/bin/sandbox-exec", "-p", isolated.sandbox_profile(self.state, port),
                                        str(isolated.python_executable()), "-I", "-S", "-B", "-c", code],
                                       capture_output=True, text=True, timeout=10)
            self.assertEqual(sandboxed.returncode, 0, sandboxed.stderr)
            self.assertIn("sandbox-ok", sandboxed.stdout)

    @staticmethod
    def free_port():
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    @staticmethod
    def listeners(port):
        result = subprocess.run(["lsof", "-nP", "-iTCP:%s" % port, "-sTCP:LISTEN"],
                                capture_output=True, text=True)
        return result.stdout

    def listening(self, port):
        return "127.0.0.1:%s" % port in self.listeners(port)

    @staticmethod
    def handshake(port, ca, cert=None, key=None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_verify_locations(cafile=str(ca))
        if cert is not None:
            context.load_cert_chain(str(cert), str(key))
        sock = socket.create_connection(("127.0.0.1", port), 2)
        return context.wrap_socket(sock, server_hostname="127.0.0.1")

    def application_data(self, port, **kwargs):
        """True only if a client can read an HTTP response. A rejected handshake is closed."""
        try:
            wrapped = self.handshake(port, **kwargs)
        except ssl.SSLError:
            return False
        wrapped.settimeout(1)
        try:
            wrapped.send(b"GET /status HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            try:
                data = wrapped.recv(64)
            except (ssl.SSLError, OSError):
                data = b""
        finally:
            wrapped.close()
        return b"HTTP" in data

    @staticmethod
    def plain_http(port):
        sock = socket.create_connection(("127.0.0.1", port), 2)
        sock.settimeout(1)
        try:
            sock.sendall(b"GET /status HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            try:
                data = sock.recv(64)
            except socket.timeout:
                data = b""
        finally:
            sock.close()
        if b"HTTP" in data:
            raise AssertionError("plaintext HTTP reached the forwarder")

    def wait_until(self, ready, proc, log):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if ready():
                return
            if proc.poll() is not None:
                self.fail(log.read_text())
            time.sleep(0.05)
        self.fail(log.read_text())

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()

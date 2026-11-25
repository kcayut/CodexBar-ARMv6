import concurrent.futures
from pathlib import Path
import socket
import ssl
import stat
import tempfile
import unittest

from native_tls import prepare


class NativeInstallTest(unittest.TestCase):
    def test_private_key_stable_and_sans_match_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "quota-desk"
            prepare(root, "desk", ["192.0.2.10"])
            cert, key = root / "tls.crt", root / "tls.key"
            first = cert.read_bytes(), key.read_bytes()
            prepare(root, "desk", ["192.0.2.10"])
            self.assertEqual((cert.read_bytes(), key.read_bytes()), first)
            for file in (cert, key):
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)

            prepare(root, "desk", ["192.0.2.20"])
            self.assertNotEqual(cert.read_bytes(), first[0])
            self.assertEqual(key.read_bytes(), first[1])
            server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server.load_cert_chain(cert, key)
            client = ssl.create_default_context(cafile=str(cert))

            def serve(channel):
                with server.wrap_socket(channel, server_side=True) as tls:
                    tls.sendall(b"ok")

            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                for name in ("desk.local", "quota-desk.local", "localhost", "127.0.0.1", "192.0.2.20"):
                    left, right = socket.socketpair()
                    task = pool.submit(serve, left)
                    with client.wrap_socket(right, server_hostname=name) as tls:
                        self.assertEqual(tls.recv(2), b"ok")
                    task.result()


if __name__ == "__main__":
    unittest.main()

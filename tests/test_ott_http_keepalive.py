import http.client
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from ott.api import BoundedHTTPServer, Handler, make_app
from ott.security import ServerPolicy
from ott.store import Store


class HTTPKeepAliveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        app = make_app(Store(root / "ott.sqlite3"), root, Path(__file__).resolve().parents[1] / "mockup",
                       policy=ServerPolicy(dispatch_enabled=False))
        request_handler = type("KeepAliveHandler", (Handler,), {"app": app, "request_deadline_seconds": 0.1})
        self.server = BoundedHTTPServer(("127.0.0.1", 0), request_handler, app)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server_thread.join()
        self.server.server_close()
        self.temporary.cleanup()

    def test_successful_requests_reuse_one_connection(self):
        for _ in range(2):
            self.connection.request("GET", "/healthz")
            response = self.connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.version, 11)
            response.read()
            self.assertIsNotNone(self.connection.sock)
            if _ == 0:
                first_socket = self.connection.sock
            else:
                self.assertIs(self.connection.sock, first_socket)

    def test_rejected_request_closes_connection_before_unread_body_reuse(self):
        self.connection.request("GET", "/healthz", body=b"x", headers={"Content-Length": "1"})
        response = self.connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertEqual(response.getheader("Connection"), "close")
        response.read()
        self.assertIsNone(self.connection.sock)

    def test_slow_header_deadline_closes_partial_request(self):
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        self.addCleanup(connection.close)
        connection.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nX-Slow:")
        try:
            received = connection.recv(1)
        except ConnectionResetError:
            received = b""
        connection.close()
        self.assertEqual(received, b"")


if __name__ == "__main__":
    unittest.main()

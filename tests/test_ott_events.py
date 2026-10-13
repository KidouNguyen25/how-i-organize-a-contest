import http.client
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ott.api import make_app, serve
from ott.events import Hub
from ott.store import Store

ROOT = Path(__file__).resolve().parent.parent


class HubTests(unittest.TestCase):
    def test_latest_event_per_key_wins_and_batches(self):
        hub = Hub()
        sub = hub.subscribe()
        for n in range(50):
            hub.publish("match", "m1", move_count=n)
        hub.publish("match", "m2", move_count=1)
        batch = sub.get_batch(1)
        self.assertEqual(len(batch), 2)
        self.assertEqual({e["id"]: e["move_count"] for e in batch}, {"m1": 49, "m2": 1})
        self.assertIsNone(sub.get_batch(0.05))  # idle -> keep-alive signal

    def test_unsubscribe_and_limit(self):
        hub = Hub()
        sub = hub.subscribe()
        self.assertEqual(hub.clients, 1)
        hub.unsubscribe(sub)
        self.assertEqual(hub.clients, 0)
        self.assertEqual(sub.get_batch(0.05), [])  # closed


class StreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ott-ev-"))
        store = Store(cls.tmp / "t.db")
        store.create_user("boss_user", "correct-horse-battery", "ORGANIZER")
        cls.app = make_app(store, cls.tmp, ROOT / "mockup", workers=1)
        cls.server = serve(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def login(self):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        con.request("POST", "/api/v1/auth/login", json.dumps({"username": "boss_user", "password": "correct-horse-battery"}), {"Content-Type": "application/json"})
        resp = con.getresponse()
        data = json.loads(resp.read())
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        return cookie, data["csrf_token"]

    def open_stream(self):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        con.request("GET", "/api/v1/stream")
        resp = con.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream", resp.getheader("Content-Type"))
        return con, resp

    @staticmethod
    def read_event(resp):
        """Read one SSE message (skipping comments); return (event_name, data_dict)."""
        name, data = "message", None
        while True:
            line = resp.fp.readline().decode().rstrip("\n")
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1])
            elif line == "" and data is not None:
                return name, data

    def test_changes_are_pushed_to_every_subscriber_quickly(self):
        streams = [self.open_stream() for _ in range(3)]
        try:
            for _, resp in streams:
                self.assertEqual(self.read_event(resp)[0], "hello")
            cookie, csrf = self.login()
            con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            started = time.monotonic()
            con.request("POST", "/api/v1/tournaments", json.dumps({"name": "Live Cup", "clock_seconds": 300}),
                        {"Content-Type": "application/json", "Cookie": cookie, "X-CSRF-Token": csrf})
            created = json.loads(con.getresponse().read())
            for _, resp in streams:
                name, event = self.read_event(resp)
                self.assertEqual((event["type"], event["id"]), ("tournament", created["id"]))
            self.assertLess(time.monotonic() - started, 1.0, "push took longer than 1 s")
            # lifecycle transition is pushed with the new status
            con.request("POST", f"/api/v1/tournaments/{created['id']}/open-registration", None,
                        {"Cookie": cookie, "X-CSRF-Token": csrf})
            con.getresponse().read()
            _, event = self.read_event(streams[0][1])
            self.assertEqual(event.get("status"), "REGISTRATION_OPEN")
            con2 = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            con2.request("GET", "/api/v1/stats")
            self.assertGreaterEqual(json.loads(con2.getresponse().read())["stream_clients"], 3)
        finally:
            for con, _ in streams:
                con.close()

    def test_stream_events_carry_no_board_or_secrets(self):
        con, resp = self.open_stream()
        try:
            self.read_event(resp)
            cookie, csrf = self.login()
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("POST", "/api/v1/tournaments", json.dumps({"name": "Quiet Cup", "clock_seconds": 600}),
                      {"Content-Type": "application/json", "Cookie": cookie, "X-CSRF-Token": csrf})
            c.getresponse().read()
            _, event = self.read_event(resp)
            self.assertLessEqual(set(event), {"type", "id", "status", "tid", "move_count", "phase"})
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()

import json
import math
import random
import tempfile
import threading
import time
import unittest
from email.message import Message
from io import BytesIO
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ott.api import ApiError, BoundedHTTPServer, Ctx, Handler, MAX_JSON_DEPTH, make_app
from ott.security import ServerPolicy
from ott.store import Store


class CountingInput(BytesIO):
    def __init__(self, raw):
        super().__init__(raw)
        self.reads = 0

    def read(self, length=-1):
        self.reads += 1
        return super().read(length)


class RequestJSONTests(unittest.TestCase):
    def context(self, raw=b"{}", *, headers=None, maximum=1048576):
        message = Message()
        for name, value in (headers if headers is not None else (
                ("Content-Length", str(len(raw))), ("Content-Type", "application/json"))):
            message[name] = value
        handler = SimpleNamespace(path="/api/v1/tournaments/example/code?language=c17", headers=message,
                                  rfile=CountingInput(raw), body_received=Mock(),
                                  app=SimpleNamespace(policy=SimpleNamespace(max_body=maximum)))
        return Ctx(handler, None, None)

    def test_repeated_callers_decode_and_validate_exactly_once(self):
        payload = {"source": "// chiến thuật 🌏\n", "language": "rust2021", "confirm": True,
                   "metadata": {"values": [None, False, 1, 2.5]}}
        context = self.context(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        with patch("ott.api.json.loads", wraps=json.loads) as decoder:
            parsed = context.json()
            for _ in range(4):
                self.assertIs(context.json(), parsed)
                self.assertEqual(context.json(), payload)
            self.assertEqual(decoder.call_count, 1)
        self.assertEqual(context.handler.rfile.reads, 1)
        context.handler.body_received.assert_called_once()
        self.assertEqual(context.query, {"language": ["c17"]})

    def test_empty_body_and_empty_object_are_cached(self):
        for raw, expected_decodes in ((b"", 0), (b"{}", 1)):
            with self.subTest(raw=raw):
                context = self.context(raw)
                with patch("ott.api.json.loads", wraps=json.loads) as decoder:
                    parsed = context.json()
                    self.assertEqual(parsed, {})
                    self.assertIs(context.json(), parsed)
                    self.assertEqual(decoder.call_count, expected_decodes)
                context.handler.body_received.assert_called_once()

    def test_cache_does_not_change_complete_body_receipt_time(self):
        with patch("ott.api.time.time", side_effect=(10.0, 20.0)):
            context = self.context()
            self.assertEqual(context.received_at, 10.0)
            context.json()
            self.assertEqual(context.received_at, 20.0)
            for _ in range(4):
                context.json()
            self.assertEqual(context.received_at, 20.0)

    def test_separate_requests_never_share_parsed_data(self):
        first = self.context(b'{"source":"first"}')
        second = self.context(b'{"source":"second"}')
        first.json()["source"] = "changed"
        self.assertEqual(second.json(), {"source": "second"})
        self.assertIsNot(first.json(), second.json())

    def test_duplicate_fields_including_nested_and_escaped_names_are_rejected(self):
        for raw in (b'{"source":"first","source":"second"}', b'{"nested":{"x":1,"x":2}}',
                    b'{"source":1,"so\\u0075rce":2}'):
            with self.subTest(raw=raw):
                context = self.context(raw)
                for _ in range(2):
                    with self.assertRaises(ApiError) as error:
                        context.json()
                    self.assertEqual((error.exception.status, error.exception.code), (400, "BAD_REQUEST"))
                    self.assertIsNone(context._json)
                self.assertEqual(context.handler.rfile.reads, 1)

    def test_all_nonfinite_numbers_and_overflow_exponents_are_rejected(self):
        for token in ("NaN", "Infinity", "-Infinity", "1e400", "-1e400", "1.9e308"):
            for template in ('{"value":%s}', '{"nested":[{"value":%s}]}'):
                with self.subTest(token=token, template=template):
                    context = self.context((template % token).encode())
                    with self.assertRaises(ApiError) as error:
                        context.json()
                    self.assertEqual(error.exception.status, 400)
                    self.assertIsNone(context._json)

    def test_finite_numbers_keep_default_decoder_values(self):
        raw = b'{"values":[-0.0,1e-400,1.7976931348623157e308,1e-10,12345678901234567890]}'
        parsed = self.context(raw).json()
        self.assertEqual(parsed, json.loads(raw))
        self.assertEqual(math.copysign(1.0, parsed["values"][0]), -1.0)

    def test_invalid_utf8_json_and_excessive_nesting_remain_rejected(self):
        for raw in (b'{"value":"\xff"}', b'{"value":}', b'{"value":"\x00"}', b'{} trailing',
                    b'{"nested":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'):
            with self.subTest(prefix=raw[:30]):
                context = self.context(raw)
                for _ in range(2):
                    with self.assertRaises(ApiError) as error:
                        context.json()
                    self.assertEqual(error.exception.status, 400)
                    self.assertIsNone(context._json)

    def test_nesting_boundary_is_checked_before_the_decoder(self):
        raw = b'{"nested":' + b'[' * (MAX_JSON_DEPTH - 1) + b'0' + b']' * (MAX_JSON_DEPTH - 1) + b'}'
        self.assertEqual(self.context(raw).json(), json.loads(raw))
        raw = b'{"nested":' + b'[' * MAX_JSON_DEPTH + b'0' + b']' * MAX_JSON_DEPTH + b'}'
        context = self.context(raw)
        with patch("ott.api.json.loads") as decoder, self.assertRaises(ApiError) as error:
            context.json()
        self.assertEqual(error.exception.status, 400)
        decoder.assert_not_called()

    def test_source_string_markers_and_escapes_do_not_count_as_nesting(self):
        randomizer = random.Random(427)
        alphabet = '{}[]"\\\n\t abc🌏'
        for source in ('[' * 1000, '}' * 1000, '\\"' * 1000, '\\\\"' * 1000,
                       *("".join(randomizer.choices(alphabet, k=500)) for _ in range(100))):
            with self.subTest(prefix=source[:20]):
                payload = {"source": source, "nested": {"value": "[{}]"}}
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.assertEqual(self.context(raw).json(), payload)

    def test_large_unterminated_strings_and_deep_structures_stop_before_decode(self):
        for raw in (b'{"source":"' + b'\\"' * 100000,
                    b'{"nested":' + b'[' * 100000 + b'0' + b']' * 100000 + b'}'):
            with self.subTest(length=len(raw)):
                context = self.context(raw)
                with patch("ott.api.json.loads") as decoder, self.assertRaises(ApiError) as error:
                    context.json()
                self.assertEqual(error.exception.status, 400)
                decoder.assert_not_called()

    def test_quote_search_preserves_backslash_parity_at_the_fallback_boundary(self):
        for count in range(21):
            for suffix in ('', '"[[[', '\n"{[]}'):
                source = '\\' * count + suffix
                with self.subTest(count=count, suffix=suffix):
                    payload = {"source": source}
                    self.assertEqual(self.context(json.dumps(payload).encode()).json(), payload)

    def test_root_must_remain_an_object(self):
        for raw in (b'null', b'true', b'false', b'[]', b'1.5', b'"source"'):
            with self.subTest(raw=raw), self.assertRaises(ApiError) as error:
                self.context(raw).json()
            self.assertEqual((error.exception.status, error.exception.message), (400, "Body must be a JSON object."))

    def test_oversized_or_incomplete_body_is_never_cached(self):
        context = self.context(b'{}', maximum=1)
        with self.assertRaises(ApiError) as error:
            context.json()
        self.assertEqual(error.exception.status, 413)
        self.assertEqual(context.handler.rfile.reads, 0)
        self.assertIsNone(context._json)
        context = self.context(b'{}', headers=(("Content-Length", "20"), ("Content-Type", "application/json")))
        with self.assertRaises(ApiError) as error:
            context.json()
        self.assertEqual(error.exception.status, 400)
        context.handler.body_received.assert_not_called()
        self.assertIsNone(context._json)

    def test_framing_encoding_and_media_checks_run_before_decode(self):
        for headers, expected in (
            ((("Transfer-Encoding", ""),), 400),
            ((("Content-Length", "2"), ("Content-Length", "2")), 400),
            ((("Content-Length", "-1"),), 400),
            ((("Content-Length", "2"), ("Content-Encoding", "gzip")), 415),
            ((("Content-Length", "2"), ("Content-Type", "text/plain")), 415),
        ):
            with self.subTest(headers=headers):
                context = self.context(headers=headers)
                with patch("ott.api.json.loads") as decoder, self.assertRaises(ApiError) as error:
                    context.json()
                self.assertEqual(error.exception.status, expected)
                decoder.assert_not_called()
                self.assertEqual(context.handler.rfile.reads, 0)
                self.assertIsNone(context._json)

    def test_http_draft_save_decodes_once_and_rejects_unsafe_json_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "ott.db")
            with store.tx() as con:
                con.execute("INSERT INTO users(id,username,password_hash,role,created_at) VALUES (1,'player','unused','CONTESTANT',0)")
            tournament = store.create_tournament("JSON Cup", 300, 1)
            entry = store.create_entry(tournament["id"], 1)
            with store.tx() as con:
                con.execute("UPDATE tournaments SET status='CODING',coding_started_at=?,coding_deadline=? WHERE id=?",
                            (int(time.time()) - 5, int(time.time()) + 3600, tournament["id"]))
            token, csrf = store.create_session(1)
            app = make_app(store, root, root, policy=ServerPolicy(dispatch_enabled=False))
            handler = type("JSONTestHandler", (Handler,), {"app": app})
            server = BoundedHTTPServer(("127.0.0.1", 0), handler, app)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            source = "// chiến thuật 🌏\n" * 1000 + "x" * 200000
            valid = json.dumps({"source": source, "language": "c17"}, ensure_ascii=False).encode("utf-8")
            cases = ((valid, 200, 1), (b'{"source":"changed","value":1e400}', 400, 1),
                     (b'{"value":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}', 400, 0))
            try:
                for raw, status, decodes in cases:
                    with self.subTest(status=status, decodes=decodes):
                        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                        try:
                            with patch("ott.api.json.loads", wraps=json.loads) as decoder:
                                client.request("PUT", f"/api/v1/tournaments/{tournament['id']}/code", body=raw,
                                               headers={"Cookie": "ott_session=" + token, "X-CSRF-Token": csrf,
                                                        "Content-Type": "application/json"})
                                response = client.getresponse()
                                response.read()
                                self.assertEqual(response.status, status)
                                self.assertEqual(decoder.call_count, decodes)
                            draft = store.get_draft(entry["id"], with_language=True)
                            self.assertEqual((draft[0], draft[2]), (source, "c17"))
                        finally:
                            client.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

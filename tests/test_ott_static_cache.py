import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ott.api import (STATIC_CACHE_MAX_BYTES, STATIC_CACHE_MAX_ENTRIES, Handler,
                     StaticFileCache)


class RequestHeaders:
    def __init__(self, etag=None):
        self.values = {"If-None-Match": etag} if etag is not None else {}

    def get(self, name, default=""):
        return self.values.get(name, default)


def serve_static(root, etag=None, app=None):
    status, headers, body = [], [], io.BytesIO()
    handler = SimpleNamespace(
        app=app or SimpleNamespace(static_dir=root, static_cache=StaticFileCache()),
        headers=RequestHeaders(etag), command="GET",
        request_id="static-test", wfile=body,
        send_response=status.append,
        send_header=lambda name, value: headers.append((name, value)),
        end_headers=lambda: None,
    )
    handler._security_headers = Handler._security_headers.__get__(handler, Handler)
    Handler._static(handler, "/")
    return status[0], dict(headers), body.getvalue()


class StaticCacheTests(unittest.TestCase):
    def test_static_file_supports_etag_revalidation_without_caching_api_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = b"<!doctype html><title>OTT</title>"
            (root / "index.html").write_bytes(content)
            app = SimpleNamespace(static_dir=root, static_cache=StaticFileCache())

            status, headers, body = serve_static(root, app=app)
            self.assertEqual(status, 200)
            self.assertEqual(body, content)
            self.assertEqual(headers["Cache-Control"], "no-cache")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertRegex(headers["ETag"], r'^"[0-9a-f]{64}"$')

            with patch.object(Path, "read_bytes", side_effect=AssertionError("cache hit read from disk")):
                status, headers, body = serve_static(root, '"other", W/' + headers["ETag"], app)
            self.assertEqual(status, 304)
            self.assertEqual(body, b"")
            self.assertEqual(headers["Cache-Control"], "no-cache")

            defaults = []
            handler = SimpleNamespace(send_header=lambda name, value: defaults.append((name, value)))
            Handler._security_headers(handler)
            self.assertIn(("Cache-Control", "no-store"), defaults)

    def test_changed_static_content_gets_new_etag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "index.html"
            app = SimpleNamespace(static_dir=root, static_cache=StaticFileCache())
            target.write_bytes(b"first")
            first = serve_static(root, app=app)[1]["ETag"]
            target.write_bytes(b"second")
            second = serve_static(root, app=app)[1]["ETag"]
            self.assertNotEqual(first, second)

    def test_cache_memory_stays_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = StaticFileCache()
            for index in range(STATIC_CACHE_MAX_ENTRIES + 8):
                target = root / f"{index}.txt"
                target.write_bytes(b"static")
                cache.read(target)
            self.assertLessEqual(len(cache._entries), STATIC_CACHE_MAX_ENTRIES)
            self.assertLessEqual(cache._size, STATIC_CACHE_MAX_BYTES)


if __name__ == "__main__":
    unittest.main()

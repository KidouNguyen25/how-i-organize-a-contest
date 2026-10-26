import unittest
from email.message import Message

from ott.security import RequestRejected, ServerPolicy


class RequestFirewallTests(unittest.TestCase):
    def validate(self, pairs=(), method="POST", target="/api/v1/tournaments"):
        headers = Message()
        for key, value in pairs:
            headers[key] = value
        ServerPolicy(max_body=1024).validate_request(headers, method, target)

    def rejected(self, pairs=(), *, status=400, method="POST", target="/api/v1/tournaments"):
        with self.assertRaises(RequestRejected) as error:
            self.validate(pairs, method, target)
        self.assertEqual(error.exception.status, status)

    def test_valid_requests_and_body_limit_boundary(self):
        self.validate((("Content-Length", "1024"), ("Content-Type", "application/json")))
        self.validate((("Content-Length", "0"),), method="GET")
        self.validate((("Sec-Fetch-Site", "same-origin"),))
        self.validate(method="GET", target="/api/v1/bot-template?language=rust2021")
        self.rejected((("Content-Length", "1025"),), status=413)

    def test_duplicate_sensitive_headers_are_rejected_case_insensitively(self):
        names = ("Host", "Content-Length", "Content-Type", "Content-Encoding", "Origin",
                 "Cookie", "X-CSRF-Token", "X-Contest-Origin-Key", "X-Contest-Client-IP")
        for name in names:
            with self.subTest(name=name):
                self.rejected(((name, "1"), (name.lower(), "1")))

    def test_transfer_encoding_is_always_rejected(self):
        for value in ("", "chunked", "identity", "gzip, chunked"):
            with self.subTest(value=value):
                self.rejected((("Transfer-Encoding", value),))

    def test_malformed_content_length_is_rejected(self):
        for value in ("-1", "+1", "1, 1", "1.0", " 1", "1 ", "", "１", "9" * 1000):
            with self.subTest(value=value):
                self.rejected((("Content-Length", value),))

    def test_body_on_safe_methods_is_rejected(self):
        for method in ("GET", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                self.rejected((("Content-Length", "1"),), method=method)

    def test_invalid_targets_are_rejected(self):
        for target in ("", "https://attacker.example/", "//attacker.example/", "/a#b", "/a b", "/a\x00", "/a\x7f"):
            with self.subTest(target=target):
                self.rejected(target=target)

    def test_header_budget_is_bounded(self):
        self.validate((("X", "a" * (32768 - 5)),))
        self.rejected((("X", "a" * (32768 - 4)),), status=431)

    def test_cross_site_mutations_are_rejected(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.rejected((("Sec-Fetch-Site", "cross-site"),), status=403, method=method)
        self.validate((("Sec-Fetch-Site", "cross-site"),), method="GET")

    def test_control_characters_in_sensitive_headers_are_rejected(self):
        for value in ("a\rb", "a\nb", "a\x00b"):
            with self.subTest(value=value):
                self.rejected((("Cookie", value),))

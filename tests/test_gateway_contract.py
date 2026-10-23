"""Regression tests for the Cloudflare BFF to private-origin security contract.

These unit tests do not constitute a deployed Cloudflare/Tunnel acceptance run.
"""
from __future__ import annotations

import unittest
from email.message import Message

from ott.api import MAX_BODY, MAX_SOURCE
from ott.security import RequestRejected, ServerPolicy

PUBLIC_ORIGIN = "https://contest.example.workers.dev"
KEY_CURRENT = "a" * 40
KEY_PREVIOUS = "b" * 40


def headers(**items: str) -> Message:
    result = Message()
    for key, value in items.items():
        result.add_header(key.replace("_", "-"), value)
    return result


class GatewayContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ServerPolicy(public_origin=PUBLIC_ORIGIN, origin_keys=(KEY_CURRENT, KEY_PREVIOUS))

    def test_origin_key_required_for_reads_as_well_as_mutations(self) -> None:
        with self.assertRaises(RequestRejected) as ctx:
            self.policy.authenticate(headers(), "GET")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (403, "ORIGIN_AUTH_REQUIRED"))
        with self.assertRaises(RequestRejected):
            self.policy.authenticate(headers(X_Contest_Origin_Key="bad", Origin=PUBLIC_ORIGIN), "POST")

    def test_key_rotation_accepts_current_and_previous_key(self) -> None:
        for key in (KEY_CURRENT, KEY_PREVIOUS):
            with self.subTest(key=key[:2]):
                self.assertTrue(self.policy.authenticate(headers(X_Contest_Origin_Key=key), "GET"))
                self.assertTrue(self.policy.authenticate(
                    headers(X_Contest_Origin_Key=key, Origin=PUBLIC_ORIGIN), "POST"))

    def test_mutations_require_exact_public_origin(self) -> None:
        for origin in (None, "https://wrong.example", "http://contest.example.workers.dev",
                       PUBLIC_ORIGIN + "/"):
            with self.subTest(origin=origin):
                values = {"X_Contest_Origin_Key": KEY_CURRENT}
                if origin is not None:
                    values["Origin"] = origin
                with self.assertRaises(RequestRejected) as ctx:
                    self.policy.authenticate(headers(**values), "POST")
                self.assertEqual(ctx.exception.status, 403)

    def test_ambiguous_headers_and_cross_site_requests_rejected(self) -> None:
        duplicate = headers(X_Contest_Origin_Key=KEY_CURRENT, Origin=PUBLIC_ORIGIN)
        duplicate.add_header("Origin", PUBLIC_ORIGIN)
        with self.assertRaises(RequestRejected):
            self.policy.validate_request(duplicate, "POST", "/api/v1/auth/logout")
        bad_site = headers(X_Contest_Origin_Key=KEY_CURRENT, Origin=PUBLIC_ORIGIN,
                           Sec_Fetch_Site="cross-site")
        with self.assertRaises(RequestRejected) as ctx:
            self.policy.validate_request(bad_site, "POST", "/api/v1/auth/logout")
        self.assertEqual(ctx.exception.status, 403)

    def test_limits_match_gateway_contract(self) -> None:
        self.assertEqual(MAX_BODY, 1024 * 1024)
        self.assertEqual(MAX_SOURCE, 256 * 1024)
        self.assertEqual(ServerPolicy().max_body, MAX_BODY)
        oversized = headers(Content_Length=str(MAX_BODY + 1))
        with self.assertRaises(RequestRejected) as ctx:
            self.policy.validate_request(oversized, "POST", "/api/v1/auth/login")
        self.assertEqual(ctx.exception.status, 413)

    def test_gateway_requires_both_origin_and_strong_key(self) -> None:
        with self.assertRaises(ValueError):
            ServerPolicy(public_origin=PUBLIC_ORIGIN)
        with self.assertRaises(ValueError):
            ServerPolicy(origin_keys=(KEY_CURRENT,))
        with self.assertRaises(ValueError):
            ServerPolicy(public_origin=PUBLIC_ORIGIN, origin_keys=("short",))


if __name__ == "__main__":
    unittest.main()

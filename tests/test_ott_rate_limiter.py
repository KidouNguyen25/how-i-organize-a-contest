import math
import random
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ott.api import ApiError, make_app
from ott.security import RateLimiter
from ott.store import Store


class RateLimiterTests(unittest.TestCase):
    def test_identity_churn_cannot_reset_an_exhausted_bucket(self):
        limiter = RateLimiter(capacity=3)
        with patch("ott.security.time.monotonic", return_value=0.0):
            for _ in range(10):
                self.assertTrue(limiter.allow("victim", burst=10, per_second=1 / 30))
            self.assertFalse(limiter.allow("victim", burst=10, per_second=1 / 30))
            for index in range(100):
                limiter.allow(f"other-{index}", burst=10, per_second=1 / 30)
            self.assertFalse(limiter.allow("victim", burst=10, per_second=1 / 30))
        self.assertEqual(len(limiter._buckets), 3)
        self.assertLessEqual(len(limiter._expiry), 6)

    def test_only_fully_refilled_buckets_can_be_replaced(self):
        limiter = RateLimiter(capacity=1)
        with patch("ott.security.time.monotonic") as clock:
            clock.return_value = 0.0
            self.assertTrue(limiter.allow("old", burst=10, per_second=1 / 30))
            clock.return_value = 29.999
            self.assertFalse(limiter.allow("new", burst=10, per_second=1 / 30))
            clock.return_value = 30.0
            self.assertTrue(limiter.allow("new", burst=10, per_second=1 / 30))
            self.assertNotIn("old", limiter._buckets)

    def test_denials_do_not_extend_bucket_lifetime(self):
        limiter = RateLimiter(capacity=1)
        with patch("ott.security.time.monotonic") as clock:
            clock.return_value = 0.0
            self.assertTrue(limiter.allow("old", burst=1, per_second=1))
            for stamp in (0.1, 0.2, 0.9):
                clock.return_value = stamp
                self.assertFalse(limiter.allow("old", burst=1, per_second=1))
            clock.return_value = 1.0
            self.assertTrue(limiter.allow("new", burst=1, per_second=1))

    def test_stale_expiry_never_deletes_a_recently_used_bucket(self):
        limiter = RateLimiter(capacity=1)
        with patch("ott.security.time.monotonic") as clock:
            clock.return_value = 0.0
            self.assertTrue(limiter.allow("old", burst=2, per_second=1))
            clock.return_value = 0.5
            self.assertTrue(limiter.allow("old", burst=2, per_second=1))
            clock.return_value = 1.0
            self.assertFalse(limiter.allow("new", burst=2, per_second=1))
            self.assertIn("old", limiter._buckets)
            clock.return_value = 2.0
            self.assertTrue(limiter.allow("new", burst=2, per_second=1))

    def test_expiry_storage_remains_bounded_during_long_hot_key_traffic(self):
        limiter = RateLimiter(capacity=8)
        with patch("ott.security.time.monotonic") as clock:
            for index in range(10000):
                clock.return_value = index / 10
                limiter.allow(f"key-{index % 8}", burst=240, per_second=60)
                self.assertLessEqual(len(limiter._buckets), 8)
                self.assertLessEqual(len(limiter._expiry), 16)

    def test_replay_preserves_token_bucket_decisions_without_capacity_pressure(self):
        limiter = RateLimiter(capacity=64)
        reference = {}
        randomizer = random.Random(724)
        stamp = 0.0
        with patch("ott.security.time.monotonic") as clock:
            for _ in range(20000):
                stamp += randomizer.random() / 10
                clock.return_value = stamp
                number = randomizer.randrange(64)
                key = str(number)
                burst, rate = ((240, 60), (30, 1), (180, 30), (10, 1 / 30))[number % 4]
                tokens, previous = reference.get(key, (float(burst), stamp))
                tokens = min(float(burst), tokens + max(0.0, stamp - previous) * rate)
                accepted = tokens >= 1
                reference[key] = (tokens - 1 if accepted else tokens, stamp)
                self.assertEqual(limiter.allow(key, burst=burst, per_second=rate), accepted)

    def test_simultaneous_callers_share_one_atomic_quota(self):
        limiter = RateLimiter()
        barrier = threading.Barrier(16)

        def attempt(_):
            barrier.wait(timeout=5)
            return limiter.allow("shared", burst=7, per_second=1)

        with patch("ott.security.time.monotonic", return_value=0.0), ThreadPoolExecutor(max_workers=16) as pool:
            self.assertEqual(sum(pool.map(attempt, range(16))), 7)

    def test_invalid_capacity_and_refill_policies_are_rejected(self):
        for capacity in (0, -1, True, 1.5, None):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                RateLimiter(capacity)
        limiter = RateLimiter()
        for burst, rate in ((0, 1), (True, 1), (1, 0), (1, -1), (1, math.nan), (1, math.inf), (1, True), (1, None)):
            with self.subTest(burst=burst, rate=rate), self.assertRaises(ValueError):
                limiter.allow("key", burst=burst, per_second=rate)
        limiter.allow("key", burst=1, per_second=1)
        with self.assertRaisesRegex(ValueError, "fixed"):
            limiter.allow("key", burst=100, per_second=1)
        for burst, rate in ((1.0, 1), (True, 1), (1, True), (1, "1"), (1, math.nan)):
            with self.subTest(burst=burst, rate=rate), self.assertRaises(ValueError):
                limiter.allow("key", burst=burst, per_second=rate)

    def test_login_identity_churn_does_not_consume_other_admission_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "ott.db")
            app = make_app(store, root, Path(__file__).resolve().parents[1] / "mockup")
            app.login_limiter = RateLimiter(capacity=1)
            handler = next(handler for method, pattern, handler, _ in app.routes
                           if method == "POST" and pattern.fullmatch("/auth/login"))
            context = SimpleNamespace(json=lambda: {"username": "player", "password": "bad"})
            with patch.object(store, "verify_login", return_value=None) as verify:
                for expected in (401, 429):
                    if expected == 429:
                        context.json = lambda: {"username": "another", "password": "bad"}
                    with self.assertRaises(ApiError) as error:
                        handler(context)
                    self.assertEqual(error.exception.status, expected)
                self.assertEqual(verify.call_count, 1)
            for limiter in (app.rate_limiter, app.auth_limiter, app.account_limiter):
                self.assertTrue(limiter.allow("other-client", burst=1, per_second=1))

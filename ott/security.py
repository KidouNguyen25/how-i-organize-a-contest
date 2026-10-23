"""Request authentication and bounded admission at the private API origin."""

from __future__ import annotations

import heapq
import hmac
import ipaddress
import math
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit


class RequestRejected(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class ServerPolicy:
    public_origin: str | None = None
    origin_keys: tuple[str, ...] = field(default=(), repr=False)
    allow_stream: bool = True
    max_body: int = 1024 * 1024
    connection_limit: int = 64
    dispatch_enabled: bool = True
    demo: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.connection_limit <= 256 or not 1 <= self.max_body <= 1024 * 1024:
            raise ValueError("Invalid connection or body limit.")
        if bool(self.public_origin) != bool(self.origin_keys):
            raise ValueError("Gateway requires both PUBLIC_ORIGIN and origin keys.")
        if self.origin_keys:
            if not self.public_origin or any(not re.fullmatch(r"[!-~]{32,256}", key) or key.startswith("REPLACE_") for key in self.origin_keys):
                raise ValueError("Gateway requires PUBLIC_ORIGIN and ASCII origin keys of at least 32 characters.")
        if self.public_origin:
            parsed = urlsplit(self.public_origin)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path or parsed.query or parsed.fragment or parsed.netloc != parsed.netloc.lower()):
                raise ValueError("PUBLIC_ORIGIN must be an HTTPS origin without a path or credentials.")
            _ = parsed.port

    def authenticate(self, headers: object, method: str) -> bool:
        """Authenticate the gateway before trusting any identity headers."""
        proxied = False
        if self.origin_keys:
            values = headers.get_all("X-Contest-Origin-Key", [])  # type: ignore[attr-defined]
            supplied = values[0] if len(values) == 1 else ""
            # Compare every key to support short overlap during key rotation.
            matches = [hmac.compare_digest(supplied.encode("utf-8"), key.encode("ascii"))
                       for key in self.origin_keys]
            if not any(matches):
                raise RequestRejected(403, "ORIGIN_AUTH_REQUIRED", "Private origin authentication required.")
            proxied = True
        if method not in ("GET", "HEAD", "OPTIONS"):
            origins = headers.get_all("Origin", [])  # type: ignore[attr-defined]
            if len(origins) > 1:
                raise RequestRejected(403, "FORBIDDEN", "Origin must appear only once.")
            origin = origins[0] if len(origins) == 1 else None
            if self.public_origin:
                if origin != self.public_origin:
                    raise RequestRejected(403, "FORBIDDEN", "Public origin does not match.")
            elif origin:
                parsed = urlsplit(origin)
                hosts = headers.get_all("Host", [])  # type: ignore[attr-defined]
                if (len(hosts) != 1 or parsed.scheme not in ("http", "https") or parsed.username
                        or parsed.password or parsed.path or parsed.query or parsed.fragment
                        or parsed.netloc != hosts[0]):
                    raise RequestRejected(403, "FORBIDDEN", "Cross-origin request rejected.")
        return proxied

    def validate_request(self, headers: object, method: str, target: str) -> None:
        """Reject ambiguous HTTP input before routing, session lookup or body parsing."""
        if (not target.startswith("/") or target.startswith("//") or "#" in target
                or any(ord(char) < 33 or ord(char) == 127 for char in target)):
            raise RequestRejected(400, "BAD_REQUEST", "Invalid request target.")
        if sum(len(key) + len(value) + 4 for key, value in headers.items()) > 32768:  # type: ignore[attr-defined]
            raise RequestRejected(431, "HEADERS_TOO_LARGE", "Request headers exceed 32 KiB.")
        for key in ("Host", "Content-Length", "Content-Type", "Content-Encoding", "Origin",
                    "Cookie", "X-CSRF-Token", "X-Contest-Origin-Key", "X-Contest-Client-IP"):
            values = headers.get_all(key, [])  # type: ignore[attr-defined]
            if len(values) > 1 or any("\r" in value or "\n" in value or "\x00" in value for value in values):
                raise RequestRejected(400, "BAD_REQUEST", "Ambiguous or invalid request header.")
        if headers.get_all("Transfer-Encoding", []):  # type: ignore[attr-defined]
            raise RequestRejected(400, "BAD_REQUEST", "Transfer-Encoding is not supported.")
        lengths = headers.get_all("Content-Length", [])  # type: ignore[attr-defined]
        if lengths:
            value = lengths[0]
            if not value.isascii() or not value.isdigit() or len(value) > 10:
                raise RequestRejected(400, "BAD_REQUEST", "Invalid Content-Length.")
            length = int(value)
            if length > self.max_body:
                raise RequestRejected(413, "PAYLOAD_TOO_LARGE", "Request body too large.")
            if method in ("GET", "HEAD", "OPTIONS") and length:
                raise RequestRejected(400, "BAD_REQUEST", "This method does not accept a request body.")
        if method not in ("GET", "HEAD", "OPTIONS") and headers.get("Sec-Fetch-Site") == "cross-site":  # type: ignore[attr-defined]
            raise RequestRejected(403, "FORBIDDEN", "Cross-site mutation rejected.")

    def client_ip(self, headers: object, peer_ip: str, proxied: bool) -> str:
        if proxied:
            values = headers.get_all("X-Contest-Client-IP", [])  # type: ignore[attr-defined]
            if len(values) == 1:
                try:
                    return str(ipaddress.ip_address(values[0]))
                except ValueError:
                    pass
        return peer_ip


@dataclass(slots=True)
class _RateBucket:
    tokens: float
    updated_at: float
    full_at: float
    serial: int
    burst: int
    per_second: float


class RateLimiter:
    """Bounded token buckets; identity churn never resets an active restriction."""

    def __init__(self, capacity: int = 4096) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("Rate limiter capacity must be a positive integer.")
        self.capacity = capacity
        self._buckets: dict[str, _RateBucket] = {}
        self._expiry: list[tuple[float, int, str]] = []
        self._serial = 0
        self._lock = threading.Lock()

    def _expire(self, stamp: float) -> None:
        while self._expiry and self._expiry[0][0] <= stamp:
            _, serial, key = heapq.heappop(self._expiry)
            bucket = self._buckets.get(key)
            if bucket is not None and bucket.serial == serial:
                del self._buckets[key]

    def allow(self, key: str, *, burst: int, per_second: float) -> bool:
        with self._lock:
            stamp = time.monotonic()
            bucket = self._buckets.get(key)
            if bucket is None:
                if (type(burst) is not int or burst < 1
                        or type(per_second) not in (int, float)
                        or not math.isfinite(per_second) or per_second <= 0):
                    raise ValueError("Rate limiter requires a positive burst and finite positive refill rate.")
                self._expire(stamp)
                if len(self._buckets) >= self.capacity:
                    return False
                tokens = float(burst)
            else:
                if (type(burst) is not int or type(per_second) not in (int, float)
                        or bucket.burst != burst or bucket.per_second != per_second):
                    raise ValueError("Rate limiter policy must remain fixed for each key.")
                tokens = min(float(burst), bucket.tokens + max(0.0, stamp - bucket.updated_at) * per_second)
            if tokens < 1:
                return False
            tokens -= 1
            full_at = stamp + (burst - tokens) / per_second
            self._serial += 1
            if bucket is None:
                bucket = _RateBucket(tokens, stamp, full_at, self._serial, burst, per_second)
                self._buckets[key] = bucket
            else:
                bucket.tokens, bucket.updated_at, bucket.full_at, bucket.serial = tokens, stamp, full_at, self._serial
            heapq.heappush(self._expiry, (full_at, self._serial, key))
            if len(self._expiry) > 2 * self.capacity:
                self._expiry = [(item.full_at, item.serial, name) for name, item in self._buckets.items()]
                heapq.heapify(self._expiry)
            return True

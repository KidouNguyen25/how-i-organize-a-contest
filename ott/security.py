"""Request authentication and bounded admission at the private API origin."""

from __future__ import annotations

import hmac
import ipaddress
import re
import threading
import time
from collections import OrderedDict
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

    def client_ip(self, headers: object, peer_ip: str, proxied: bool) -> str:
        if proxied:
            values = headers.get_all("X-Contest-Client-IP", [])  # type: ignore[attr-defined]
            if len(values) == 1:
                try:
                    return str(ipaddress.ip_address(values[0]))
                except ValueError:
                    pass
        return peer_ip


class RateLimiter:
    """Token buckets with bounded memory; callers choose stable, private keys."""

    def __init__(self, capacity: int = 4096) -> None:
        self.capacity = capacity
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str, *, burst: int, per_second: float) -> bool:
        stamp = time.monotonic()
        with self._lock:
            tokens, previous = self._buckets.pop(key, (float(burst), stamp))
            tokens = min(float(burst), tokens + max(0.0, stamp - previous) * per_second)
            accepted = tokens >= 1
            self._buckets[key] = (tokens - 1 if accepted else tokens, stamp)
            while len(self._buckets) > self.capacity:
                self._buckets.popitem(last=False)
            return accepted

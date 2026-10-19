"""Line-delimited JSON protocol constants and validation helpers."""

from __future__ import annotations

from typing import Any

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024
SUPPORTED_OPERATIONS = {
    "REGISTER",
    "LOGIN",
    "LIST_PROBLEMS",
    "GET_PROBLEM",
    "SUBMIT",
    "GET_SUBMISSION",
    "LIST_MY_SUBMISSIONS",
}


class RequestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RequestError("INVALID_REQUEST", "Request must be a JSON object.")
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != PROTOCOL_VERSION:
        raise RequestError("UNSUPPORTED_VERSION", "Protocol version must be 1.")
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise RequestError("INVALID_REQUEST", "request_id must be a non-empty string of at most 128 characters.")
    operation = value.get("op")
    if not isinstance(operation, str) or operation not in SUPPORTED_OPERATIONS:
        raise RequestError("INVALID_REQUEST", "op is missing or unsupported.")
    token = value.get("token")
    if token is not None and not isinstance(token, str):
        raise RequestError("INVALID_REQUEST", "token must be a string.")
    data = value.get("data", {})
    if not isinstance(data, dict):
        raise RequestError("INVALID_REQUEST", "data must be a JSON object.")
    return {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "op": operation,
        "token": token,
        "data": data,
    }


def success(request_id: str | None, data: dict[str, Any]) -> dict[str, Any]:
    return {"request_id": request_id, "ok": True, "data": data}


def failure(request_id: str | None, code: str, message: str) -> dict[str, Any]:
    return {"request_id": request_id, "ok": False, "error": {"code": code, "message": message}}


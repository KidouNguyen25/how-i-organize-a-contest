"""Verdict and points calculation."""

from __future__ import annotations

from typing import Any


def output_tokens(value: bytes) -> list[bytes]:
    return value.split()


def summarize_cases(cases: list[dict[str, Any]]) -> tuple[str, int]:
    score = sum(case["points"] for case in cases if case["verdict"] == "AC")
    if score == 100:
        return "AC", score
    if score > 0:
        return "PARTIAL", score
    verdicts = {case["verdict"] for case in cases}
    if "TLE" in verdicts:
        return "TLE", score
    if "RE" in verdicts:
        return "RE", score
    return "WA", score


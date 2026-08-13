"""Shared visual-review outcome states for forward image search."""

from __future__ import annotations

from enum import Enum


class ReviewStatus(str, Enum):
    NOT_RUN = "not_run"
    MATCHED = "matched"
    NO_MATCH = "no_match"
    ERROR = "error"


def review_status_value(value: object) -> str:
    if isinstance(value, ReviewStatus):
        return value.value
    normalized = str(value or "").strip().lower()
    if normalized in {status.value for status in ReviewStatus}:
        return normalized
    return ReviewStatus.NOT_RUN.value

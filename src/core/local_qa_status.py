"""Strict credential-free status projection for local QA evidence."""

from typing import Any, Dict, Mapping, Optional


QA_STATUS_FIELDS = (
    "model",
    "account_id",
    "stage",
    "status",
    "error_class",
    "has_media",
    "duration",
    "attempt_count",
    "delivery_mode",
)

QA_POOL_COUNT_FIELDS = (
    "account_records",
    "active_accounts",
    "inactive_accounts",
    "quota_reservations",
    "image_inflight",
    "video_inflight",
    "browser_reservations",
    "browser_inflight",
)


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _non_negative_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_local_qa_status(
    record: Mapping[str, Any],
    *,
    pool_counts: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return only the approved local-QA fields and aggregate pool counts."""
    account_id = record.get("account_id")
    if account_id is None:
        account_id = record.get("token_id")

    result: Dict[str, Any] = {
        "model": str(record.get("model") or "").strip(),
        "account_id": _optional_int(account_id),
        "stage": str(record.get("stage") or "").strip(),
        "status": str(record.get("status") or "").strip(),
        "error_class": str(record.get("error_class") or "").strip(),
        "has_media": bool(record.get("has_media")),
        "duration": _non_negative_float(record.get("duration")),
        "attempt_count": _non_negative_int(record.get("attempt_count")),
        "delivery_mode": str(record.get("delivery_mode") or "").strip(),
    }

    if pool_counts is not None:
        result["pool_counts"] = {
            field: _non_negative_int(pool_counts.get(field))
            for field in QA_POOL_COUNT_FIELDS
        }

    return result

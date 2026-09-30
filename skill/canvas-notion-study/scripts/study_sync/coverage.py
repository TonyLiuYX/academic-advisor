"""Collection provenance: distinguish fresh reads from recoverable old data."""
from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Mapping


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def object_ids(value):
    records = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(str(item.get("id", item.get("page_id", item.get("url"))))
                             for item in records if isinstance(item, Mapping)
                             and item.get("id", item.get("page_id", item.get("url"))) is not None))


def mark_records(value, status, timestamp):
    if isinstance(value, list):
        return [mark_records(item, status, timestamp) for item in value]
    if isinstance(value, Mapping):
        result = dict(value)
        result["coverage_status"] = status
        if status == "fresh":
            result["retrieved_at"] = timestamp
        return result
    return value


def record_read(client, path, params, value, succeeded, *, pagination=None, error=None):
    now = utc_now()
    status = "fresh" if succeeded else "stale" if value else "unavailable"
    records = value if isinstance(value, list) else [value]
    prior_times = [str(item["retrieved_at"]) for item in records
                   if isinstance(item, Mapping) and item.get("retrieved_at")]
    entry = {"endpoint": path, "params": dict(params or {}), "status": status,
             "checked_at": now, "retrieved_at": now if succeeded else max(prior_times, default=None),
             "ids": object_ids(value), "count": len(value) if isinstance(value, list) else int(bool(value)),
             "page_count": (pagination or {}).get("page_count", int(succeeded)),
             "pagination_complete": bool(succeeded and (pagination or {}).get("complete", True))}
    if pagination and pagination.get("pages") is not None:
        entry["pages"] = pagination["pages"]
        entry["fresh_ids"] = pagination.get("ids", [])
    if error:
        entry["error"] = error
    if not hasattr(client, "coverage_log"):
        client.coverage_log = []
    client.coverage_log.append(entry)
    return mark_records(value, status, now)


def summarize(entries):
    entries = list(entries)
    statuses = {entry["status"] for entry in entries}
    status = "unavailable" if not entries or statuses == {"unavailable"} else "fresh" if statuses == {"fresh"} else "stale"
    return {"status": status, "checked_at": utc_now(), "entries": entries,
            "fresh": sum(e["status"] == "fresh" for e in entries),
            "stale": sum(e["status"] == "stale" for e in entries),
            "unavailable": sum(e["status"] == "unavailable" for e in entries)}

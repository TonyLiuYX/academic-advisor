"""Local learner facts and explicit, lossless Notion personal-field readback."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping
import json
import re
from urllib.parse import unquote, urlsplit

SCHEMA_VERSION = 2
PERSONAL_FIELDS = ("Done", "Planned", "Priority", "Personal Notes")


def timestamp(now: Any = None) -> str:
    if isinstance(now, datetime):
        return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).isoformat()
    return str(now) if now else datetime.now(timezone.utc).isoformat()


def empty_learner() -> dict:
    return {"schema_version": SCHEMA_VERSION, "profile": {}, "personal": {},
            "task_progress": {}, "weeks": {}, "sessions": {}, "imports": []}


def normalise_learner(learner: Mapping | None) -> dict:
    result = deepcopy(dict(learner or {}))
    version = result.get("schema_version", 1)
    if not isinstance(version, int) or version > SCHEMA_VERSION or version < 1:
        raise ValueError(f"Unsupported learner schema version: {version}")
    for key, value in empty_learner().items():
        result.setdefault(key, deepcopy(value))
    result["schema_version"] = SCHEMA_VERSION
    return result


def notion_value(value: Any) -> Any:
    """Decode actual Notion API property objects as well as flattened MCP rows."""
    if not isinstance(value, Mapping):
        return value
    type_ = value.get("type")
    if type_ == "checkbox" or "checkbox" in value:
        return value.get("checkbox")
    if type_ in ("title", "rich_text") or "rich_text" in value or "title" in value:
        parts = value.get(type_) if type_ in ("title", "rich_text") else value.get("rich_text", value.get("title"))
        return "".join(str(p.get("plain_text", p.get("text", {}).get("content", ""))) for p in (parts or []))
    if type_ == "date" or "date" in value:
        return deepcopy(value.get("date"))
    if type_ == "select" or "select" in value:
        selected = value.get("select")
        return selected.get("name") if isinstance(selected, Mapping) else selected
    if "value" in value:
        return value["value"]
    return deepcopy(dict(value))


def unpack_payload(payload: Any) -> Any:
    if isinstance(payload, str):
        return unpack_payload(json.loads(payload))
    if isinstance(payload, Mapping) and isinstance(payload.get("content"), list):
        texts = [item.get("text", "") for item in payload["content"] if item.get("type") == "text"]
        for text in texts:
            try:
                return unpack_payload(json.loads(text))
            except (ValueError, TypeError):
                continue
    return payload


def source_identity(value: Any) -> str | None:
    value = notion_value(value)
    if not isinstance(value, str):
        return None
    # Faithful Notion rich text may autolink only the URL/mail-like prefix;
    # the remainder of the stable key stays outside that Markdown link.
    value = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", lambda match: match.group(1), value.strip())
    return unquote(re.sub(r"\\+([:|])", r"\1", value).strip()) or None


def page_identity(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    path = urlsplit(value).path if "://" in value else value
    compact = path.rstrip("/").rsplit("/", 1)[-1].replace("-", "")
    match = re.search(r"([0-9a-fA-F]{32})$", compact)
    return match.group(1).lower() if match else compact


def personal_rows(payload: Any) -> list:
    payload = unpack_payload(payload)
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        raise ValueError("Personal readback must contain a rows/pages/records/results list.")
    for field in ("pages", "records", "results", "rows"):
        if field in payload:
            if not isinstance(payload[field], list):
                raise ValueError(f"{field} must be a list")
            return payload[field]
    if "properties" in payload or "source_key" in payload:
        return [payload]
    raise ValueError("Personal readback has no rows/pages/records/results list.")


def import_personal(learner: Mapping | None, payload: Any, notion_state: Mapping | None = None,
                    now: Any = None) -> tuple[dict, dict]:
    """Merge only fields actually read; explicit false/null/empty values clear.

    Missing pages or omitted properties never remove prior facts. Official
    properties are ignored. Older timestamped readback cannot revert a newer
    local user update.
    """
    result = normalise_learner(learner)
    at = timestamp(now)
    reverse = {}
    for key, record in (notion_state or {}).get("records", {}).items():
        page_id = record.get("page_id")
        if page_id:
            reverse.setdefault(page_identity(page_id), []).append(key)
    payload = unpack_payload(payload)
    complete = isinstance(payload, Mapping) and payload.get("has_more") is False and not payload.get("truncated")
    report = {"complete": complete, "completeness": "complete_supplied_page_set" if complete else "partial_or_unknown", "imported_at": at, "updated": [], "unchanged": [], "unresolved": [], "stale": [], "conflicts": []}
    observed = {}
    for index, row in enumerate(personal_rows(payload)):
        if not isinstance(row, Mapping):
            report["unresolved"].append({"row": index, "reason": "invalid_row"})
            continue
        props = row.get("properties", row)
        if not isinstance(props, Mapping):
            report["unresolved"].append({"row": index, "reason": "invalid_properties"})
            continue
        page_id = row.get("page_id", row.get("id", row.get("url")))
        key = source_identity(row.get("source_key") or props.get("Source Key"))
        matches = reverse.get(page_identity(page_id), [])
        if not key and len(matches) == 1:
            key = matches[0]
        if not isinstance(key, str) or not key:
            report["unresolved"].append({"row": index, "page_id": page_id, "reason": "missing_or_ambiguous_source_key"})
            continue
        if matches and key not in matches:
            report["conflicts"].append({"source_key": key, "page_id": page_id, "reason": "page_identity_mismatch"})
            continue
        explicit = {field: notion_value(props[field]) for field in PERSONAL_FIELDS if field in props}
        if "Notes" in props and "Personal Notes" not in explicit:
            explicit["Personal Notes"] = notion_value(props["Notes"])
        if any(k.startswith("date:Planned:") for k in props):
            old = result["personal"].get(key, {}).get("Planned")
            planned = deepcopy(old) if isinstance(old, Mapping) else ({"start": old} if old else {})
            for component in ("start", "end", "is_datetime"):
                field = f"date:Planned:{component}"
                if field in props:
                    planned[component] = props[field] or None
            explicit["Planned"] = planned if planned.get("start") else None
        if "Done" in explicit:
            value = explicit["Done"]
            if value in (True, "__YES__", "true", "True", 1):
                explicit["Done"] = True
            elif value in (False, None, "", "__NO__", "false", "False", 0):
                explicit["Done"] = False
            else:
                report["conflicts"].append({"source_key": key, "reason": "invalid_done_value"})
                explicit.pop("Done")
        if key in observed and observed[key] != explicit:
            report["conflicts"].append({"source_key": key, "reason": "duplicate_personal_rows"})
            continue
        observed[key] = explicit
        previous = result["personal"].get(key, {})
        source_at = row.get("last_edited_time", row.get("updated_at", at))
        try:
            older = bool(previous.get("updated_at")) and datetime.fromisoformat(str(source_at).replace("Z", "+00:00")) < datetime.fromisoformat(previous["updated_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            older = False
        if older:
            report["stale"].append(key)
            continue
        changed = {field: value for field, value in explicit.items() if field not in previous or previous[field] != value}
        if not changed:
            report["unchanged"].append(key)
            continue
        merged = deepcopy(previous)
        merged.update(explicit)
        merged.update({"updated_at": str(source_at), "read_at": at, "source": "notion_readback"})
        if page_id:
            merged["page_id"] = page_id
        result["personal"][key] = merged
        report["updated"].append({"source_key": key, "fields": sorted(changed)})
    report["read_complete"] = complete
    report["applied_complete"] = complete and not any(report[key] for key in ("unresolved", "stale", "conflicts"))
    result["personal_read_at"] = at
    result["updated_at"] = at
    result["imports"].append({k: len(report[k]) for k in ("updated", "unresolved", "stale", "conflicts")} | {"at": at})
    return result, report

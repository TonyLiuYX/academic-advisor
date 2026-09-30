"""Durable, deterministic outbox and receipts for host-run Notion tools."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

START = "**课程资料同步区**"
END = "**个人补充区**"
PERSONAL = "在此记录自己的计划和补充；同步会保留这一区域。"
PERSONAL_PROPERTIES = {"Done", "Planned", "Priority", "Personal Notes"}


def personal_property(name: str) -> bool:
    return name in PERSONAL_PROPERTIES or (name.startswith("date:") and name.split(":", 2)[1] in PERSONAL_PROPERTIES)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        if default is not None:
            return default
        raise FileNotFoundError(p)
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(path: str | Path, value: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_name(p.name + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, p)


def empty_state() -> dict:
    return {"schema_version": 1, "records": {}, "inflight": {}, "history": []}


def managed_page(content: str) -> str:
    return f"{START}\n\n{content.strip()}\n\n{END}\n\n{PERSONAL}"


def managed_region(page: str) -> str:
    if page.count(START) != 1 or page.count(END) != 1:
        raise ValueError("Managed region missing or ambiguous; fetch the page and preserve all personal content.")
    start = page.index(START) + len(START)
    end = page.index(END)
    if end < start:
        raise ValueError("Managed region markers are out of order.")
    return page[start:end].strip()


def content_edit(fetched_content: str, replacement: str, baseline: str | None = None) -> dict:
    current = managed_region(fetched_content)
    if baseline is not None and current.strip() != baseline.strip():
        raise ValueError("Managed content was edited since the last verified receipt; preserve it and report a conflict.")
    old = START + fetched_content.split(START, 1)[1].split(END, 1)[0] + END
    new = f"{START}\n\n{replacement.strip()}\n\n{END}"
    return {"old_str": old, "new_str": new}


def _properties(record: dict, state: dict, schema: dict | None = None) -> tuple[dict, list[str]]:
    props = {k: v for k, v in record.get("properties", {}).items() if not personal_property(k)}
    unresolved = []
    for name, value in list(props.items()):
        if schema is not None and schema.get('properties', {}).get(name, {}).get('type') != 'relation':
            continue
        if isinstance(value, list):
            mapped = []
            for entry in value:
                if isinstance(entry, str) and entry in state.get("records", {}):
                    mapped.append(state["records"][entry]["page_id"])
                elif isinstance(entry, str) and (schema is not None or entry.startswith("canvas:") or "|user:" in entry or entry.startswith("https://") and "|" in entry):
                    unresolved.append(entry)
                else:
                    mapped.append(entry)
            props[name] = mapped
    return props, unresolved


def record_fingerprint(record: dict) -> str:
    return digest({"kind": record["kind"], "properties": {k:v for k,v in record.get("properties",{}).items() if not personal_property(k)}, "generated_content": rendered_content(record)})


def clear_removed_source_properties(props: dict, previous: dict) -> dict:
    """Omitting an MCP property preserves it; explicitly clear removed source values."""
    result = dict(props)
    for name, value in previous.items():
        if name in props or personal_property(name):
            continue
        if name.startswith("date:"):
            if name.endswith(":start") or name.endswith(":end"):
                result[name] = ""
            elif name.endswith(":is_datetime"):
                result[name] = 0
        elif isinstance(value, list):
            result[name] = []
        elif value in ("__YES__", "__NO__"):
            result[name] = "__NO__"
        else:
            result[name] = ""
    return result


def rendered_content(record: dict) -> str:
    value = record.get("generated_content", "")
    if not isinstance(value, str):
        from .projection import render_generated_content
        value = render_generated_content(record)
    if record.get("note_materials"):
        links = [f'- {m["title"]}：<mention-page url="{m["url"]}"/>' for m in record["note_materials"]]
        value += "\n\n### 回顾上次课笔记\n" + "\n".join(links)
    return value


def linked_content(record: dict, state: dict) -> tuple[str, list[str]]:
    """Resolve stable internal references to existing, clickable Notion pages."""
    unresolved=[]
    def replace(match):
        key=match.group(1)
        page=state.get("records",{}).get(key,{}).get("page_id")
        if not page:
            unresolved.append(key)
            return match.group(0)
        url=page if str(page).startswith("https://") else "https://app.notion.com/p/"+str(page).replace("-","")
        return '<mention-page url="'+url+'"/>'
    return re.sub(r'\[\[record:(.*?)\]\]',replace,rendered_content(record)),unresolved


def tool_properties(properties: dict) -> dict:
    """A filesystem path is literal text, not Notion inline Markdown."""
    result = dict(properties)
    path = result.get("Local Path")
    if isinstance(path, str):
        result["Local Path"] = re.sub(r'([\\`*_{}\[\]()<>!~])', r'\\\1', path)
    return result


def prepare_operations(plan: dict, state: dict, bindings: dict, kind: str | None = None, limit: int = 20) -> dict:
    """No remote writes. A durable inflight create always requires reconciliation."""
    operations, blocked, unchanged = [], [], 0
    for record in plan.get("records", []):
        if kind and record["kind"] != kind:
            continue
        key = record["source_key"]
        fingerprint = record_fingerprint(record)
        previous = state.get("records", {}).get(key)
        if key in state.get("inflight", {}):
            blocked.append({"source_key":key,"reason":"inflight_requires_reconciliation","operation":state["inflight"][key]})
            continue
        if previous and previous.get("fingerprint") == fingerprint:
            unchanged += 1
            continue
        metadata_only = record.get("content_mode") == "remote_append"
        if metadata_only and not previous:
            blocked.append({"source_key": key, "reason": "use_notes_next", "kind": "notes"})
            continue
        binding = bindings.get(record["kind"])
        if not binding or not binding.get("data_source_id"):
            blocked.append({"source_key":key,"reason":"missing_database_binding","kind":record["kind"]})
            continue
        props, unresolved = _properties(record, state, plan.get('databases', {}).get(record['kind']))
        content, content_unresolved = ("", []) if metadata_only else linked_content(record,state)
        unresolved.extend(content_unresolved)
        if unresolved:
            blocked.append({"source_key":key,"reason":"unresolved_relations","keys":unresolved})
            continue
        action = "update" if previous else "create"
        op = {"operation_id":digest([key,fingerprint]), "source_key":key,"kind":record["kind"],"action":action,
              "fingerprint":fingerprint,"properties":props,"generated_content":content,
              "data_source_id":binding["data_source_id"]}
        if previous:
            op["page_id"] = previous["page_id"]
            if metadata_only:
                op.update(action="update_properties", content_mode="remote_append")
            else:
                op["baseline_region"] = previous.get("remote_region")
            op["properties"] = clear_removed_source_properties(props, previous.get("properties", {}))
        else:
            op["content"] = managed_page(op["generated_content"])
        escaped = tool_properties(op["properties"])
        if escaped != op["properties"]:
            op["source_properties"] = op["properties"]
            op["properties"] = escaped
        operations.append(op)
        if len(operations) >= limit:
            break
    return {"operations":operations,"blocked":blocked,"unchanged":unchanged}


def begin_operations(state: dict, operations: list[dict]) -> dict:
    for op in operations:
        existing = state["inflight"].get(op["source_key"])
        if existing and existing["operation_id"] != op["operation_id"]:
            raise ValueError("A prior operation has not been reconciled.")
        state["inflight"][op["source_key"]] = op
    return state


def commit_receipts(state: dict, receipts: list[dict]) -> dict:
    """Record only explicit succeeded results. Ambiguous creates stay inflight."""
    for receipt in receipts:
        key = receipt["source_key"]
        op = state["inflight"].get(key)
        if not op or receipt.get("operation_id") != op["operation_id"]:
            raise ValueError("Receipt does not match an inflight operation.")
        if op.get("notes_stage"):
            raise ValueError("Use notes-receipts for notebook operations and attachment readback validation.")
        if receipt.get("status") == "definitely_not_applied":
            state["inflight"].pop(key)
            continue
        if receipt.get("status") != "succeeded" or not receipt.get("page_id"):
            continue
        entry = {"page_id":receipt["page_id"],"fingerprint":op["fingerprint"],"kind":op["kind"],
                 "generated_content":op["generated_content"],"properties":op.get("source_properties",op["properties"]),
                 "verified_at":datetime.now(timezone.utc).isoformat()}
        if op.get("content_mode"):
            entry["content_mode"] = op["content_mode"]
        if "remote_region" in receipt:
            entry["remote_region"] = receipt["remote_region"]
        state["records"][key] = entry
        state["inflight"].pop(key)
        state["history"].append({"operation_id":op["operation_id"],"source_key":key,"action":op["action"],"page_id":receipt["page_id"]})
    return state

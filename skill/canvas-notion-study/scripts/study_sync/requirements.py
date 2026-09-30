"""Source-to-requirement reconciliation without inferring academic obligations."""
from __future__ import annotations

from copy import deepcopy


def _list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _refs(value):
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    return [str(v.get("source_key") or v.get("source_url") or v.get("id") or "") if isinstance(v, dict) else str(v) for v in _list(value)]


def _items(value):
    return _list(value.get("requirements", [])) if isinstance(value, dict) else _list(value)


def build_requirements(canvas_plan, gmail_supplement=None, reviews=None):
    """Combine explicit requirements and existing tasks, never infer from titles.

    A projected-task baseline cannot establish coverage of unreviewed sources.
    Supply explicit source-review requirements for full reconciliation.
    """
    entries = deepcopy(canvas_plan.get("requirements", []))
    basis = "explicit_requirements" if entries else "projected_tasks_only"
    if not entries:
        for record in canvas_plan.get("records", []):
            if record.get("kind") != "tasks":
                continue
            key, properties = record["source_key"], record.get("properties", {})
            if properties.get("Planning Origin") == "Assistant suggestion" and "|preparation=rule:" in key:
                continue
            refs = _refs(record.get("source_refs") or properties.get("Source"))
            refs = list(dict.fromkeys(refs + [key] + _refs(properties.get("Source URL"))))
            excluded = properties.get("Scope") == "Historical"
            optional = properties.get("Scope") == "Optional" or properties.get("Planning Origin") == "Teacher recommendation"
            entries.append({"id": key, "source_refs": refs, "task_keys": [] if excluded else [key],
                            "status": "non_actionable" if excluded else "optional" if optional else "required",
                            "reason": "Previously published historical record excluded from active planning." if excluded else "Existing projected task; source-wide review is separate."})
    entries.extend(deepcopy(_items(gmail_supplement)))
    entries.extend(deepcopy(_items(reviews)))
    combined, warnings = {}, []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("id"):
            warnings.append({"code": "requirement_id_missing"})
            continue
        key = str(entry["id"])
        current = combined.get(key)
        if current:
            if current.get("status") != entry.get("status"):
                warnings.append({"id": key, "code": "requirement_status_conflict"})
            current["source_refs"] = list(dict.fromkeys(_refs(current.get("source_refs")) + _refs(entry.get("source_refs"))))
            current["task_keys"] = list(dict.fromkeys(_refs(current.get("task_keys")) + _refs(entry.get("task_keys"))))
            continue
        combined[key] = {"id": key, "source_refs": _refs(entry.get("source_refs")), "task_keys": _refs(entry.get("task_keys")),
                         "status": entry.get("status", "required"), "reason": entry.get("reason", "")}
        if combined[key]["status"] not in {"required", "optional", "non_actionable"}:
            warnings.append({"id": key, "code": "invalid_requirement_status"})
        if not combined[key]["source_refs"]:
            warnings.append({"id": key, "code": "requirement_source_missing"})
        if combined[key]["status"] != "required" and not combined[key]["reason"]:
            warnings.append({"id": key, "code": "exclusion_reason_missing"})
    return {"schema_version": 1, "requirements": list(combined.values()), "canvas_coverage_basis": basis, "warnings": warnings}


def reconcile_sources(sources, requirements):
    """Account for every supplied source, including explicitly unreviewed ones."""
    if isinstance(sources, dict):
        sources = sources.get("sources", sources.get("messages", sources.get("records", [])))
    rows, issues = [], []
    requirements = _items(requirements)
    by_ref = {}
    for requirement in requirements:
        for ref in _refs(requirement.get("source_refs")):
            by_ref.setdefault(ref, []).append(requirement)
    for source in sources or []:
        if isinstance(source, str):
            key, aliases = source, [source]
        else:
            key = str(source.get("source_key") or source.get("source_url") or source.get("id") or "")
            aliases = list(dict.fromkeys([key] + _refs(source.get("source_aliases")) + _refs(source.get("source_url"))))
        matched = {entry["id"]: entry for alias in aliases for entry in by_ref.get(alias, [])}
        if not matched:
            issues.append({"source_key": key, "code": "source_review_missing"})
        rows.append({"source_key": key, "reviewed": bool(matched), "requirement_ids": list(matched),
                     "task_keys": list(dict.fromkeys(task for entry in matched.values() for task in _refs(entry.get("task_keys")))),
                     "statuses": list(dict.fromkeys(entry.get("status", "required") for entry in matched.values()))})
    return {"schema_version": 1, "sources": rows, "source_count": len(rows), "reviewed_source_count": sum(row["reviewed"] for row in rows), "issues": issues, "ok": not issues}

"""Evidence coverage, Notion reconciliation, and local archive verification."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
from pathlib import Path
from typing import Any, Mapping

from .learner import notion_value, page_identity, personal_rows, source_identity, unpack_payload
from .state import digest


def _items(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [dict(item, id=key) if isinstance(item, Mapping) and "id" not in item else item for key, item in value.items()]
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _refs(value: Any) -> list[str]:
    if isinstance(value, Mapping) and any(k in value for k in ("source_key", "url", "id")):
        value = [value]
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    return [str(v.get("source_key", v.get("url", v.get("id", "")))) if isinstance(v, Mapping) else str(v) for v in _items(value)]


def _requirements(requirements: Any) -> list:
    if isinstance(requirements, Mapping) and "requirements" in requirements:
        requirements = requirements["requirements"]
    return _items(requirements)


def audit_plan(plan: Mapping, requirements: Any = None, notion_rows: Any = None,
               notion_state: Mapping | None = None) -> dict:
    records = plan.get("records", [])
    counts = Counter(r.get("source_key") for r in records)
    tasks = {r["source_key"]: r for r in records if r.get("kind") == "tasks"}
    all_keys = set(counts)
    duplicates = [{"source_key": key, "count": count} for key, count in sorted(counts.items(), key=lambda p: str(p[0])) if count > 1]
    issues, coverage = [], []
    task_requirements = defaultdict(set)
    for key, record in tasks.items():
        generated = record.get("generated_content", {})
        generated = generated if isinstance(generated, Mapping) else {}
        for req in _items(record.get("requirement_ids", generated.get("requirement_ids", []))):
            task_requirements[str(req)].add(key)
        sources = _refs(record.get("source_refs", record.get("properties", {}).get("Source")))
        url = record.get("properties", {}).get("Source URL")
        if not sources and not url:
            issues.append({"code": "task_source_missing", "task_key": key})
    reqs = _requirements(requirements if requirements is not None else plan.get("requirements", []))
    req_counts = Counter(str(req.get("id", req.get("requirement_id", ""))) for req in reqs if isinstance(req, Mapping))
    for req_id, count in req_counts.items():
        if count > 1 or not req_id:
            issues.append({"code": "duplicate_requirement_id" if req_id else "requirement_id_missing", "id": req_id, "count": count})
    for req in reqs:
        if not isinstance(req, Mapping):
            issues.append({"code": "invalid_requirement"})
            continue
        req_id = str(req.get("id", req.get("requirement_id", "")))
        refs = _refs(req.get("source_refs", req.get("sources", req.get("source_key", req.get("source_url")))))
        linked = set(_refs(req.get("task_keys", req.get("task_ids", req.get("task_key"))))) | task_requirements.get(req_id, set())
        # Canonical source requirements can identify the projected task itself.
        if req_id in tasks:
            linked.add(req_id)
        missing = sorted(linked - tasks.keys())
        status = str(req.get("status", "required"))
        excluded = status in ("optional", "non_actionable", "superseded", "informational")
        covered = bool(linked & tasks.keys())
        coverage.append({"id": req_id, "source_refs": refs, "task_keys": sorted(linked & tasks.keys()),
                         "missing_task_keys": missing, "status": status, "covered": covered,
                         "exclusion_reason": req.get("reason") if excluded else None})
        if not refs:
            issues.append({"code": "requirement_source_missing", "id": req_id})
        if not covered and not excluded:
            issues.append({"code": "requirement_task_missing", "id": req_id})
        if excluded and not req.get("reason"):
            issues.append({"code": "requirement_exclusion_reason_missing", "id": req_id})
        if missing:
            issues.append({"code": "requirement_task_reference_missing", "id": req_id, "task_keys": missing})
    if requirements is None and not reqs:
        issues.append({"code": "requirements_not_supplied", "message": "Coverage cannot establish that all source requirements were reviewed."})
    reconciliation = {"status": "not_run"}
    if notion_rows is not None:
        payload = unpack_payload(notion_rows)
        rows = personal_rows(payload)
        complete = isinstance(payload, Mapping) and payload.get("has_more") is False and not payload.get("truncated")
        remote = defaultdict(list)
        unidentified = []
        for index, row in enumerate(rows):
            props = row.get("properties", row)
            key = source_identity(row.get("source_key") or props.get("Source Key"))
            if not key:
                unidentified.append(index)
                continue
            page = row.get("page_id", row.get("id", row.get("url")))
            remote[key].append(page)
        duplicate_remote = [{"source_key": key, "page_ids": values} for key, values in sorted(remote.items()) if len(values) > 1]
        mismatches = []
        for key, record in (notion_state or {}).get("records", {}).items():
            if key in remote and record.get("page_id") and page_identity(record["page_id"]) not in {page_identity(v) for v in remote[key]}:
                mismatches.append({"source_key": key, "expected_page_id": record["page_id"], "actual_page_ids": remote[key]})
        reconciliation = {"status": "complete" if complete else "partial", "row_count": len(rows),
                          "duplicate_source_keys": duplicate_remote, "unidentified_rows": unidentified,
                          "page_id_mismatches": mismatches, "missing_in_supplied_rows": sorted(all_keys - remote.keys()),
                          "unexpected_in_supplied_rows": sorted(remote.keys() - all_keys)}
        if duplicate_remote or mismatches or unidentified:
            issues.append({"code": "notion_reconciliation_conflict"})
        if complete and reconciliation["missing_in_supplied_rows"]:
            issues.append({"code": "notion_records_missing"})
    return {"schema_version": 2, "record_count": len(records), "task_count": len(tasks), "duplicate_source_keys": duplicates,
            "requirements": coverage, "requirement_count": len(reqs),
            "covered_required_count": sum(r["covered"] for r in coverage if r["status"] == "required"),
            "issues": issues, "notion": reconciliation, "ok": not duplicates and not issues}


def _file_records(snapshot: Mapping) -> list:
    result = []
    if "records" in snapshot:
        for record in snapshot["records"]:
            if record.get("kind") != "resources":
                continue
            props = record.get("properties", {})
            if props.get("Local Path") or props.get("SHA256") or props.get("Type") == "file":
                result.append({"source_key": record["source_key"], "local_path": props.get("Local Path"),
                               "sha256": props.get("SHA256"), "download_status": props.get("Download Status")})
    else:
        # Canvas collection has already reconciled referenced attachments into
        # each course.files list. Recursing source_raw/metadata duplicates the
        # same file and mistakes non-file download_status fields for files.
        unique = {}
        archive_fields = ("local_path", "sha256", "size", "download_status", "extraction_status")
        for course in snapshot.get("courses", []):
            if not isinstance(course, Mapping):
                continue
            course_id = str(course.get("id", course.get("course_id", "unknown")))
            for index, file in enumerate(course.get("files", [])):
                if not isinstance(file, Mapping):
                    continue
                merged = {}
                for name in ("archive", "archive_metadata"):
                    archive = file.get(name)
                    if isinstance(archive, Mapping):
                        merged.update({field: archive[field] for field in archive_fields if field in archive})
                merged.update({key: value for key, value in file.items() if key not in archive_fields})
                merged.update({field: file[field] for field in archive_fields if file.get(field) not in (None, "")})
                file_id = str(file.get("id", file.get("source_id", file.get("source_key", file.get("url", "unidentified:"+str(index))))))
                identity = (course_id, file_id)
                merged["source_key"] = str(file.get("source_key") or course_id + ":" + file_id)
                previous = unique.get(identity)
                if previous:
                    # Prefer the archive-bearing representation over a bare
                    # duplicate metadata record, without counting either twice.
                    combined = dict(previous)
                    combined.update({key: value for key, value in merged.items() if value not in (None, "")})
                    if previous.get("local_path") and not merged.get("local_path"):
                        combined.update({field: previous[field] for field in archive_fields if field in previous})
                    merged = combined
                unique[identity] = merged
        result.extend(unique.values())
    return result


def audit_files(snapshot: Mapping, baseline: Any = None) -> dict:
    files = []
    for record in _file_records(snapshot):
        path, expected = record.get("local_path"), record.get("sha256")
        entry = {"source_key": record["source_key"], "path": path, "expected_sha256": expected,
                 "download_status": record.get("download_status"), "actual_sha256": None}
        if not path:
            entry["status"] = "not_downloaded"
        elif not Path(path).is_file():
            entry["status"] = "file_missing"
        else:
            hasher = hashlib.sha256()
            try:
                with Path(path).open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                entry["actual_sha256"] = hasher.hexdigest()
                entry["size"] = Path(path).stat().st_size
                entry["status"] = "unverified_no_expected_hash" if not expected else "verified" if expected.lower() == entry["actual_sha256"] else "hash_mismatch"
            except OSError as error:
                entry.update({"status": "read_failed", "error": str(error)})
        files.append(entry)
    counts = dict(Counter(file["status"] for file in files))
    report = {"schema_version": 2, "files": files, "counts": counts, "file_count": len(files),
              "ok": bool(files) and all(file["status"] == "verified" for file in files)}
    if baseline is not None:
        report["baseline_diff"] = compare_baseline(report, baseline)
    return report


def _baseline_entries(value: Any) -> dict:
    if isinstance(value, Mapping):
        for field in ("files", "records", "items"):
            if field in value:
                value = value[field]
                break
        else:
            return {str(key): item for key, item in value.items()}
    result = {}
    for index, item in enumerate(value or []):
        if isinstance(item, Mapping):
            key = item.get("source_key", item.get("id", item.get("path", index)))
            result[str(key)] = item
        else:
            result[str(item)] = item
    return result


def compare_baseline(current: Any, baseline: Any) -> dict:
    current_items, old_items = _baseline_entries(current), _baseline_entries(baseline)
    common = current_items.keys() & old_items.keys()
    changed = []
    for key in sorted(common):
        if digest(current_items[key]) != digest(old_items[key]):
            changed.append({"source_key": key, "before": old_items[key], "after": current_items[key]})
    return {"added": sorted(current_items.keys() - old_items.keys()), "missing": sorted(old_items.keys() - current_items.keys()),
            "changed": changed, "unchanged": len(common) - len(changed)}

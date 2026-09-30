"""Evidence packets and source-bound enrichment validation.

The review step is intentionally separate from projection.  ``evidence_packet``
only exposes source material and deterministic hashes; it does not summarize
or interpret Canvas text.  ``validate_enrichments`` is the CLI boundary that
prevents a reviewed summary from silently surviving a source change.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1

# These fields are source version markers, rather than run metadata.  Canvas
# exports vary by endpoint, so the first available value is retained in the
# evidence identity.  ``generated_at``, download state and local archive paths
# are deliberately absent from this list.
VERSION_FIELDS = (
    "version",
    "file_version",
    "revision",
    "lock_version",
    "updated_at",
    "modified_at",
    "etag",
    "content_version",
)

ANNOUNCEMENT_TEXT_FIELDS = ("message", "body", "description", "text", "content", "html")
PAGE_TEXT_FIELDS = ("body", "text", "description", "content", "html")
FILE_TEXT_FIELDS = ("extracted_text", "text", "body", "description", "content", "html")
CALENDAR_TEXT_FIELDS = ("description", "location", "start_at", "end_at", "start", "end")
TITLE_FIELDS = ("display_name", "filename", "name", "title", "label")
ID_FIELDS = ("id", "source_id", "file_id", "page_id", "resource_id", "url")

# A review packet should contain the documents that can carry syllabus facts
# without dumping every lecture slide into the model context.  Pages/files are
# selected by their source label, while course.syllabus_body is always kept.
REVIEW_LABEL_RE = re.compile(
    r"(?:syllab(?:us)?|outline|basic[ _-]*information|course[ _-]*(?:information|info|overview|details)|"
    r"office(?:[ _-]*hours?)?|teaching[ _-]*(?:team|staff)|instructor|professor|faculty|"
    r"(?:teaching[ _-]*)?assistants?|\bta\b|contact|requirement|quiz(?:zes)?|exam(?:s)?|"
    r"midterm|final(?:s)?|grading|gradebook|grade|policy|policies|textbook|welcome|practical|schedule)",
    re.IGNORECASE,
)

_COURSE_GENERATED_KEYS = {
    "summary",
    "course_summary",
    "syllabus_summary",
    "important_info",
    "important_information",
    "key_info",
    "events",
    "action_items",
    "actions",
}
_ANNOUNCEMENT_GENERATED_KEYS = {
    "summary",
    "announcement_summary",
    "action_items",
    "actions",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def _id(value: Any, *keys: str) -> str | None:
    if isinstance(value, Mapping):
        if not keys:
            keys = ID_FIELDS
        for key in keys:
            raw = value.get(key)
            if raw not in (None, "") and not isinstance(raw, (Mapping, list, tuple, set)):
                return str(raw)
    elif value not in (None, ""):
        return str(value)
    return None


def _source_version(source: Mapping[str, Any]) -> Any:
    """Return source version metadata without including run-only fields."""

    values: dict[str, Any] = {}
    for key in VERSION_FIELDS:
        value = source.get(key)
        if value not in (None, ""):
            values[key] = value
    return values


def _raw_value(value: Any) -> Any:
    """Keep source text losslessly where possible, canonicalizing structures."""

    if isinstance(value, str):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _content_fields(source: Mapping[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in fields:
        if key not in source or source.get(key) is None:
            continue
        value = _raw_value(source.get(key))
        if value not in (None, ""):
            result[key] = value
    return result


def _primary_text(content: Mapping[str, Any]) -> str | None:
    for key in content:
        value = content[key]
        if isinstance(value, str) and value != "":
            return value
    return None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_source(kind: str, source_id: str | None, version: Mapping[str, Any], content: Mapping[str, Any]) -> str:
    """Hash only source version and original content.

    Caller-facing metadata such as title, URL, ``download_status``,
    ``local_path`` and snapshot ``generated_at`` is intentionally omitted.
    ``kind`` and ``source_id`` remain arguments for call-site clarity but are
    deliberately excluded so a metadata-only rename cannot invalidate review.
    """

    payload = {
        "version": dict(version),
        "content": dict(content),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _source_url(source: Mapping[str, Any]) -> str | None:
    for key in ("source_url", "html_url", "url", "web_url", "href"):
        value = _text(source.get(key))
        if value:
            return value
    return None


def _title(source: Mapping[str, Any], fallback: str = "") -> str:
    for key in TITLE_FIELDS:
        value = _text(source.get(key))
        if value:
            return value
    return fallback


def _relevant_label(source: Mapping[str, Any], *, include_text: bool = False) -> bool:
    # Archived Canvas files may lose their original display name and retain a
    # generic filename (for example ``file-900.pdf``).  Source text still
    # carries stable labels such as "syllabus" or "office hours"; using it as
    # a fallback keeps a metadata-only archive rename from dropping evidence.
    label_fields = TITLE_FIELDS + ("url", "page_url", "slug")
    if include_text:
        label_fields += ("extracted_text", "text", "body", "description")
    label = " ".join(value for value in (_text(source.get(key)) for key in label_fields) if value)
    return bool(label and REVIEW_LABEL_RE.search(label))


def _evidence_item(
    source: Mapping[str, Any],
    *,
    kind: str,
    text_fields: Iterable[str],
    fallback_id: str | None = None,
    hash_title: bool = False,
) -> dict[str, Any]:
    source_id = _id(source) or fallback_id
    content = _content_fields(source, text_fields)
    version = _source_version(source)
    hash_content = dict(content)
    if hash_title:
        # Announcement/page titles can carry deadline or scope changes even
        # when their body remains byte-for-byte identical. File display names
        # intentionally stay outside the hash because archive renames are not
        # source revisions.
        hash_content = {"title": _title(source, source_id or kind), **hash_content}
    item: dict[str, Any] = {
        "id": source_id,
        "title": _title(source, source_id or kind),
        "name": _title(source, source_id or kind),
        "source_url": _source_url(source),
        "version": version,
        "text": _primary_text(content),
        "full_text": _primary_text(content),
        "content": content,
        "source_hash": _hash_source(kind, source_id, version, hash_content),
    }
    if "body" in content:
        item["body"] = content["body"]
    if "message" in content:
        item["message"] = content["message"]
    # Preserve useful source status for the reviewer, without feeding it into
    # the hash.  These fields describe availability, not source content.
    for key in ("download_status", "extraction_status", "status"):
        value = _text(source.get(key))
        if value:
            item[key] = value
    return item


def _iter_records(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [item for item in value.values() if isinstance(item, Mapping)]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _iter_records_with_keys(value: Any) -> Iterable[tuple[str | None, Mapping[str, Any]]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, Mapping):
                yield str(key), item
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, Mapping):
                yield None, item


def _course_sources(course: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    documents: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    for raw in _iter_records(course.get("files")):
        if _relevant_label(raw, include_text=True):
            documents.append(_evidence_item(raw, kind="file", text_fields=FILE_TEXT_FIELDS))
    for raw in _iter_records(course.get("pages")):
        if raw.get("front_page") or _relevant_label(raw):
            pages.append(_evidence_item(raw, kind="page", text_fields=PAGE_TEXT_FIELDS, hash_title=True))
    documents.sort(key=lambda item: (str(item.get("id") or ""), str(item.get("title") or "")))
    pages.sort(key=lambda item: (str(item.get("id") or ""), str(item.get("title") or "")))
    return documents, pages


def _calendar_events(course: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose the calendar subset that can change course guidance."""

    events: list[dict[str, Any]] = []
    for raw in _iter_records(course.get("calendar_events")):
        item = _evidence_item(
            raw,
            kind="calendar_event",
            text_fields=CALENDAR_TEXT_FIELDS,
            hash_title=True,
        )
        for key in ("start_at", "start", "end_at", "end", "location", "description", "updated_at"):
            if key in raw and raw.get(key) is not None:
                item[key] = _raw_value(raw.get(key))
        events.append(item)
    events.sort(key=lambda item: (str(item.get("id") or ""), str(item.get("title") or "")))
    return events


def _syllabus_evidence(course: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = course.get("syllabus_body")
    if raw in (None, ""):
        return None
    source = {
        "id": course.get("id"),
        "source_url": course.get("html_url") or course.get("source_url"),
        "syllabus_body": raw,
    }
    # A syllabus can carry a Canvas revision/version on the course object.
    for key in VERSION_FIELDS:
        if key in course:
            source[key] = course[key]
    result = _evidence_item(source, kind="syllabus", text_fields=("syllabus_body",), fallback_id=_id(course))
    result["title"] = f"{_title(course, 'Course')} syllabus"
    return result


def _course_hash(
    course: Mapping[str, Any],
    syllabus: Mapping[str, Any] | None,
    documents: list[Mapping[str, Any]],
    pages: list[Mapping[str, Any]],
    calendar_events: list[Mapping[str, Any]] = (),
    announcements: list[Mapping[str, Any]] = (),
) -> str:
    sources: list[dict[str, Any]] = []
    for kind, values in (("syllabus", [syllabus] if syllabus else []), ("file", documents), ("page", pages)):
        for item in values:
            if not isinstance(item, Mapping):
                continue
            sources.append(
                {
                    "version": item.get("version", {}),
                    "content": (
                        {"title": item.get("title"), **item.get("content", {})}
                        if kind == "page"
                        else item.get("content", {})
                    ),
                }
            )
    for event in calendar_events:
        if not isinstance(event, Mapping):
            continue
        event_content = event.get("content", {})
        if not isinstance(event_content, Mapping):
            event_content = {}
        sources.append(
            {
                "kind": "calendar_event",
                "version": event.get("version", {}),
                "content": {"title": event.get("title"), **event_content},
            }
        )
    for announcement in announcements:
        if not isinstance(announcement, Mapping):
            continue
        source_hash = _text(announcement.get("source_hash"))
        if source_hash:
            # The announcement hash already covers title, full source text and
            # source version; keeping only the digest makes the course hash
            # compact while conservatively revoking course review on changes.
            sources.append({"kind": "announcement", "source_hash": source_hash})
    sources.sort(key=_canonical)
    return _hash_source(
        "course",
        _id(course),
        _source_version(course),
        {"sources": sources},
    )


def _reply_evidence(reply: Mapping[str, Any]) -> dict[str, Any]:
    item = _evidence_item(reply, kind="announcement_reply", text_fields=ANNOUNCEMENT_TEXT_FIELDS)
    for key in ("user_name", "created_at", "updated_at", "coverage_status", "retrieved_at"):
        if reply.get(key) is not None:
            item[key] = deepcopy(reply[key])
    nested = reply.get("replies") if "replies" in reply else reply.get("recent_replies")
    item["replies"] = [_reply_evidence(child) for child in _iter_records(nested)]
    item["replies"].sort(key=lambda child: str(child.get("id") or ""))
    return item


def _reply_hash_content(reply: Mapping[str, Any]) -> dict[str, Any]:
    # Store only source identity/content/version, never collection times or
    # pagination status. A changed or removed readable reply revokes review.
    return {"id": reply.get("id"), "version": reply.get("version", {}),
            "content": reply.get("content", {}),
            "replies": [_reply_hash_content(child) for child in reply.get("replies", [])]}


def _announcement_evidence(announcement: Mapping[str, Any], course_id: str | None = None,
                           coverage_entries=()) -> dict[str, Any]:
    item = _evidence_item(announcement, kind="announcement", text_fields=ANNOUNCEMENT_TEXT_FIELDS, hash_title=True)
    replies = [_reply_evidence(reply) for reply in _iter_records(announcement.get("replies"))]
    replies.sort(key=lambda reply: str(reply.get("id") or ""))
    item["replies"] = replies
    item["replies_status"] = announcement.get("replies_status", "not_collected")
    entry_path = f"/discussion_topics/{item['id']}/entries"
    item["reply_coverage"] = {"status": item["replies_status"], "entries": [deepcopy(entry)
        for entry in coverage_entries if entry_path in str(entry.get("endpoint", ""))]}
    if replies:
        # Empty new collection fields remain compatible with earlier reviewed
        # announcements. Actual reply text becomes part of their identity.
        hash_content = {"title": item["title"], **item["content"],
                        "replies": [_reply_hash_content(reply) for reply in replies]}
        item["source_hash"] = _hash_source("announcement", item["id"], item["version"], hash_content)
    for key in ("posted_at", "published_at", "created_at"):
        value = _text(announcement.get(key))
        if value:
            item["posted_at"] = value
            break
    if course_id is not None:
        item["course_id"] = course_id
    return item


def evidence_packet(snapshot: Mapping[str, Any], course_id: Any = None) -> dict[str, Any]:
    """Build a deterministic source packet for the CLI review step.

    ``courses`` and ``announcements`` are canonical dictionaries keyed by
    Canvas IDs.  Every included source retains its complete available text;
    no summary is inferred here.  A course hash covers its syllabus,
    review-relevant files/pages, calendar evidence and related announcement
    hashes, while each announcement gets an independent content/version hash.
    """

    snapshot = _mapping(snapshot)
    wanted = str(course_id) if course_id is not None else None
    courses_out: dict[str, Any] = {}
    announcements_out: dict[str, Any] = {}
    course_records: dict[str, Mapping[str, Any]] = {}
    courses = _iter_records_with_keys(snapshot.get("courses"))

    for mapping_key, raw_course in courses:
        course_key = _id(raw_course) or mapping_key
        if course_key is None or (wanted is not None and course_key != wanted):
            continue
        if str(raw_course.get("mode", "course")).lower() == "ignore":
            continue
        syllabus = _syllabus_evidence(raw_course)
        documents, pages = _course_sources(raw_course)
        calendar_events = _calendar_events(raw_course)
        course_entry: dict[str, Any] = {
            "id": course_key,
            "name": _title(raw_course, course_key),
            "mode": _text(raw_course.get("mode")) or "course",
            "source_url": _source_url(raw_course),
            "source_hash": "",
            "syllabus": syllabus,
            "syllabus_text": syllabus.get("text") if syllabus else None,
            "documents": documents,
            "pages": pages,
            "calendar_events": calendar_events,
            "announcement_ids": [],
        }
        courses_out[course_key] = course_entry
        course_records[course_key] = raw_course

        for mapping_key, raw_announcement in _iter_records_with_keys(raw_course.get("announcements")):
            announcement_key = _id(raw_announcement) or mapping_key
            if announcement_key is None:
                continue
            announcement = _announcement_evidence(raw_announcement, course_key,
                _mapping(raw_course.get("coverage")).get("entries", []))
            announcements_out[announcement_key] = announcement
            course_entry["announcement_ids"].append(announcement_key)
        course_entry["announcement_ids"].sort()

    # A few snapshots keep announcements in a top-level collection.  Include
    # those when their course can be resolved, while retaining all course-local
    # records above.  Duplicate IDs are merged in favor of the course-local
    # record because it carries the canonical relation.
    for mapping_key, raw_announcement in _iter_records_with_keys(snapshot.get("announcements")):
        announcement_key = _id(raw_announcement) or mapping_key
        if announcement_key is None or announcement_key in announcements_out:
            continue
        raw_course_id = _id(raw_announcement, "course_id", "context_id")
        if wanted is not None and raw_course_id not in (None, wanted):
            continue
        if raw_course_id is not None and raw_course_id not in courses_out:
            continue
        announcements_out[announcement_key] = _announcement_evidence(raw_announcement, raw_course_id)
        if raw_course_id in courses_out:
            courses_out[raw_course_id]["announcement_ids"].append(announcement_key)
            courses_out[raw_course_id]["announcement_ids"].sort()

    # Compute course hashes after both course-local and top-level announcements
    # have been associated with their course.
    for course_key, course_entry in courses_out.items():
        related_announcements = [
            announcements_out[announcement_id]
            for announcement_id in course_entry.get("announcement_ids", [])
            if announcement_id in announcements_out
        ]
        course_entry["source_hash"] = _course_hash(
            course_records[course_key],
            course_entry.get("syllabus"),
            course_entry.get("documents", []),
            course_entry.get("pages", []),
            course_entry.get("calendar_events", []),
            related_announcements,
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "term": deepcopy(snapshot.get("term", {})),
        "courses": dict(sorted(courses_out.items())),
        "announcements": dict(sorted(announcements_out.items())),
        "warnings": [],
    }


def _is_reviewed(item: Mapping[str, Any], *, global_reviewed: bool = False) -> bool:
    value = item.get("reviewed")
    if isinstance(value, str):
        value = value.strip().lower() in {"true", "yes", "reviewed", "approved"}
    if value is True:
        return True
    status = _text(item.get("status"))
    return global_reviewed or bool(status and status.lower() in {"reviewed", "approved"})


def _pending_item(item: Mapping[str, Any], *, expected_hash: str | None, course: bool) -> dict[str, Any]:
    """Remove generated review prose so stale content cannot be projected."""

    result = dict(item)
    generated_keys = _COURSE_GENERATED_KEYS if course else _ANNOUNCEMENT_GENERATED_KEYS
    for key in generated_keys:
        result.pop(key, None)
    result["reviewed"] = False
    result["status"] = "pending_review"
    result["source_hash"] = expected_hash
    if course:
        result["summary_status"] = "pending_review"
        result["important_info_status"] = "pending_review"
    else:
        result["summary_status"] = "pending_review"
        result["action_items_status"] = "pending_review"
    return result


def _warning(scope: str, code: str, message: str, object_id: str) -> dict[str, str]:
    return {"scope": scope, "code": code, "message": message, "object_id": object_id}


def validate_enrichments(
    snapshot: Mapping[str, Any],
    enrichments: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Validate reviewed enrichment against the current source hashes.

    Only canonical ``courses`` and ``announcements`` dictionaries are
    validated.  Other projection-compatible keys are preserved so callers can
    continue using the trusted projection API unchanged.
    """

    result: dict[str, Any] = deepcopy(dict(enrichments or {}))
    packet = evidence_packet(snapshot)
    warnings: list[dict[str, str]] = []
    global_reviewed = bool(result.get("reviewed") is True or result.get("status") in {"reviewed", "approved"})

    for section, evidence_key, is_course in (("courses", "courses", True), ("announcements", "announcements", False)):
        raw_items = result.get(section)
        if not isinstance(raw_items, Mapping):
            continue
        filtered: dict[str, Any] = {}
        for raw_key in sorted(raw_items, key=str):
            raw_item = raw_items[raw_key]
            key = str(raw_key)
            item = dict(raw_item) if isinstance(raw_item, Mapping) else {}
            reviewed = _is_reviewed(item, global_reviewed=global_reviewed)
            if not reviewed:
                filtered[key] = item
                continue
            evidence = _mapping(packet.get(evidence_key, {})).get(key)
            expected_hash = _text(_mapping(evidence).get("source_hash")) if evidence else None
            provided_hash = _text(item.get("source_hash"))
            if expected_hash is None:
                warnings.append(
                    _warning(
                        "course" if is_course else "announcement",
                        "enrichment_source_missing",
                        "Reviewed enrichment has no matching source in the current snapshot; marked pending_review.",
                        key,
                    )
                )
                filtered[key] = _pending_item(item, expected_hash=None, course=is_course)
            elif not provided_hash:
                warnings.append(
                    _warning(
                        "course" if is_course else "announcement",
                        "enrichment_source_hash_missing",
                        "Reviewed enrichment requires source_hash; marked pending_review.",
                        key,
                    )
                )
                filtered[key] = _pending_item(item, expected_hash=expected_hash, course=is_course)
            elif provided_hash != expected_hash:
                warnings.append(
                    _warning(
                        "course" if is_course else "announcement",
                        "enrichment_source_changed",
                        "Source content or version changed since review; marked pending_review.",
                        key,
                    )
                )
                filtered[key] = _pending_item(item, expected_hash=expected_hash, course=is_course)
            else:
                filtered[key] = item
        result[section] = filtered

    # A global reviewed marker would otherwise re-authorize an invalid item in
    # projection.  Once any item is revoked, make that global marker pending
    # so every canonical item must carry its own reviewed/hash pair.
    if warnings and global_reviewed:
        result["reviewed"] = False
        result["status"] = "pending_review"
    return result, warnings


__all__ = ["SCHEMA_VERSION", "evidence_packet", "validate_enrichments"]

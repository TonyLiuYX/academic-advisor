"""Deterministic projection of a Canvas snapshot into a Notion write plan.

The collector owns Canvas I/O and the caller owns Notion I/O.  This module only
turns a canonical snapshot into JSON-serialisable records.  In particular, it
does not call a model, fetch an attachment, or decide a user's completion or
planning state.

The public surface is intentionally small:

``build_plan(snapshot, enrichments=None, bindings=None)``
    Build a deterministic plan whose relation values are symbolic ``Source
    Key`` strings until the caller binds them to Notion page IDs.

``tasks_to_ics(plan)`` / ``export_tasks_ics(plan)``
    Produce a reproducible ICS export for tasks with one unambiguous due date.

The property names in this module are the names root uses for the Notion
databases.  Notion property values are flattened: dates are strings, checkbox
values are ``__YES__``/``__NO__``, and relation values are source-key strings
or lists of source-key strings.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Mapping, MutableMapping, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

try:
    # Canvas owns the HTML parser and same-origin filtering.  Keeping the
    # helper here avoids reimplementing the file-link rules in projection.
    from .canvas import extract_html_file_refs, extract_html_text
except (ImportError, ModuleNotFoundError):  # pragma: no cover - direct-file use
    extract_html_file_refs = None
    extract_html_text = None


PROJECTION_SCHEMA_VERSION = 1
PENDING_MARKER = "[PENDING REVIEW]"
YES = "__YES__"
NO = "__NO__"
MAX_GENERATED_TEXT = 2_000
# Generated enrichment remains bounded at ``MAX_GENERATED_TEXT`` when it is
# stored in a record.  The managed page renderer can show a complete course
# review packet (for example, a reviewed syllabus with ten information items)
# without applying the task-sized bound to the whole page.
MAX_RENDERED_TEXT_BY_KIND = {
    "courses": 6_000,
    "notes": 6_000,
    "resources": 4_000,
    "tasks": 3_000,
    "announcements": 3_000,
    "timetable": 3_000,
}

DATABASE_KINDS = (
    "courses",
    "tasks",
    "resources",
    "announcements",
    "notes",
    "timetable",
)

_KIND_ORDER = {name: index for index, name in enumerate(DATABASE_KINDS)}
_PERSONAL_PROPERTY_NAMES = {
    "done",
    "completed",
    "completion",
    "planned",
    "plan",
    "priority",
    "personal notes",
    "personal note",
}


# This is deliberately plain data.  It is copied into each plan so callers can
# inspect it and generate database definitions without importing private
# helpers from this module.
DATABASE_SCHEMAS: dict[str, dict[str, Any]] = {
    "courses": {
        "name": "Courses",
        "description": "Formal Canvas courses for the selected term.",
        "properties": {
            "Name": {"type": "title", "required": True},
            "Source Key": {"type": "text", "required": True, "unique": True},
            "Canvas ID": {"type": "text", "required": True},
            "Course Code": {"type": "text"},
            "Course Mode": {"type": "select"},
            "Term": {"type": "text"},
            "Source URL": {"type": "url"},
            "Canvas Status": {"type": "select"},
            "Syllabus Available": {"type": "checkbox", "encoding": [YES, NO]},
            "Enrichment Status": {"type": "select"},
            "Announcement Count": {"type": "number"},
            "Assignment Count": {"type": "number"},
        },
    },
    "tasks": {
        "name": "Tasks",
        "description": "Canvas assignments and reviewed announcement action items.",
        "properties": {
            "Name": {"type": "title", "required": True},
            "Source Key": {"type": "text", "required": True, "unique": True},
            "Course": {"type": "relation", "symbolic": True, "many": True},
            "Term": {"type": "text"},
            "Source URL": {"type": "url"},
            "Source Space": {"type": "text"},
            "Source": {
                "type": "rich_text",
                "format": "newline-delimited symbolic source keys",
            },
            "Type": {"type": "select"},
            "Due": {
                "type": "date",
                "flattened_keys": ["date:Due:start", "date:Due:is_datetime"],
            },
            "Due Choices": {"type": "multi_select"},
            "Due Conflict": {"type": "checkbox", "encoding": [YES, NO]},
            "Has Attachments": {"type": "checkbox", "encoding": [YES, NO]},
            "Attachment Count": {"type": "number"},
            "Resources": {"type": "relation", "symbolic": True, "many": True},
            "Assignment ID": {"type": "text"},
            "Announcement ID": {"type": "text"},
            "Canvas Status": {"type": "select"},
            "Sync Status": {"type": "select"},
        },
    },
    "resources": {
        "name": "Resources",
        "description": "Canvas files, pages, and assignment attachments indexed by ID and version.",
        "properties": {
            "Name": {"type": "title", "required": True},
            "Source Key": {"type": "text", "required": True, "unique": True},
            "Course": {"type": "relation", "symbolic": True, "many": True},
            "Term": {"type": "text"},
            "Source URL": {"type": "url"},
            "Source Space": {"type": "text"},
            "Canvas ID": {"type": "text", "required": True},
            "Type": {"type": "select"},
            "Syllabus": {"type": "checkbox", "encoding": [YES, NO]},
            "Version": {"type": "text"},
            "Local Path": {"type": "text"},
            "Download Status": {"type": "select"},
            "Extraction Status": {"type": "select"},
            "SHA256": {"type": "text"},
            "Sync Status": {"type": "select"},
        },
    },
    "announcements": {
        "name": "Announcements",
        "description": "Formal-course and hub announcements with reviewed content separated from source properties.",
        "properties": {
            "Name": {"type": "title", "required": True},
            "Source Key": {"type": "text", "required": True, "unique": True},
            "Course": {"type": "relation", "symbolic": True, "many": True},
            "Term": {"type": "text"},
            "Source URL": {"type": "url"},
            "Source Space": {"type": "text"},
            "Canvas ID": {"type": "text", "required": True},
            "Posted": {
                "type": "date",
                "flattened_keys": ["date:Posted:start", "date:Posted:is_datetime"],
            },
            "Type": {"type": "select"},
            "Has Action Items": {"type": "checkbox", "encoding": [YES, NO]},
            "Sync Status": {"type": "select"},
        },
    },
    "notes": {
        "name": "Notes",
        "description": "Reviewed syllabus information and continuing classroom notebooks with original evidence.",
        "properties": {
            "Name": {"type": "title", "required": True},
            "Source Key": {"type": "text", "required": True, "unique": True},
            "Course": {"type": "relation", "symbolic": True, "many": True},
            "Term": {"type": "text"},
            "Source URL": {"type": "url"},
            "Source Space": {"type": "text"},
            "Type": {"type": "select", "options": ["syllabus", "Class notes"]},
            "Sync Status": {"type": "select"},
            "Storage": {"type": "select", "options": ["Notion", "Obsidian"]},
            "Note ID": {"type": "text"},
            "Vault Key": {"type": "text"},
            "Note Path": {"type": "text"},
            "Obsidian URI": {"type": "text"},
            "Last Indexed": {"type": "date"},
            "Link Status": {"type": "select", "options": ["pending", "ready", "missing", "ambiguous"]},
        },
    },
    "timetable": {
        "name": "Timetable",
        "description": "Canvas calendar events with source links and explicit start/end values.",
        "properties": {
            "Name": {"type": "title", "required": True},
            "Source Key": {"type": "text", "required": True, "unique": True},
            "Course": {"type": "relation", "symbolic": True, "many": True},
            "Term": {"type": "text"},
            "Source URL": {"type": "url"},
            "Source Space": {"type": "text"},
            "Start": {
                "type": "date",
                "flattened_keys": ["date:Start:start", "date:Start:is_datetime"],
            },
            "End": {
                "type": "date",
                "flattened_keys": ["date:End:start", "date:End:is_datetime"],
            },
            "All Day": {"type": "checkbox", "encoding": [YES, NO]},
            "Type": {"type": "select"},
            "Sync Status": {"type": "select"},
        },
    },
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def _scalar_id(value: Any) -> str | None:
    """Return a stable textual ID without inventing one for null values."""

    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    text_value = str(value).strip()
    return text_value or None


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _source_url(obj: Mapping[str, Any]) -> str | None:
    links = _mapping(obj.get("links"))
    return _text(
        _first(
            obj,
            "source_url",
            "html_url",
            "url",
            "web_url",
            "href",
        )
        or _first(links, "self", "url", "web_url")
    )


def _origin(snapshot: Mapping[str, Any]) -> str:
    value = _text(snapshot.get("canvas_origin"))
    return (value or "unknown-origin").rstrip("/")


def _owner_id(snapshot: Mapping[str, Any]) -> str:
    user = _mapping(snapshot.get("user"))
    return _scalar_id(_first(user, "id", "user_id")) or "unknown-user"


def source_key(
    origin: str,
    owner_id: str | int,
    source_type: str,
    object_id: str | int,
    *,
    version: str | int | None = None,
) -> str:
    """Build the canonical source key used for every relation.

    The key is intentionally human-readable while escaping delimiters in
    source values.  It contains origin, Canvas user ID, source type, and object
    ID.  Resource versions are an optional final component so a new Canvas
    file revision cannot overwrite an older indexed revision.
    """

    def escaped(value: Any) -> str:
        return quote(str(value), safe="-._~:/@")

    pieces = [
        escaped(origin or "unknown-origin"),
        f"user={escaped(owner_id or 'unknown-user')}",
        f"type={escaped(source_type)}",
        f"id={escaped(object_id)}",
    ]
    if version is not None and str(version).strip():
        pieces.append(f"version={escaped(version)}")
    return "|".join(pieces)


def _yes_no(value: Any, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"", "unknown", "null", "none"}:
            return default
        if lowered in {"1", "true", "yes", "y", "on", "published", "available"}:
            return YES
        if lowered in {"0", "false", "no", "n", "off", "unpublished", "missing"}:
            return NO
    return YES if bool(value) else NO


def _props(**values: Any) -> dict[str, Any]:
    """Drop absent values and personal fields from a flat property mapping."""

    result: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        if key.strip().lower() in _PERSONAL_PROPERTY_NAMES:
            continue
        result[key] = value
    return result


def _bounded_text(value: Any, *, limit: int = MAX_GENERATED_TEXT) -> str | None:
    """Bound reviewed text without deriving a summary from source text.

    This helper is used only for explicit reviewed enrichment.  Raw Canvas
    descriptions and syllabus HTML are never passed through it as summaries.
    """

    text_value = _text(value)
    if text_value is None:
        return None
    if len(text_value) <= limit:
        return text_value
    # Keep a complete-word boundary where possible and mark the bound.  The
    # caller also gets ``*_truncated`` metadata so the loss is explicit.
    clipped = text_value[: max(1, limit - 1)].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return f"{clipped}…"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_text(value: Any, *, length: int = 16) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:length]


def _normalise_datetime(value: Any) -> str | None:
    """Normalise common Canvas date forms without guessing missing values."""

    text_value = _text(value)
    if text_value is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text_value):
        return text_value
    candidate = text_value
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text_value)
        except (TypeError, ValueError, IndexError, OverflowError):
            # Preserve the actual source value so the caller can inspect it;
            # never manufacture a date from a title or a term boundary.
            return text_value
    return parsed.isoformat()


def _effective_date_values(obj: Mapping[str, Any], keys: Sequence[str] = ("due_at", "due_date")) -> list[str]:
    """Return effective dates supplied by Canvas or reviewed enrichment.

    Canvas ``all_dates`` often contains dates for every section, group, or
    student.  The collector's effective ``due_at`` (with overrides enabled) is
    authoritative for this student, so alternatives are intentionally ignored
    here.  A caller can still provide two explicit effective keys to make a
    conflict visible.
    """

    # Prefer the explicit effective override, then Canvas's due_at.  The
    # collector returns a scalar effective value for the current user; other
    # date-shaped fields are fallbacks, not independent alternatives.
    for key in ("effective_due_at", "due_at", "effective_due_date", "due_date", *keys):
        if key not in obj:
            continue
        normalised = _normalise_datetime(obj.get(key))
        if normalised is not None:
            return [normalised]
    return []


def _event_date(obj: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        if key in obj and obj.get(key) not in (None, ""):
            return _normalise_datetime(obj.get(key))
    return None


def _record(
    kind: str,
    key: str,
    *,
    course_key: str | None,
    properties: Mapping[str, Any],
    generated_content: Mapping[str, Any] | None = None,
    user_content: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": kind,
        "source_key": key,
        "course_key": course_key,
        "properties": dict(properties),
        "generated_content": dict(generated_content or {}),
    }
    if user_content:
        result["user_content"] = dict(user_content)
    return result


def _safe_warning(
    warning: Any,
    *,
    default_scope: str,
    default_object_id: str | None = None,
) -> dict[str, Any]:
    raw = _mapping(warning)
    result: dict[str, Any] = {
        "scope": _text(raw.get("scope")) or default_scope,
        "code": _text(raw.get("code")) or "source_warning",
        "message": _text(raw.get("message")) or "Source warning",
    }
    object_id = _scalar_id(_first(raw, "object_id", "id")) or default_object_id
    if object_id is not None:
        result["object_id"] = object_id
    return result


def _warning_key(warning: Mapping[str, Any]) -> str:
    return _canonical_json(warning)


def _iter_items(value: Any, *, id_keys: Sequence[str]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    """Iterate an enrichment section represented as a map or list."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).startswith("_"):
                continue
            if not isinstance(item, Mapping):
                continue
            item_id = None
            for id_key in id_keys:
                item_id = _scalar_id(item.get(id_key))
                if item_id is not None:
                    break
            yield (item_id or _scalar_id(key) or "", item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, Mapping):
                continue
            item_id = None
            for id_key in id_keys:
                item_id = _scalar_id(item.get(id_key))
                if item_id is not None:
                    break
            yield (item_id or "", item)


def _is_reviewed(item: Mapping[str, Any], *, global_reviewed: bool = False) -> bool:
    value = item.get("reviewed")
    if isinstance(value, str):
        value = value.strip().lower() in {"true", "yes", "reviewed", "approved"}
    if value is True:
        return True
    status = _text(item.get("status"))
    return global_reviewed or (status is not None and status.lower() in {"reviewed", "approved"})


def _index_enrichments(enrichments: Any) -> dict[str, Any]:
    """Index the documented enrichment contract while accepting list forms."""

    root = _mapping(enrichments)
    global_reviewed = bool(root.get("reviewed") is True or root.get("status") in {"reviewed", "approved"})
    courses: dict[str, Mapping[str, Any]] = {}
    announcements: dict[str, Mapping[str, Any]] = {}

    def put(target: MutableMapping[str, Mapping[str, Any]], item_id: str, item: Mapping[str, Any]) -> None:
        if item_id:
            # Preserve the first complete item when a convenience alias and a
            # canonical section both contain the same ID.
            existing = target.get(item_id)
            if existing is None:
                target[item_id] = item
            else:
                merged = dict(existing)
                merged.update(item)
                target[item_id] = merged

    for section in ("courses", "course_summaries", "syllabus", "syllabi"):
        raw = root.get(section)
        for item_id, item in _iter_items(raw, id_keys=("course_id", "id", "canvas_id")):
            put(courses, item_id, item)

    for section in ("announcements", "announcement_summaries", "notices"):
        raw = root.get(section)
        for item_id, item in _iter_items(raw, id_keys=("announcement_id", "id", "canvas_id")):
            put(announcements, item_id, item)

    # A course-local announcements list is a useful and documented shorthand.
    for course_item in list(courses.values()):
        for item_id, item in _iter_items(
            course_item.get("announcements"), id_keys=("announcement_id", "id", "canvas_id")
        ):
            put(announcements, item_id, item)

    return {
        "courses": courses,
        "announcements": announcements,
        "global_reviewed": global_reviewed,
    }


def _normalise_important_info(value: Any) -> tuple[list[Any], bool]:
    """Return structured reviewed info without flattening raw HTML."""

    def bound_nested(item: Any) -> Any:
        if isinstance(item, str):
            return _bounded_text(item)
        if isinstance(item, list):
            return [bound_nested(child) for child in item]
        if isinstance(item, tuple):
            return [bound_nested(child) for child in item]
        if isinstance(item, Mapping):
            return {str(key): bound_nested(child) for key, child in item.items()}
        return item

    if value is None:
        return [], False
    if isinstance(value, str):
        bounded = _bounded_text(value)
        return ([bounded] if bounded else []), bool(bounded and len(value.strip()) > MAX_GENERATED_TEXT)
    if isinstance(value, (list, tuple)):
        result: list[Any] = []
        truncated = False
        for item in value:
            if isinstance(item, Mapping):
                if "text" in item:
                    source_text = _text(item.get("text"))
                    bounded = _bounded_text(source_text)
                    if bounded is not None:
                        result.append(bounded)
                        truncated = truncated or (source_text is not None and len(source_text) > MAX_GENERATED_TEXT)
                else:
                    result.append(bound_nested(dict(item)))
            else:
                source_text = _text(item)
                bounded = _bounded_text(source_text)
                if bounded is not None:
                    result.append(bounded)
                    truncated = truncated or (source_text is not None and len(source_text) > MAX_GENERATED_TEXT)
        return result, truncated
    if isinstance(value, Mapping):
        result = []
        truncated = False
        for key in sorted(value, key=str):
            item = value[key]
            if isinstance(item, (str, int, float, bool)):
                source_text = f"{key}: {item}"
                bounded = _bounded_text(source_text)
                if bounded is not None:
                    result.append(bounded)
                    truncated = truncated or len(source_text) > MAX_GENERATED_TEXT
            else:
                    result.append({str(key): bound_nested(item)})
        return result, truncated
    return [], False


def _user_content(item: Mapping[str, Any]) -> dict[str, Any]:
    """Keep caller-owned notes outside generated_content."""

    result: dict[str, Any] = {}
    for key in ("user_notes", "notes", "user_content"):
        if key in item and item[key] not in (None, "", [], {}):
            result[key] = item[key]
    return result


def _title(obj: Mapping[str, Any], fallback: str) -> str:
    return _text(_first(obj, "name", "title", "subject", "display_name", "filename", "summary")) or fallback


def _course_title(course: Mapping[str, Any], fallback: str) -> str:
    """Use the human course title after Canvas's ``CODE: title`` prefix."""

    raw_name = _text(_first(course, "name", "title")) or fallback
    if ":" in raw_name:
        after_colon = raw_name.split(":", 1)[1].strip()
        if after_colon:
            return after_colon
    return raw_name


def _compact_course_code(course: Mapping[str, Any]) -> str | None:
    raw_code = _text(course.get("course_code"))
    if raw_code is None:
        return None
    # Canvas course codes often append section/meeting details.  Keep the
    # first compact academic code while preserving unusual codes verbatim when
    # no recognizable token exists.
    match = re.search(r"\b[A-Za-z]{2,}\d{2,}[A-Za-z]?\d*\b", raw_code)
    return match.group(0).upper() if match else raw_code


def _is_syllabus_resource(resource: Mapping[str, Any], resource_type: str) -> bool:
    if resource_type == "syllabus":
        return True
    label = " ".join(
        value
        for value in (
            _text(_first(resource, "name", "title", "display_name", "filename")),
            _text(resource.get("mime_class")),
        )
        if value
    ).lower()
    # Canvas/archive filenames use spaces, underscores, and hyphens
    # interchangeably (for example ``Basic_Information`` or
    # ``Course-Outline``).  Normalize separators before matching the syllabus
    # vocabulary, including the course's "Basic Information" handout.
    normalized = re.sub(r"[^a-z0-9]+", " ", label).strip()
    return bool(re.search(r"\b(syllabus|outline|course outline|basic information)\b", normalized))


def _course_has_syllabus(course: Mapping[str, Any]) -> bool:
    """Detect syllabus availability from body, files, pages, or module items."""

    if _text(course.get("syllabus_body")) or _text(course.get("syllabus_text")):
        return True
    for field, default_type in (("files", "file"), ("resources", "resource"), ("pages", "page")):
        raw = course.get(field)
        if isinstance(raw, Mapping):
            raw = list(raw.values())
        if isinstance(raw, (list, tuple)):
            for item in raw:
                if isinstance(item, Mapping) and _is_syllabus_resource(item, _resource_kind(item, default=default_type)):
                    return True
    modules = course.get("modules")
    if isinstance(modules, Mapping):
        modules = list(modules.values())
    for module in modules if isinstance(modules, (list, tuple)) else []:
        if not isinstance(module, Mapping):
            continue
        items = module.get("items")
        if isinstance(items, Mapping):
            items = list(items.values())
        for item in items if isinstance(items, (list, tuple)) else []:
            if not isinstance(item, Mapping):
                continue
            item_type = _text(_first(item, "resource_type", "type", "kind")) or "file"
            if _is_syllabus_resource(item, item_type.lower().replace(" ", "_")):
                return True
    return False


def _resource_id(resource: Mapping[str, Any]) -> str | None:
    result = _scalar_id(
        _first(
            resource,
            "id",
            "file_id",
            "resource_id",
            "attachment_id",
            "page_id",
        )
    )
    if result is None:
        source_id = _text(resource.get("source_id"))
        if source_id:
            match = re.search(r"(?:^|:)(\d+)$", source_id)
            if match:
                result = match.group(1)
    if result is not None:
        return result
    url = _source_url(resource)
    if url:
        return f"url-{_hash_text(url)}"
    return None


def _resource_version(resource: Mapping[str, Any]) -> str | None:
    # The collector/archive preserves a stable version token when Canvas
    # exposes one.  Keep that token ahead of human-facing version labels so
    # two references to the same archived file resolve to the same key.
    archive = _mapping(resource.get("archive"))
    explicit = _scalar_id(
        _first(
            resource,
            "version_token",
            "versionToken",
            "version",
            "file_version",
            "revision",
            "lock_version",
        )
    ) or _scalar_id(_first(archive, "version_token", "versionToken"))
    if explicit is not None:
        return explicit

    # Archive records without a token still commonly carry the pair that
    # identifies a downloaded revision.  Hash the pair rather than exposing
    # a fragile timestamp/size delimiter in Source Key.  The same file ID,
    # updated_at, and size therefore gets the same version across an
    # assignment attachment and a course file reference.
    updated_at = _first(
        resource,
        "updated_at",
        "updatedAt",
        "modified_at",
        "modifiedAt",
        "last_modified",
        "lastModified",
    )
    size = _first(resource, "size", "file_size", "fileSize", "content_length", "bytes")
    normalized_updated = (
        _normalise_datetime(updated_at) if updated_at not in (None, "") else None
    )
    normalized_size = _scalar_id(size)
    if normalized_updated is not None or normalized_size is not None:
        return f"hash-{_hash_text({'updated_at': normalized_updated, 'size': normalized_size})}"
    return None


def _resource_kind(resource: Mapping[str, Any], default: str = "file") -> str:
    kind = _text(_first(resource, "resource_type", "type", "kind")) or default
    kind = kind.lower().replace(" ", "_")
    if kind in {"file", "resource", "attachment"} and _is_syllabus_resource(resource, kind):
        return "syllabus"
    return kind


def _resource_status(resource: Mapping[str, Any], *keys: str) -> str | None:
    return _text(_first(resource, *keys))


def _stable_download_status(resource: Mapping[str, Any]) -> str | None:
    """Collapse per-run download outcomes into stable projection values."""

    local_path = _text(_first(resource, "local_path", "archive_path", "download_path", "path"))
    raw = _text(_first(resource, "download_status", "status"))
    downloaded = resource.get("downloaded")
    if local_path:
        return "saved"
    if isinstance(downloaded, bool):
        return "available" if downloaded else "not_saved"
    if raw:
        lowered = raw.lower()
        if lowered in {"downloaded", "reused", "available", "saved"}:
            return "available"
        if lowered in {"not_downloaded", "not_saved", "missing", "pending"}:
            return "not_saved"
        if lowered in {"failed", "error"}:
            return "failed"
        return raw
    return None


def _attachment_items(assignment: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = assignment.get("attachments")
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    return [item for item in (raw or []) if isinstance(item, Mapping)] if isinstance(raw, (list, tuple)) else []


def _merge_relation(existing: Any, new_values: Iterable[str]) -> list[str]:
    values: list[str] = []
    if isinstance(existing, str):
        values.append(existing)
    elif isinstance(existing, (list, tuple)):
        values.extend(str(item) for item in existing if item is not None)
    values.extend(str(item) for item in new_values if item is not None)
    return sorted(set(values))


def _source_text(existing: Any, new_values: Iterable[str]) -> str:
    """Merge source provenance into a rich-text value.

    ``Source`` can point to assignments *and* announcements, which cannot be
    represented by one Notion relation property with a single target database.
    Newline-delimited keys keep every actual source while remaining valid rich
    text at the MCP boundary.
    """

    values: list[str] = []
    if isinstance(existing, str):
        values.extend(line.strip() for line in existing.splitlines() if line.strip())
    elif isinstance(existing, (list, tuple)):
        values.extend(str(item) for item in existing if item is not None)
    values.extend(str(item) for item in new_values if item is not None)
    return "\n".join(sorted(set(values)))


def _date_property(name: str, value: str | None) -> dict[str, Any]:
    """Flatten one Notion date into MCP-compatible keys."""

    if value is None:
        return {}
    is_datetime = not bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
    return {
        f"date:{name}:start": value,
        # Notion MCP's flattened date transport uses numeric 1/0 for this
        # flag; checkbox fields elsewhere continue to use YES/NO tokens.
        f"date:{name}:is_datetime": 1 if is_datetime else 0,
    }


def _dates_agree(left: str, right: str) -> bool:
    """Treat a reviewed date-only deadline as agreeing with same-day Canvas time."""

    if left == right:
        return True
    left_day = left[:10] if re.match(r"\d{4}-\d{2}-\d{2}", left) else None
    right_day = right[:10] if re.match(r"\d{4}-\d{2}-\d{2}", right) else None
    return left_day is not None and left_day == right_day


def _course_relation_properties(properties: Mapping[str, Any], info: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the root Notion relation convention for formal courses and hubs."""

    result = dict(properties)
    if info.get("mode") == "hub":
        result.pop("Course", None)
        course = _mapping(info.get("course"))
        result["Source Space"] = _course_title(course, f"Canvas course {info.get('key', '')}")
    else:
        result["Course"] = [str(info["key"])]
        result.pop("Source Space", None)
    return result


def _merge_record(existing: MutableMapping[str, Any], incoming: Mapping[str, Any]) -> None:
    existing_props = existing.setdefault("properties", {})
    for key, value in _mapping(incoming.get("properties")).items():
        if key == "Source":
            existing_props[key] = _source_text(existing_props.get(key), value if isinstance(value, list) else [value])
        elif key == "Resources":
            existing_props[key] = _merge_relation(existing_props.get(key), value if isinstance(value, list) else [value])
        elif key not in existing_props or existing_props[key] in (None, "", [], {}):
            existing_props[key] = value
    existing_generated = existing.setdefault("generated_content", {})
    for key, value in _mapping(incoming.get("generated_content")).items():
        if key not in existing_generated or existing_generated[key] in (None, "", [], {}):
            existing_generated[key] = value
        elif isinstance(existing_generated[key], list) and isinstance(value, list):
            existing_generated[key] = existing_generated[key] + [item for item in value if item not in existing_generated[key]]
    incoming_user = _mapping(incoming.get("user_content"))
    if incoming_user:
        existing.setdefault("user_content", {}).update(incoming_user)


def _resource_record(
    resource: Mapping[str, Any],
    *,
    course_key: str,
    term_key: str | None,
    origin: str,
    owner_id: str,
    default_type: str = "file",
) -> tuple[dict[str, Any] | None, str | None]:
    resource_id = _resource_id(resource)
    if resource_id is None:
        return None, None
    resource_type = _resource_kind(resource, default=default_type)
    version = _resource_version(resource)
    key = source_key(origin, owner_id, resource_type, resource_id, version=version)
    local_path = _text(_first(resource, "local_path", "archive_path", "download_path", "path"))
    source_url = _source_url(resource)
    download_status = _stable_download_status(resource)
    extraction_status = _resource_status(resource, "extraction_status", "extract_status")
    props = _props(
        **{
            "Name": _title(resource, f"Canvas {resource_type} {resource_id}"),
            "Source Key": key,
            "Course": course_key,
            "Term": term_key,
            "Source URL": source_url,
            "Canvas ID": resource_id,
            "Type": resource_type,
            "Syllabus": _yes_no(resource_type == "syllabus", default=NO),
            "Version": version,
            "Local Path": local_path,
            "Download Status": download_status,
            "Extraction Status": extraction_status,
            "SHA256": _text(_first(resource, "sha256", "checksum")),
            "Sync Status": "source",
        }
    )
    generated: dict[str, Any] = {}
    extracted = _text(resource.get("extracted_text"))
    if extracted:
        # Extracted document text is source content, not a generated summary.
        generated["extracted_text"] = extracted
        generated["extraction_status"] = extraction_status or "available"
    source_text = _text(resource.get("text"))
    if source_text:
        # Page/assignment text is source material.  The label makes that
        # distinction explicit to the reader and prevents it being mistaken
        # for an AI-generated summary.
        generated["page_original" if resource_type == "page" else "resource_original"] = _bounded_text(source_text)
        if len(source_text) > MAX_GENERATED_TEXT:
            generated["page_original_truncated" if resource_type == "page" else "resource_original_truncated"] = YES
    return _record("resources", key, course_key=course_key, properties=props, generated_content=generated), key


def _due_properties(values: Sequence[str]) -> dict[str, Any]:
    unique = sorted(set(values))
    conflict = len(unique) > 1
    result: dict[str, Any] = {"Due Conflict": YES if conflict else NO}
    if conflict:
        result["Due Choices"] = unique
    elif unique:
        result.update(_date_property("Due", unique[0]))
    return result


def _canonical_action_id(action: Mapping[str, Any], text_value: str) -> str:
    assignment_id = _scalar_id(_first(action, "assignment_id", "assignmentId"))
    if assignment_id:
        return f"assignment-{assignment_id}"
    explicit = _scalar_id(_first(action, "id", "action_id"))
    if explicit:
        return f"item-{explicit}"
    return f"text-{_hash_text(text_value)}"


def _reviewed_item_id(item: Mapping[str, Any], *, kind: str) -> str | None:
    """Return an explicit stable ID for a course review item.

    Syllabus actions and events are authored outside Canvas's object IDs.  A
    stable authored ``id`` is therefore required before they can become
    records.  Falling back to the prose would make a later paraphrase look
    like a new task/event, which is exactly the duplicate behaviour the
    enrichment contract is intended to prevent.
    """

    return _scalar_id(_first(item, "id", f"{kind}_id", f"{kind}Id"))


def _reviewed_source_ref(item: Mapping[str, Any]) -> Any:
    """Keep a human review packet's source reference intact for rendering."""

    return _first(item, "source_ref", "source_reference", "reference")


def _reviewed_provenance(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return source URL/reference fields for a reviewed item body."""

    result: dict[str, Any] = {}
    source_ref = _reviewed_source_ref(item)
    source_url = _source_url(item)
    if source_ref not in (None, "", [], {}):
        result["source_ref"] = source_ref
    if source_url:
        result["source_url"] = source_url
    return result


def _reviewed_action_text(action: Mapping[str, Any]) -> str | None:
    return _text(_first(action, "text", "task", "name", "title", "action"))


def _reviewed_action_type(action: Mapping[str, Any]) -> str:
    return _text(_first(action, "type", "action_type", "kind")) or "course_action"


def _iter_course_resources(course: Mapping[str, Any]) -> Iterable[tuple[Mapping[str, Any], str]]:
    for field, default_type in (("files", "file"), ("resources", "resource"), ("pages", "page")):
        raw = course.get(field)
        if isinstance(raw, Mapping):
            raw = list(raw.values())
        if isinstance(raw, (list, tuple)):
            for item in raw:
                if isinstance(item, Mapping):
                    yield item, default_type


def _course_mode(course: Mapping[str, Any]) -> str:
    value = _text(course.get("mode"))
    return (value or "course").lower()


def _term_key(snapshot: Mapping[str, Any]) -> str | None:
    return _text(_mapping(snapshot.get("term")).get("key"))


def _course_id(course: Mapping[str, Any]) -> str | None:
    return _scalar_id(_first(course, "id", "course_id"))


def _course_enrichment(index: Mapping[str, Any], course_id: str) -> Mapping[str, Any] | None:
    value = _mapping(index.get("courses")).get(course_id)
    return value if isinstance(value, Mapping) else None


def _announcement_enrichment(index: Mapping[str, Any], announcement_id: str) -> Mapping[str, Any] | None:
    value = _mapping(index.get("announcements")).get(announcement_id)
    return value if isinstance(value, Mapping) else None


def _assignment_id(assignment: Mapping[str, Any]) -> str | None:
    return _scalar_id(_first(assignment, "id", "assignment_id"))


def _assignment_canvas_status(assignment: Mapping[str, Any]) -> str | None:
    """Prefer current-student submission status over publication state."""

    submission = _mapping(assignment.get("submission"))
    nested_submission = _mapping(submission.get("submission"))
    status = _text(_first(submission, "workflow_state", "status")) or _text(
        _first(nested_submission, "workflow_state", "status")
    )
    return status or _text(_first(assignment, "workflow_state", "status"))


def _assignment_original_text(assignment: Mapping[str, Any]) -> str | None:
    """Return Canvas-provided assignment text for an explicitly labelled block."""

    return _text(_first(assignment, "text"))


def _event_original_text(event: Mapping[str, Any]) -> str | None:
    for field in ("text", "description", "details", "body"):
        value = _text(event.get(field))
        if not value:
            continue
        if field != "text" and extract_html_text is not None and "<" in value:
            try:
                value = extract_html_text(value)
            except Exception:
                pass
        return value
    return None


def _assignment_file_refs(assignment: Mapping[str, Any], base_url: str) -> list[Mapping[str, Any]]:
    """Find Canvas file references in assignment HTML/text fields."""

    if extract_html_file_refs is None:
        return []
    refs: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for field in ("description", "body", "message", "text"):
        value = assignment.get(field)
        if not value:
            continue
        try:
            candidates = extract_html_file_refs(value, base_url)
        except Exception:
            candidates = []
        for ref in candidates:
            if not isinstance(ref, Mapping):
                continue
            identity = _resource_id(ref) or _text(ref.get("source_id")) or _source_url(ref)
            if identity and identity not in seen:
                seen.add(identity)
                refs.append(ref)
    return refs


def _announcement_id(announcement: Mapping[str, Any]) -> str | None:
    return _scalar_id(_first(announcement, "id", "announcement_id"))


def _event_id(event: Mapping[str, Any]) -> str | None:
    result = _scalar_id(_first(event, "id", "event_id", "calendar_event_id"))
    if result:
        return result
    # Canvas occasionally returns an event without an ID.  The source fields
    # below are still actual source values, so the derived identity is stable
    # and inspectable rather than an arbitrary counter.
    identity = {
        "title": _title(event, ""),
        "start": _event_date(event, "start_at", "starts_at", "start", "start_date", "date"),
        "url": _source_url(event),
    }
    if any(identity.values()):
        return f"derived-{_hash_text(identity)}"
    return None


def _normalise_action_items(enrichment: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool, bool]:
    raw = enrichment.get("action_items", enrichment.get("actions"))
    supplied = "action_items" in enrichment or "actions" in enrichment
    if raw is None:
        return [], supplied, False
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    if not isinstance(raw, (list, tuple)):
        return [], supplied, False
    result: list[dict[str, Any]] = []
    truncated = False
    for item in raw:
        if isinstance(item, Mapping):
            text_value = _text(_first(item, "text", "task", "name", "title", "action"))
            if text_value is None:
                continue
            bounded = _bounded_text(text_value)
            truncated = truncated or len(text_value) > MAX_GENERATED_TEXT
            current = dict(item)
            current["text"] = bounded
            current["_truncated"] = len(text_value) > MAX_GENERATED_TEXT
            result.append(current)
        else:
            text_value = _text(item)
            if text_value is None:
                continue
            bounded = _bounded_text(text_value)
            truncated = truncated or len(text_value) > MAX_GENERATED_TEXT
            result.append({"text": bounded, "_truncated": len(text_value) > MAX_GENERATED_TEXT})
    return result, supplied, truncated


def _course_summary_content(
    course: Mapping[str, Any],
    enrichment: Mapping[str, Any] | None,
    *,
    global_reviewed: bool,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Build bounded reviewed syllabus content and a separate note payload."""

    has_syllabus = bool(_course_has_syllabus(course) or enrichment)
    generated: dict[str, Any] = {}
    note_content: dict[str, Any] = {}
    reviewed = isinstance(enrichment, Mapping) and _is_reviewed(enrichment, global_reviewed=global_reviewed)
    if reviewed and enrichment is not None:
        summary = _text(
            _first(
                enrichment,
                "syllabus_summary",
                "summary",
                "important_summary",
            )
        )
        if summary:
            bounded = _bounded_text(summary)
            generated["syllabus_summary"] = bounded
            generated["syllabus_summary_status"] = "reviewed"
            generated["syllabus_summary_truncated"] = _yes_no(len(summary) > MAX_GENERATED_TEXT, default=NO)
            note_content["syllabus_summary"] = bounded
        else:
            generated["syllabus_summary"] = PENDING_MARKER
            generated["syllabus_summary_status"] = "pending_review"
        important_raw = _first(enrichment, "important_info", "important_information", "key_info")
        important, truncated = _normalise_important_info(important_raw)
        if important:
            generated["important_info"] = important
            generated["important_info_status"] = "reviewed"
            generated["important_info_truncated"] = _yes_no(truncated, default=NO)
            note_content["important_info"] = important
        else:
            generated["important_info"] = [PENDING_MARKER]
            generated["important_info_status"] = "pending_review"
    elif has_syllabus:
        generated["syllabus_summary"] = PENDING_MARKER
        generated["syllabus_summary_status"] = "pending_review"
        generated["important_info"] = [PENDING_MARKER]
        generated["important_info_status"] = "pending_review"
    status = "reviewed" if reviewed else ("pending_review" if has_syllabus else "not_provided")
    if not note_content and has_syllabus:
        note_content = {
            "syllabus_summary": PENDING_MARKER,
            "important_info": [PENDING_MARKER],
        }
    return generated, status, note_content


def _build_views(term_key: str | None) -> dict[str, Any]:
    return {
        "cross_course_source_index": {
            "type": "linked_database",
            "databases": ["tasks", "announcements", "resources", "notes", "timetable"],
            "filter": {"property": "Source Key", "is_not_empty": True},
            "group_by": ["Course", "Type"],
            "sort": [{"property": "Name", "direction": "ascending"}],
            "purpose": "Compare source-backed material across formal courses and retained hubs.",
        },
        "term_filtered": {
            "type": "linked_database_collection",
            "databases": list(DATABASE_KINDS),
            "filter": {"property": "Term", "equals": term_key} if term_key else {"property": "Term", "is_not_empty": True},
            "group_by": ["Type"],
            "sort": [{"property": "Name", "direction": "ascending"}],
            "purpose": "Limit all projection databases to the selected Canvas term.",
        },
    }


def _comparison_metadata(plan_without_comparison: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    canonical = _canonical_json(plan_without_comparison)
    record_keys = [
        str(record.get("source_key"))
        for record in plan_without_comparison.get("records", [])
        if isinstance(record, Mapping) and record.get("source_key") is not None
    ]
    return {
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "canonicalization": "JSON UTF-8, sorted object keys, compact separators",
        "plan_hash_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "record_keys_sha256": hashlib.sha256(_canonical_json(sorted(record_keys)).encode("utf-8")).hexdigest(),
        "record_keys": sorted(record_keys),
        "snapshot_schema_version": snapshot.get("schema_version"),
    }


def build_plan(
    snapshot: Mapping[str, Any],
    enrichments: Mapping[str, Any] | None = None,
    bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic Canvas-to-Notion projection plan.

    ``enrichments`` follows the contract in ``references/projection-schema.md``.
    Only items explicitly marked ``reviewed: true`` (or a top-level reviewed
    marker) contribute generated summaries or action tasks.  ``bindings`` is
    accepted for root's apply phase: it is intentionally only copied as
    comparison metadata and never used to replace symbolic relation keys.
    """

    snapshot = _mapping(snapshot)
    enrichment_index = _index_enrichments(enrichments)
    origin = _origin(snapshot)
    owner_id = _owner_id(snapshot)
    term = _mapping(snapshot.get("term"))
    term_key = _term_key(snapshot)
    user = _mapping(snapshot.get("user"))
    display_timezone = _text(term.get("timezone")) or _text(user.get("time_zone"))
    courses = snapshot.get("courses")
    if isinstance(courses, Mapping):
        courses = list(courses.values())
    if not isinstance(courses, (list, tuple)):
        courses = []

    warnings: list[dict[str, Any]] = []
    seen_warnings: set[str] = set()

    def warn(
        code: str,
        message: str,
        *,
        scope: str = "projection",
        object_id: str | None = None,
    ) -> None:
        current = _safe_warning(
            {"scope": scope, "code": code, "message": message, "object_id": object_id},
            default_scope=scope,
            default_object_id=object_id,
        )
        key = _warning_key(current)
        if key not in seen_warnings:
            seen_warnings.add(key)
            warnings.append(current)

    for raw_warning in snapshot.get("warnings", []) if isinstance(snapshot.get("warnings"), (list, tuple)) else []:
        current = _safe_warning(raw_warning, default_scope="snapshot")
        key = _warning_key(current)
        if key not in seen_warnings:
            seen_warnings.add(key)
            warnings.append(current)

    records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    course_info: dict[str, dict[str, Any]] = {}
    assignment_index: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
    assignment_global_index: dict[str, list[tuple[dict[str, Any], str, str | None]]] = {}
    announcement_records: list[tuple[Mapping[str, Any], str, str | None, str, str]] = []
    action_tasks: list[tuple[Mapping[str, Any], Mapping[str, Any], str, str | None, str, str]] = []
    resource_keys_by_course: dict[str, list[str]] = {}
    resource_key_by_course_id: dict[tuple[str, str], str] = {}
    resource_kind_by_course_id: dict[tuple[str, str], str] = {}

    def add(record: dict[str, Any]) -> None:
        if display_timezone:
            record.setdefault("display_timezone", display_timezone)
        kind = str(record.get("kind"))
        key = str(record.get("source_key"))
        identity = (kind, key)
        existing = records_by_key.get(identity)
        if existing is None:
            records_by_key[identity] = record
        else:
            _merge_record(existing, record)

    # First pass: establish course identities and assignment/resource indexes.
    for raw_course in courses:
        course = _mapping(raw_course)
        course_id = _course_id(course)
        if course_id is None:
            warn("missing_course_id", "Course omitted because it has no Canvas ID", scope="course")
            continue
        mode = _course_mode(course)
        ckey = source_key(origin, owner_id, "course", course_id)
        course_info[course_id] = {
            "course": course,
            "mode": mode,
            "key": ckey,
            "formal": mode == "course",
            "processable": mode in {"course", "hub"},
        }
        if mode not in {"course", "hub", "ignore", "review"}:
            warn("unknown_course_mode", f"Course mode {mode!r} requires review", scope="course", object_id=course_id)
        if not course_info[course_id]["processable"]:
            continue
        assignments = course.get("assignments")
        if isinstance(assignments, Mapping):
            assignments = list(assignments.values())
        if not isinstance(assignments, (list, tuple)):
            assignments = []
        for raw_assignment in assignments:
            assignment = _mapping(raw_assignment)
            assignment_id = _assignment_id(assignment)
            if assignment_id is None:
                warn("missing_assignment_id", "Assignment omitted because it has no Canvas ID", scope="assignment", object_id=course_id)
                continue
            task_key = source_key(origin, owner_id, "assignment", assignment_id)
            assignment_index[(course_id, assignment_id)] = (assignment, task_key)
            assignment_global_index.setdefault(assignment_id, []).append((assignment, task_key, course_id))

    # Resource pass runs before tasks so attachment relation keys are available.
    for course_id, info in course_info.items():
        if not info["processable"]:
            continue
        course = info["course"]
        ckey = info["key"]
        resource_keys_by_course.setdefault(course_id, [])
        for raw_resource, default_type in _iter_course_resources(course):
            raw_resource_id = _resource_id(raw_resource)
            resource_record, resource_key = _resource_record(
                raw_resource,
                course_key=ckey,
                term_key=term_key,
                origin=origin,
                owner_id=owner_id,
                default_type=default_type,
            )
            if resource_record is None or resource_key is None:
                warn("missing_resource_id", "Resource omitted because it has no Canvas ID or source URL", scope="resource", object_id=course_id)
                continue
            resource_record["properties"] = _course_relation_properties(resource_record["properties"], info)
            add(resource_record)
            if resource_key not in resource_keys_by_course[course_id]:
                resource_keys_by_course[course_id].append(resource_key)
            if raw_resource_id is not None:
                identity = (course_id, raw_resource_id)
                candidate_kind = _resource_kind(raw_resource, default=default_type)
                existing_kind = resource_kind_by_course_id.get(identity)
                # Prefer a course file/page over an assignment attachment when
                # HTML points to the same Canvas file ID.
                if identity not in resource_key_by_course_id or (
                    existing_kind == "attachment" and candidate_kind != "attachment"
                ):
                    resource_key_by_course_id[identity] = resource_key
                    resource_kind_by_course_id[identity] = candidate_kind
        assignments = course.get("assignments")
        if isinstance(assignments, Mapping):
            assignments = list(assignments.values())
        for raw_assignment in assignments if isinstance(assignments, (list, tuple)) else []:
            assignment = _mapping(raw_assignment)
            for attachment in _attachment_items(assignment):
                attachment_id = _resource_id(attachment)
                existing_key = resource_key_by_course_id.get((course_id, attachment_id)) if attachment_id else None
                if existing_key is not None and resource_kind_by_course_id.get((course_id, attachment_id)) != "attachment":
                    if existing_key not in resource_keys_by_course[course_id]:
                        resource_keys_by_course[course_id].append(existing_key)
                    continue
                resource_record, resource_key = _resource_record(
                    attachment,
                    course_key=ckey,
                    term_key=term_key,
                    origin=origin,
                    owner_id=owner_id,
                    default_type="attachment",
                )
                if resource_record is None or resource_key is None:
                    warn("missing_attachment_id", "Assignment attachment omitted because it has no Canvas ID or source URL", scope="resource", object_id=_assignment_id(assignment))
                    continue
                resource_record["properties"] = _course_relation_properties(resource_record["properties"], info)
                add(resource_record)
                if resource_key not in resource_keys_by_course[course_id]:
                    resource_keys_by_course[course_id].append(resource_key)
                if attachment_id is not None:
                    identity = (course_id, attachment_id)
                    resource_key_by_course_id.setdefault(identity, resource_key)
                    resource_kind_by_course_id.setdefault(identity, _resource_kind(attachment, default="attachment"))

    # Formal course records are deliberately emitted only for mode=course.
    for course_id, info in course_info.items():
        if not info["formal"]:
            continue
        course = info["course"]
        ckey = info["key"]
        enrichment = _course_enrichment(enrichment_index, course_id)
        generated, enrichment_status, note_content = _course_summary_content(
            course,
            enrichment,
            global_reviewed=bool(enrichment_index.get("global_reviewed")),
        )
        assignments = course.get("assignments")
        if isinstance(assignments, Mapping):
            assignments = list(assignments.values())
        announcements = course.get("announcements")
        if isinstance(announcements, Mapping):
            announcements = list(announcements.values())
        assignment_count = len(assignments) if isinstance(assignments, (list, tuple)) else 0
        announcement_count = len(announcements) if isinstance(announcements, (list, tuple)) else 0
        course_props = _props(
            **{
                "Name": _course_title(course, f"Canvas course {course_id}"),
                "Source Key": ckey,
                "Canvas ID": course_id,
                "Course Code": _compact_course_code(course),
                "Course Mode": "course",
                "Term": term_key,
                "Source URL": _source_url(course),
                "Canvas Status": _text(_first(course, "workflow_state", "status")),
                "Syllabus Available": _yes_no(_course_has_syllabus(course), default=NO),
                "Enrichment Status": enrichment_status,
                "Announcement Count": announcement_count,
                "Assignment Count": assignment_count,
            }
        )
        add(
            _record(
                "courses",
                ckey,
                course_key=ckey,
                properties=course_props,
                generated_content=generated,
                user_content=_user_content(enrichment or {}),
            )
        )
        if generated.get("syllabus_summary_status") == "pending_review" or generated.get("important_info_status") == "pending_review":
            warn("pending_syllabus_enrichment", "Syllabus important information is pending reviewed enrichment", scope="course", object_id=course_id)
        # Syllabus notes are one per formal course and use the course as the
        # actual source relation.  A pending note is useful when syllabus HTML
        # exists, while courses without syllabus content get no synthetic note.
        if note_content:
            note_key = source_key(origin, owner_id, "syllabus", course_id)
            note_props = _props(
                **{
                    "Name": f"{_course_title(course, f'Canvas course {course_id}')} syllabus",
                    "Source Key": note_key,
                    "Course": ckey,
                    "Term": term_key,
                    "Source URL": _source_url(course),
                    "Type": "syllabus",
                    "Sync Status": enrichment_status,
                }
            )
            add(
                _record(
                    "notes",
                    note_key,
                    course_key=ckey,
                    properties=_course_relation_properties(note_props, info),
                    generated_content=note_content,
                    user_content=_user_content(enrichment or {}),
                )
            )

    def ensure_html_resource(
        course_id: str,
        info: Mapping[str, Any],
        reference: Mapping[str, Any],
    ) -> str | None:
        """Reuse a course resource for an HTML file reference, or add one once."""

        reference_id = _resource_id(reference)
        if reference_id is None:
            return None
        identity = (course_id, reference_id)
        existing_key = resource_key_by_course_id.get(identity)
        if existing_key is not None:
            return existing_key
        resource_record, resource_key = _resource_record(
            reference,
            course_key=str(info["key"]),
            term_key=term_key,
            origin=origin,
            owner_id=owner_id,
            default_type="file",
        )
        if resource_record is None or resource_key is None:
            return None
        resource_record["properties"] = _course_relation_properties(resource_record["properties"], info)
        add(resource_record)
        resource_keys_by_course.setdefault(course_id, []).append(resource_key)
        resource_key_by_course_id[identity] = resource_key
        resource_kind_by_course_id[identity] = _resource_kind(reference, default="file")
        return resource_key

    # Assignments always become tasks for processable courses, including no due
    # date and no attachments.  This pass also captures date conflicts before
    # announcement action-item deduplication.
    for course_id, info in course_info.items():
        if not info["processable"]:
            continue
        course = info["course"]
        ckey = info["key"]
        assignments = course.get("assignments")
        if isinstance(assignments, Mapping):
            assignments = list(assignments.values())
        if not isinstance(assignments, (list, tuple)):
            continue
        for raw_assignment in assignments:
            assignment = _mapping(raw_assignment)
            assignment_id = _assignment_id(assignment)
            if assignment_id is None:
                continue
            task_key = source_key(origin, owner_id, "assignment", assignment_id)
            due_values = _effective_date_values(assignment)
            attachments = _attachment_items(assignment)
            attachment_keys: list[str] = []
            for attachment in attachments:
                attachment_id = _resource_id(attachment)
                if attachment_id is not None:
                    attachment_key = resource_key_by_course_id.get((course_id, attachment_id))
                    if attachment_key is None:
                        attachment_key = source_key(
                            origin,
                            owner_id,
                            _resource_kind(attachment, default="attachment"),
                            attachment_id,
                            version=_resource_version(attachment),
                        )
                    attachment_keys.append(attachment_key)
            # Canvas assignment HTML may link files without populating the
            # assignment.attachments array.  Match by Canvas file ID and reuse
            # the course resource key instead of creating an attachment twin.
            for reference in _assignment_file_refs(assignment, origin):
                reference_key = ensure_html_resource(course_id, info, reference)
                if reference_key is not None:
                    attachment_keys.append(reference_key)
            attachment_keys = sorted(set(attachment_keys))
            task_props = _props(
                **{
                    "Name": _title(assignment, f"Canvas assignment {assignment_id}"),
                    "Source Key": task_key,
                    "Course": ckey,
                    "Term": term_key,
                    "Source URL": _source_url(assignment),
                    "Source": _source_text(None, [task_key]),
                    "Type": "assignment",
                    **_due_properties(due_values),
                    "Has Attachments": _yes_no(bool(attachment_keys), default=NO),
                    "Attachment Count": len(attachment_keys),
                    "Resources": attachment_keys,
                    "Assignment ID": assignment_id,
                    "Canvas Status": _assignment_canvas_status(assignment),
                    "Sync Status": "source",
                }
            )
            generated_task: dict[str, Any] = {}
            assignment_text = _assignment_original_text(assignment)
            if assignment_text:
                generated_task["assignment_original"] = _bounded_text(assignment_text)
                if len(assignment_text) > MAX_GENERATED_TEXT:
                    generated_task["assignment_original_truncated"] = YES
            if len(set(due_values)) > 1:
                generated_task["date_conflict"] = {
                    "status": "conflict",
                    "values": sorted(set(due_values)),
                    "marker": "[DATE CONFLICT]",
                }
                warn("assignment_date_conflict", "Assignment has multiple due dates; no single Due value was selected", scope="assignment", object_id=assignment_id)
            add(
                _record(
                    "tasks",
                    task_key,
                    course_key=ckey,
                    properties=_course_relation_properties(task_props, info),
                    generated_content=generated_task,
                )
            )

    # A reviewed course enrichment can contain assessed items that Canvas did
    # not expose as assignments (for example an exam or a slide-deck deadline
    # found in a syllabus PDF).  These are source-backed tasks in their own
    # right.  An explicit assignment_id links one to an existing assignment
    # in the *same* course; it must never turn into a guessed Canvas ID or a
    # cross-course merge.
    for course_id, info in course_info.items():
        if not info["processable"]:
            continue
        enrichment = _course_enrichment(enrichment_index, course_id)
        if not isinstance(enrichment, Mapping) or not _is_reviewed(
            enrichment,
            global_reviewed=bool(enrichment_index.get("global_reviewed")),
        ):
            continue
        raw_actions = enrichment.get("actions", enrichment.get("action_items"))
        if isinstance(raw_actions, Mapping):
            raw_actions = list(raw_actions.items())
            action_items: list[tuple[str, Any]] = [
                (_scalar_id(item_id) or "", item) for item_id, item in raw_actions
            ]
        elif isinstance(raw_actions, (list, tuple)):
            action_items = [("", item) for item in raw_actions]
        else:
            action_items = []
        ckey = str(info["key"])
        for container_id, raw_action in action_items:
            action = _mapping(raw_action)
            action_id = _reviewed_item_id(action, kind="action") or container_id
            action_text = _reviewed_action_text(action)
            if action_id is None or not action_id:
                warn(
                    "missing_reviewed_action_id",
                    "Reviewed course action omitted because it has no stable id",
                    scope="course_action",
                    object_id=course_id,
                )
                continue
            if action_text is None:
                warn(
                    "missing_reviewed_action_text",
                    "Reviewed course action omitted because it has no text",
                    scope="course_action",
                    object_id=str(action_id),
                )
                continue
            action_key = source_key(
                origin,
                owner_id,
                "course_action",
                f"{course_id}:{action_id}",
            )
            assignment_id = _scalar_id(_first(action, "assignment_id", "assignmentId"))
            assignment_match = (
                assignment_index.get((course_id, assignment_id))
                if assignment_id is not None
                else None
            )
            action_due_values = _effective_date_values(action)
            action_text_bounded = _bounded_text(action_text)
            action_type = _reviewed_action_type(action)
            provenance = _reviewed_provenance(action)
            source_entry: dict[str, Any] = {
                "id": str(action_id),
                "text": action_text_bounded,
                "type": action_type,
            }
            source_entry.update(provenance)

            if assignment_id is not None and assignment_match is None:
                warn(
                    "unresolved_course_action_assignment",
                    "Reviewed course action references an assignment_id not present in the same course",
                    scope="course_action",
                    object_id=str(action_id),
                )
            if assignment_match is not None:
                assignment_task = records_by_key.get(("tasks", assignment_match[1]))
                if assignment_task is not None:
                    props = assignment_task.setdefault("properties", {})
                    props["Source"] = _source_text(props.get("Source"), [action_key])
                    generated = assignment_task.setdefault("generated_content", {})
                    actions = generated.setdefault("reviewed_actions", [])
                    if action_text_bounded not in actions:
                        actions.append(action_text_bounded)
                    action_sources = generated.setdefault("reviewed_action_sources", [])
                    if source_entry not in action_sources:
                        action_sources.append(source_entry)
                    if len(action_text) > MAX_GENERATED_TEXT:
                        generated["reviewed_actions_truncated"] = YES

                    # A reviewed source may fill a missing Canvas due date;
                    # if it disagrees with the effective Canvas date, retain
                    # both values as an explicit conflict.
                    existing_due = _text(props.get("date:Due:start"))
                    unique_action_dates = sorted(set(action_due_values))
                    if unique_action_dates:
                        date_conflict = (
                            len(unique_action_dates) > 1
                            or (
                                existing_due is not None
                                and any(
                                    not _dates_agree(value, existing_due)
                                    for value in unique_action_dates
                                )
                            )
                            or props.get("Due Conflict") == YES
                        )
                        if date_conflict:
                            choices = set(_mapping(props).get("Due Choices", []))
                            if existing_due:
                                choices.add(existing_due)
                            choices.update(unique_action_dates)
                            props.pop("date:Due:start", None)
                            props.pop("date:Due:is_datetime", None)
                            props["Due Choices"] = sorted(choices)
                            props["Due Conflict"] = YES
                            generated["date_conflict"] = {
                                "status": "conflict",
                                "values": sorted(choices),
                                "marker": "[DATE CONFLICT]",
                            }
                            warn(
                                "reviewed_course_action_date_conflict",
                                "Reviewed course action date disagrees with the effective assignment date; no single Due value was selected",
                                scope="course_action",
                                object_id=str(action_id),
                            )
                        elif existing_due is None:
                            props.update(_date_property("Due", unique_action_dates[0]))
                            generated["due_from_reviewed_action"] = YES
                    continue

            action_props = _props(
                **{
                    "Name": action_text_bounded,
                    "Source Key": action_key,
                    "Course": ckey,
                    "Term": term_key,
                    "Source URL": _source_url(action),
                    "Source": _source_text(None, [action_key]),
                    "Type": action_type,
                    **_due_properties(action_due_values),
                    "Assignment ID": assignment_id,
                    "Sync Status": "reviewed",
                }
            )
            generated_action: dict[str, Any] = {
                "reviewed_action": action_text_bounded,
                "reviewed_action_id": str(action_id),
                "reviewed_action_type": action_type,
            }
            generated_action.update(provenance)
            if len(action_text) > MAX_GENERATED_TEXT:
                generated_action["reviewed_action_truncated"] = YES
            if len(set(action_due_values)) > 1:
                generated_action["date_conflict"] = {
                    "status": "conflict",
                    "values": sorted(set(action_due_values)),
                    "marker": "[DATE CONFLICT]",
                }
                warn(
                    "course_action_date_conflict",
                    "Reviewed course action has multiple due dates; no single Due value was selected",
                    scope="course_action",
                    object_id=str(action_id),
                )
            add(
                _record(
                    "tasks",
                    action_key,
                    course_key=ckey,
                    properties=_course_relation_properties(action_props, info),
                    generated_content=generated_action,
                    user_content=_user_content(action),
                )
            )

    # Announcements are retained for formal courses and hubs.  Their raw body
    # is not converted to an inferred summary.  Reviewed enrichment alone can
    # produce generated summaries or action tasks.
    for course_id, info in course_info.items():
        if not info["processable"]:
            continue
        course = info["course"]
        ckey = info["key"]
        announcements = course.get("announcements")
        if isinstance(announcements, Mapping):
            announcements = list(announcements.values())
        if not isinstance(announcements, (list, tuple)):
            announcements = []
        for raw_announcement in announcements:
            announcement = _mapping(raw_announcement)
            announcement_id = _announcement_id(announcement)
            if announcement_id is None:
                warn("missing_announcement_id", "Announcement omitted because it has no Canvas ID", scope="announcement", object_id=course_id)
                continue
            akey = source_key(origin, owner_id, "announcement", announcement_id)
            enrichment = _announcement_enrichment(enrichment_index, announcement_id)
            reviewed = isinstance(enrichment, Mapping) and _is_reviewed(
                enrichment,
                global_reviewed=bool(enrichment_index.get("global_reviewed")),
            )
            summary = _text(_first(enrichment or {}, "summary", "announcement_summary")) if reviewed else None
            action_items, action_items_supplied, action_items_truncated = (
                _normalise_action_items(enrichment) if reviewed and enrichment is not None else ([], False, False)
            )
            generated_announcement: dict[str, Any] = {}
            if summary:
                generated_announcement["summary"] = _bounded_text(summary)
                generated_announcement["summary_status"] = "reviewed"
                generated_announcement["summary_truncated"] = _yes_no(len(summary) > MAX_GENERATED_TEXT, default=NO)
            else:
                generated_announcement["summary"] = PENDING_MARKER
                generated_announcement["summary_status"] = "pending_review"
            if action_items_supplied:
                generated_announcement["action_items_status"] = "reviewed"
                generated_announcement["action_items"] = [
                    {
                        "text": action_item["text"],
                        **(
                            {"assignment_id": _scalar_id(_first(action_item, "assignment_id", "assignmentId"))}
                            if _scalar_id(_first(action_item, "assignment_id", "assignmentId")) is not None
                            else {}
                        ),
                    }
                    for action_item in action_items
                ]
            else:
                generated_announcement["action_items_status"] = "pending_review"
                generated_announcement["action_items"] = [PENDING_MARKER]
            if action_items_truncated:
                generated_announcement["action_items_truncated"] = YES
            announcement_props = _props(
                **{
                    "Name": _title(announcement, f"Canvas announcement {announcement_id}"),
                    "Source Key": akey,
                    "Course": ckey,
                    "Term": term_key,
                    "Source URL": _source_url(announcement),
                    "Canvas ID": announcement_id,
                    **_date_property(
                        "Posted",
                        _normalise_datetime(_first(announcement, "posted_at", "published_at", "created_at")),
                    ),
                    "Type": "announcement",
                    "Has Action Items": _yes_no(bool(action_items), default=NO),
                    "Sync Status": "reviewed" if reviewed else "pending_review",
                }
            )
            add(
                _record(
                    "announcements",
                    akey,
                    course_key=ckey,
                    properties=_course_relation_properties(announcement_props, info),
                    generated_content=generated_announcement,
                    user_content=_user_content(enrichment or {}),
                )
            )
            if not reviewed:
                warn("pending_announcement_enrichment", "Announcement summary and action items are pending reviewed enrichment", scope="announcement", object_id=announcement_id)
            announcement_records.append((announcement, akey, ckey, course_id, announcement_id))
            for action in action_items:
                action_tasks.append((action, announcement, akey, ckey, course_id, announcement_id))

    # Convert only reviewed action items to tasks.  A reviewed assignment_id
    # links an action to an existing assignment task instead of duplicating it.
    for action, announcement, announcement_key, ckey, course_id, announcement_id in action_tasks:
        action_text = _text(action.get("text"))
        if action_text is None:
            continue
        assignment_id = _scalar_id(_first(action, "assignment_id", "assignmentId"))
        action_due_values = _effective_date_values(action)
        assignment_match: tuple[dict[str, Any], str] | None = None
        if assignment_id is not None:
            assignment_match = assignment_index.get((course_id, assignment_id))
            if assignment_match is None:
                global_matches = assignment_global_index.get(assignment_id, [])
                if len(global_matches) == 1:
                    assignment_match = (global_matches[0][0], global_matches[0][1])
                elif len(global_matches) > 1:
                    warn("ambiguous_action_assignment", "Action item assignment_id matches multiple courses; action remains a separate task", scope="announcement", object_id=announcement_id)
            if assignment_match is not None:
                assignment_task = records_by_key.get(("tasks", assignment_match[1]))
                if assignment_task is not None:
                    props = assignment_task.setdefault("properties", {})
                    props["Source"] = _source_text(props.get("Source"), [announcement_key])
                    generated = assignment_task.setdefault("generated_content", {})
                    actions = generated.setdefault("reviewed_actions", [])
                    if action_text not in actions:
                        actions.append(action_text)
                    if action.get("_truncated"):
                        generated["reviewed_actions_truncated"] = YES
                    # A reviewed announcement may carry an explicit date.  It
                    # can fill an assignment's otherwise missing date, but a
                    # disagreement with the effective Canvas date is surfaced
                    # as a conflict rather than silently replacing it.
                    existing_due = _text(props.get("date:Due:start"))
                    unique_action_dates = sorted(set(action_due_values))
                    if unique_action_dates:
                        if len(unique_action_dates) > 1 or (
                            existing_due is not None and any(not _dates_agree(value, existing_due) for value in unique_action_dates)
                        ) or props.get("Due Conflict") == YES:
                            choices = set(_mapping(props).get("Due Choices", []))
                            if existing_due:
                                choices.add(existing_due)
                            choices.update(unique_action_dates)
                            props.pop("date:Due:start", None)
                            props.pop("date:Due:is_datetime", None)
                            props["Due Choices"] = sorted(choices)
                            props["Due Conflict"] = YES
                            generated["date_conflict"] = {
                                "status": "conflict",
                                "values": sorted(choices),
                                "marker": "[DATE CONFLICT]",
                            }
                            warn(
                                "reviewed_action_date_conflict",
                                "Reviewed announcement date disagrees with the effective assignment date; no single Due value was selected",
                                scope="announcement",
                                object_id=announcement_id,
                            )
                        elif existing_due is None:
                            props.update(_date_property("Due", unique_action_dates[0]))
                            generated["due_from_reviewed_action"] = YES
                    # The announcement itself remains the canonical source of
                    # the reviewed action; the assignment task is just linked.
                    continue
            else:
                warn("unresolved_action_assignment", "Reviewed action item references an assignment_id not present in the snapshot", scope="announcement", object_id=announcement_id)
        action_id = _canonical_action_id(action, action_text)
        action_key = source_key(origin, owner_id, "announcement_action", f"{announcement_id}:{action_id}")
        due_values = action_due_values
        action_props = _props(
            **{
                "Name": action_text,
                "Source Key": action_key,
                "Course": ckey,
                "Term": term_key,
                "Source URL": _source_url(announcement),
                "Source": _source_text(None, [announcement_key]),
                "Type": "announcement_action",
                **_due_properties(due_values),
                "Announcement ID": announcement_id,
                "Assignment ID": assignment_id,
                "Sync Status": "reviewed",
            }
        )
        generated_action: dict[str, Any] = {"reviewed_action": action_text}
        if action.get("_truncated"):
            generated_action["reviewed_action_truncated"] = YES
        if len(set(due_values)) > 1:
            generated_action["date_conflict"] = {
                "status": "conflict",
                "values": sorted(set(due_values)),
                "marker": "[DATE CONFLICT]",
            }
            warn("action_date_conflict", "Reviewed action has multiple due dates; no single Due value was selected", scope="announcement", object_id=announcement_id)
        action_info = course_info.get(course_id)
        if action_info is not None:
            action_props = _course_relation_properties(action_props, action_info)
        add(_record("tasks", action_key, course_key=ckey, properties=action_props, generated_content=generated_action, user_content=_user_content(action)))

    # Timetable events remain source-backed even when they have no assignment.
    for course_id, info in course_info.items():
        if not info["processable"]:
            continue
        course = info["course"]
        ckey = info["key"]
        events = course.get("calendar_events")
        if isinstance(events, Mapping):
            events = list(events.values())
        if not isinstance(events, (list, tuple)):
            continue
        for raw_event in events:
            event = _mapping(raw_event)
            event_id = _event_id(event)
            if event_id is None:
                warn("missing_calendar_event_id", "Calendar event omitted because it has no stable source identity", scope="calendar", object_id=course_id)
                continue
            event_key = source_key(origin, owner_id, "calendar_event", event_id)
            start = _event_date(event, "start_at", "starts_at", "start", "start_date", "date")
            end = _event_date(event, "end_at", "ends_at", "end", "end_date")
            if start is None and end is None:
                warn("missing_calendar_event_date", "Calendar event omitted because it has no start or end date", scope="calendar", object_id=event_id)
                continue
            all_day_value = event.get("all_day")
            if all_day_value is None:
                all_day_value = bool(start and re.fullmatch(r"\d{4}-\d{2}-\d{2}", start))
            event_props = _props(
                **{
                    "Name": _title(event, f"Canvas event {event_id}"),
                    "Source Key": event_key,
                    "Course": ckey,
                    "Term": term_key,
                    "Source URL": _source_url(event),
                    **_date_property("Start", start),
                    **_date_property("End", end),
                    "All Day": _yes_no(all_day_value, default=NO),
                    "Type": _text(_first(event, "event_type", "type", "context_type")) or "calendar_event",
                    "Sync Status": "source",
                }
            )
            generated_event: dict[str, Any] = {}
            location_name = _text(_first(event, "location_name", "location", "locationName"))
            if location_name:
                generated_event["location_name"] = location_name
            event_text = _event_original_text(event)
            if event_text:
                generated_event["description_original"] = _bounded_text(event_text)
                if len(event_text) > MAX_GENERATED_TEXT:
                    generated_event["description_original_truncated"] = YES
            add(
                _record(
                    "timetable",
                    event_key,
                    course_key=ckey,
                    properties=_course_relation_properties(event_props, info),
                    generated_content=generated_event,
                )
            )

    # Reviewed course events cover schedule facts that are present in a
    # syllabus/outline but absent from Canvas calendar_events.  They are kept
    # in Timetable with a course-scoped stable key and never inferred from raw
    # PDF prose.
    for course_id, info in course_info.items():
        if not info["processable"]:
            continue
        enrichment = _course_enrichment(enrichment_index, course_id)
        if not isinstance(enrichment, Mapping) or not _is_reviewed(
            enrichment,
            global_reviewed=bool(enrichment_index.get("global_reviewed")),
        ):
            continue
        raw_events = enrichment.get("events")
        if isinstance(raw_events, Mapping):
            event_items: list[tuple[str, Any]] = [
                (_scalar_id(item_id) or "", item) for item_id, item in raw_events.items()
            ]
        elif isinstance(raw_events, (list, tuple)):
            event_items = [("", item) for item in raw_events]
        else:
            event_items = []
        ckey = str(info["key"])
        for container_id, raw_event in event_items:
            event = _mapping(raw_event)
            event_id = _reviewed_item_id(event, kind="event") or container_id
            if event_id is None or not event_id:
                warn(
                    "missing_reviewed_event_id",
                    "Reviewed course event omitted because it has no stable id",
                    scope="reviewed_event",
                    object_id=course_id,
                )
                continue
            start = _event_date(event, "start_at", "starts_at", "start", "start_date")
            end = _event_date(event, "end_at", "ends_at", "end", "end_date")
            if start is None:
                warn(
                    "missing_reviewed_event_start",
                    "Reviewed course event omitted because it has no start_at",
                    scope="reviewed_event",
                    object_id=str(event_id),
                )
                continue
            event_key = source_key(
                origin,
                owner_id,
                "reviewed_event",
                f"{course_id}:{event_id}",
            )
            all_day_value = event.get("all_day")
            if all_day_value is None:
                all_day_value = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", start))
            event_props = _props(
                **{
                    "Name": _title(event, f"Reviewed event {event_id}"),
                    "Source Key": event_key,
                    "Course": ckey,
                    "Term": term_key,
                    "Source URL": _source_url(event),
                    **_date_property("Start", start),
                    **_date_property("End", end),
                    "All Day": _yes_no(all_day_value, default=NO),
                    "Type": _text(_first(event, "type", "event_type", "kind")) or "reviewed_event",
                    "Sync Status": "reviewed",
                }
            )
            generated_event: dict[str, Any] = {
                "reviewed_event_id": str(event_id),
            }
            generated_event.update(_reviewed_provenance(event))
            location_name = _text(_first(event, "location_name", "location", "locationName"))
            if location_name:
                generated_event["location_name"] = location_name
            event_text = _event_original_text(event)
            if event_text:
                generated_event["description_original"] = _bounded_text(event_text)
                if len(event_text) > MAX_GENERATED_TEXT:
                    generated_event["description_original_truncated"] = YES
            add(
                _record(
                    "timetable",
                    event_key,
                    course_key=ckey,
                    properties=_course_relation_properties(event_props, info),
                    generated_content=generated_event,
                    user_content=_user_content(event),
                )
            )

    # Ensure every plan relation points at an actual source key, and that all
    # record properties remain flat.  This catches accidental personal fields
    # introduced by future changes without dropping source records.
    for record in records_by_key.values():
        properties = record.get("properties", {})
        for key in list(properties):
            if key.strip().lower() in _PERSONAL_PROPERTY_NAMES:
                del properties[key]

    records = sorted(
        records_by_key.values(),
        key=lambda item: (_KIND_ORDER.get(str(item.get("kind")), 999), str(item.get("source_key"))),
    )
    stats = {
        "record_counts": {kind: sum(1 for record in records if record.get("kind") == kind) for kind in DATABASE_KINDS},
        "record_count": len(records),
        "warning_count": len(warnings),
        "pending_enrichment_count": sum(
            1
            for record in records
            if any(
                isinstance(value, str) and "pending_review" in value
                for value in _mapping(record.get("generated_content")).values()
            )
        ),
        "bindings_supplied": bool(bindings),
    }
    plan: dict[str, Any] = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "term": dict(term),
        "databases": json.loads(_canonical_json(DATABASE_SCHEMAS)),
        "records": records,
        "views": _build_views(term_key),
        "warnings": warnings,
        "stats": stats,
    }
    plan["comparison"] = _comparison_metadata({key: value for key, value in plan.items() if key != "comparison"}, snapshot)
    return plan


def _ics_escape(value: Any) -> str:
    text_value = str(value or "")
    return text_value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_datetime(value: str, timezone_name: str | None = None) -> tuple[str | None, bool]:
    """Return an RFC 5545 date/time, using UTC for aware values.

    A naïve date-time is only exported when the record supplies a valid
    display timezone.  Without one, returning ``None`` lets the caller skip
    the event rather than silently treating a local Canvas time as UTC.
    """

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value.replace("-", ""), True
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None, False
    if parsed.tzinfo is None:
        if not timezone_name:
            return None, False
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except Exception:
            return None, False
    # RFC 5545's trailing Z makes the UTC interpretation explicit.  This also
    # preserves daylight-saving transitions because conversion happens before
    # formatting.
    parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y%m%dT%H%M%SZ"), False


def _render_marker(value: Any) -> str:
    """Translate machine markers to readable text without hiding uncertainty."""

    text_value = _text(value)
    if text_value is None:
        return ""
    text_value = text_value.replace(PENDING_MARKER, "待人工审核")
    text_value = text_value.replace("[DATE CONFLICT]", "日期冲突")
    return text_value


def _render_item(value: Any) -> str:
    if isinstance(value, Mapping):
        text_value = _text(_first(value, "text", "label", "value", "name", "title", "action"))
        if text_value:
            return _render_marker(text_value)
        parts = []
        for key in sorted(value, key=str):
            child = _render_item(value[key])
            if child:
                parts.append(f"{key}: {child}")
        return "；".join(parts)
    if isinstance(value, (list, tuple)):
        rendered_items = [_render_item(item) for item in value]
        return "；".join(item for item in rendered_items if item)
    return _render_marker(value)


def _render_source_link(props: Mapping[str, Any]) -> str | None:
    url = _text(props.get("Source URL"))
    if not url:
        return None
    safe_url = url.replace("(", "%28").replace(")", "%29")
    return f"来源：[Canvas 页面]({safe_url})"


def _render_reviewed_provenance(item: Mapping[str, Any], *, label: str = "来源依据") -> list[str]:
    """Render an enrichment source reference and URL inside the page body."""

    lines: list[str] = []
    source_ref = _render_item(item.get("source_ref"))
    if source_ref:
        lines.append(f"- {label}：{source_ref}")
    source_url = _text(item.get("source_url"))
    if source_url:
        safe_url = source_url.replace("(", "%28").replace(")", "%29")
        lines.append(f"- 来源链接：[原始资料]({safe_url})")
    return lines


def _render_original_heading(label: str, *, truncated: bool = False) -> str:
    return f"### {label}（节选）" if truncated else f"### {label}"


def _literal_source_block(value: str, *, limit: int) -> tuple[str, bool]:
    """Bound raw text before fencing so Markdown cannot reinterpret its data.

    A longer fence preserves any backtick runs already present in the source.
    Closing fences count toward the budget and are never themselves clipped.
    """
    length = min(len(value), max(0, limit - 10))
    while length > 0:
        truncated = length < len(value)
        content = value[:length] + ("…" if truncated else "")
        longest = max((len(run) for run in re.findall(r"`+", content)), default=0)
        fence = "`" * max(3, longest + 1)
        block = f"{fence}text\n{content}\n{fence}"
        if len(block) <= limit:
            return block, truncated
        length -= max(1, len(block) - limit)
    return "", bool(value)


def _render_resource_source(lines: list[str], generated: Mapping[str, Any]) -> list[str]:
    """Keep source excerpts literal while headings and links remain Markdown."""
    sections = []
    extracted = _text(generated.get("extracted_text"))
    if extracted:
        sections.append(("资源原文", extracted, False))
    original = _text(generated.get("page_original")) or _text(generated.get("resource_original"))
    if original:
        sections.append(("页面原文", original, generated.get("page_original_truncated") == YES or generated.get("resource_original_truncated") == YES))
    notice = "> 原文过长，以上为节选；请通过来源链接查看完整内容。"
    limit = MAX_RENDERED_TEXT_BY_KIND["resources"]
    for label, content, previously_truncated in sections:
        # Reserve space for a full heading and an excerpt notice. Source links
        # are appended separately by the caller, outside this body budget.
        heading = _render_original_heading(label, truncated=True)
        available = limit - len("\n".join(lines)) - len(heading) - len(notice) - 3
        block, truncated = _literal_source_block(content, limit=available)
        if block:
            lines.extend([_render_original_heading(label, truncated=previously_truncated or truncated), block])
        if truncated:
            lines.append(notice)
            break
    return lines


def _display_date(value: str | None, timezone_name: str | None) -> str | None:
    if value is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value
    if timezone_name:
        try:
            parsed = parsed.astimezone(ZoneInfo(timezone_name))
        except Exception:
            pass
    label = parsed.strftime("%Y-%m-%d %H:%M")
    zone_label = parsed.tzname()
    return f"{label} {zone_label}" if zone_label else label


def _render_date(props: Mapping[str, Any], name: str, timezone_name: str | None = None) -> str | None:
    return _display_date(_text(props.get(f"date:{name}:start")), timezone_name)


def render_generated_content(record: Mapping[str, Any]) -> str:
    """Render generated content as readable Markdown for a managed page.

    The output has stable Chinese section labels, preserves pending and date
    conflict markers in reader-friendly form, renders announcement action
    items as unchecked Markdown boxes, and includes source URLs when available.  It
    intentionally omits technical ``source_key`` values and caller-owned
    ``user_content``; root keeps those in properties and its own user section.
    """

    props = _mapping(record.get("properties"))
    generated = _mapping(record.get("generated_content"))
    kind = _text(record.get("kind")) or ""
    display_timezone = _text(record.get("display_timezone"))
    lines: list[str] = []
    name = _text(props.get("Name"))

    if kind == "courses":
        summary = _render_marker(generated.get("syllabus_summary"))
        if summary:
            summary_truncated = generated.get("syllabus_summary_truncated") == YES
            lines.extend([_render_original_heading("课程概述", truncated=summary_truncated), summary])
        important = generated.get("important_info")
        if isinstance(important, (list, tuple)) and important:
            info_truncated = generated.get("important_info_truncated") == YES
            lines.append(_render_original_heading("重要信息", truncated=info_truncated))
            for item in important:
                rendered_item = _render_item(item)
                if rendered_item:
                    lines.append(f"- {rendered_item}")
    elif kind == "tasks":
        if name:
            lines.extend(["### 任务", f"- {name}", "- 完成请使用 Notion 的 Done 属性。"])
        due = _render_date(props, "Due", display_timezone)
        if due:
            lines.append(f"- 截止：{due}")
        study_date = _render_date(props, "Study Date", display_timezone)
        if study_date:
            lines.append(f"- 课程准备日期：{study_date}（建议学习日期；与提交截止分别记录）")
        if props.get("Planning Origin"):
            labels={"Teacher requirement":"教师要求","Teacher recommendation":"教师推荐","Assistant suggestion":"助手建议"}
            lines.append("- 安排来源：" + labels.get(props["Planning Origin"],props["Planning Origin"]))
        before=_render_date(props,"Prepare Before",display_timezone)
        if before:
            lines.append("- 对应课节／考核之前准备："+before)
        if props.get("Preparation Status") not in (None,"Active"):
            labels={"Cancelled":"课节取消","Merged":"已合并到后续准备","Past":"准备时段已过","Deferred":"后续周安排","Needs confirmation":"安排需确认"}
            lines.append("- 安排状态："+labels.get(props["Preparation Status"],props["Preparation Status"]))
        if generated.get("merged_into"):
            lines.append("- 后续准备：[[record:"+generated["merged_into"]+"]]")
        for linked in generated.get("linked_task_keys",[]):
            lines.append("- 相关独立任务：[[record:"+linked+"]]")
        if generated.get("carried_from"):
            lines.append("- 本次回顾一并覆盖此前尚未完成的常规回顾。")
        if generated.get("preserved_source_content"):
            lines.append(generated["preserved_source_content"])
        steps = record.get("study_steps") or generated.get("study_steps")
        if isinstance(steps, (list, tuple)) and steps:
            lines.append("### 这一项怎么准备")
            for step in steps:
                if isinstance(step, Mapping):
                    lines.append("- " + str(step.get("title", step.get("text", "学习步骤"))))
                    if step.get("completion_criteria"):
                        lines.append("  - 完成标准：" + str(step["completion_criteria"]))
                    if step.get("estimated_minutes") is not None:
                        lines.append("  - 建议预留约 " + str(step["estimated_minutes"]) + " 分钟；这是学习估计。")
        observations = record.get("observations") or generated.get("observations")
        if isinstance(observations, (list, tuple)) and observations:
            lines.append("### 已收到的进度证据")
            for observation in observations:
                if isinstance(observation, Mapping) and observation.get("summary"):
                    lines.append("- " + str(observation["summary"]))
        if props.get("Due Conflict") == YES:
            choices = props.get("Due Choices")
            rendered_choices = _render_item(choices) if choices else "待确认"
            lines.append(f"- 日期冲突：{rendered_choices}")
        actions = generated.get("reviewed_actions")
        if isinstance(actions, (list, tuple)) and actions:
            lines.append("### 已审核行动项")
            for item in actions:
                rendered_item = _render_item(item)
                if rendered_item:
                    lines.append(f"- {rendered_item}")
        action_sources = generated.get("reviewed_action_sources")
        if isinstance(action_sources, (list, tuple)) and action_sources:
            for item in action_sources:
                if isinstance(item, Mapping):
                    lines.extend(_render_reviewed_provenance(item))
        reviewed_action = _render_marker(generated.get("reviewed_action"))
        if reviewed_action:
            lines.extend(["### 行动项", f"- {reviewed_action}"])
            lines.extend(_render_reviewed_provenance(generated))
        original = _render_marker(generated.get("assignment_original"))
        if original:
            lines.extend(
                [
                    _render_original_heading(
                        "作业原文",
                        truncated=generated.get("assignment_original_truncated") == YES,
                    ),
                    original,
                ]
            )
    elif kind == "announcements":
        summary = _render_marker(generated.get("summary"))
        if summary:
            lines.extend(["### 公告摘要", summary])
        action_items = generated.get("action_items")
        if isinstance(action_items, (list, tuple)) and action_items:
            lines.append("### 行动项")
            for item in action_items:
                rendered_item = _render_item(item)
                if rendered_item:
                    lines.append(f"- [ ] {rendered_item}")
    elif kind == "notes":
        summary = _render_marker(generated.get("syllabus_summary"))
        if summary:
            lines.extend(
                [
                    _render_original_heading(
                        "大纲摘要",
                        truncated=generated.get("syllabus_summary_truncated") == YES,
                    ),
                    summary,
                ]
            )
        important = generated.get("important_info")
        if isinstance(important, (list, tuple)) and important:
            lines.append(
                _render_original_heading(
                    "重要信息",
                    truncated=generated.get("important_info_truncated") == YES,
                )
            )
            for item in important:
                rendered_item = _render_item(item)
                if rendered_item:
                    lines.append(f"- {rendered_item}")
    elif kind == "resources":
        if name:
            lines.extend(["### 资源资料", name])
        lines = _render_resource_source(lines, generated)
    elif kind == "timetable":
        if name:
            lines.extend(["### 日程", name])
        start = _render_date(props, "Start", display_timezone)
        end = _render_date(props, "End", display_timezone)
        if start and end:
            lines.append(f"时间：{start} – {end}")
        elif start:
            lines.append(f"时间：{start}")
        elif end:
            lines.append(f"结束：{end}")
        location = _render_marker(generated.get("location_name"))
        if location:
            lines.append(f"地点：{location}")
        lines.extend(_render_reviewed_provenance(generated))
        description = _render_marker(generated.get("description_original"))
        if description:
            lines.extend(
                [
                    _render_original_heading(
                        "日程原文",
                        truncated=generated.get("description_original_truncated") == YES,
                    ),
                    description,
                ]
            )
    else:
        # Future kinds should remain readable without exposing debug keys.
        for key in sorted(generated, key=str):
            value = generated[key]
            rendered_value = _render_item(value)
            if rendered_value:
                lines.extend([f"### {key}", rendered_value])

    # Source links are kept outside the bounded body so a long review packet
    # cannot hide the provenance needed to inspect the source.  Item-specific
    # reviewed-action/event links are treated the same way as the record's
    # primary Source URL.
    source_lines: list[str] = []
    body_lines: list[str] = []
    for line in lines:
        if line is not None and (line.startswith("来源：") or line.startswith("- 来源链接：")):
            if line not in source_lines:
                source_lines.append(line)
        else:
            body_lines.append(line)
    source_link = _render_source_link(props)
    if source_link and source_link not in source_lines:
        source_lines.append(source_link)
    body = "\n".join(line for line in body_lines if line is not None).strip()
    render_limit = MAX_RENDERED_TEXT_BY_KIND.get(kind, MAX_GENERATED_TEXT)
    if len(body) > render_limit:
        bounded = _bounded_text(body, limit=render_limit)
        body = "\n".join(
            part
            for part in (
                bounded or "",
                "> 原文过长，以上为节选；请通过来源链接查看完整内容。",
            )
            if part
        )
    rendered_parts = [part for part in (body, "\n".join(source_lines)) if part]
    return "\n\n".join(rendered_parts).strip()


def tasks_to_ics(plan: Mapping[str, Any], *, calendar_name: str = "Canvas Tasks") -> str:
    """Export unambiguous task due dates as deterministic RFC 5545 text.

    Tasks without ``date:Due:start`` or with ``Due Conflict=__YES__`` are skipped because
    there is no honest calendar date to export.  The fixed DTSTAMP makes the
    result reproducible across runs.
    """

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Canvas Notion Study//EN",
        f"X-WR-CALNAME:{_ics_escape(calendar_name)}",
    ]
    records = [
        record
        for record in _mapping(plan).get("records", [])
        if isinstance(record, Mapping) and record.get("kind") == "tasks"
    ]
    for record in sorted(records, key=lambda item: str(item.get("source_key", ""))):
        props = _mapping(record.get("properties"))
        scope = str(props.get("Scope", "")).lower()
        if scope in {"historical", "reference", "informational", "out_of_scope"}:
            continue
        due = _text(props.get("date:Due:start"))
        conflict = props.get("Due Conflict") == YES
        if not due or conflict:
            continue
        start, all_day = _ics_datetime(due, _text(record.get("display_timezone")))
        if start is None:
            continue
        source = _text(props.get("Source Key")) or _text(record.get("source_key")) or "task"
        lines.extend(["BEGIN:VEVENT", f"UID:{_ics_escape(source)}@canvas-notion-study", "DTSTAMP:19700101T000000Z"])
        if all_day:
            lines.append(f"DTSTART;VALUE=DATE:{start}")
            try:
                next_day = date.fromisoformat(due).toordinal() + 1
                lines.append(f"DTEND;VALUE=DATE:{date.fromordinal(next_day).strftime('%Y%m%d')}")
            except ValueError:
                pass
        else:
            lines.append(f"DTSTART:{start}")
        title = _text(props.get('Name')) or 'Canvas task'
        if scope == "optional":
            title = "[可选] " + title
        lines.append(f"SUMMARY:{_ics_escape(title)}")
        description = [f"Source Key: {source}"]
        source_url = _text(props.get("Source URL"))
        if source_url:
            description.append(f"Source URL: {source_url}")
        lines.append(f"DESCRIPTION:{_ics_escape('\\n'.join(description))}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def export_tasks_ics(plan: Mapping[str, Any], *, calendar_name: str = "Canvas Tasks") -> str:
    """Compatibility alias for callers that prefer an export-style name."""

    return tasks_to_ics(plan, calendar_name=calendar_name)


__all__ = [
    "DATABASE_KINDS",
    "DATABASE_SCHEMAS",
    "MAX_GENERATED_TEXT",
    "MAX_RENDERED_TEXT_BY_KIND",
    "NO",
    "PENDING_MARKER",
    "PROJECTION_SCHEMA_VERSION",
    "YES",
    "build_plan",
    "export_tasks_ics",
    "render_generated_content",
    "source_key",
    "tasks_to_ics",
]

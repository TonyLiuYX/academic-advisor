"""Pure Gmail import/review helpers; the host owns authenticated connector I/O."""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import json
import quopri
import re
from urllib.parse import quote

from .state import digest


CLASSIFICATIONS = {"academic", "academic_admin", "optional", "out_of_scope", "non_actionable"}
HEADER_NAMES = {"subject", "from", "to", "cc", "date", "message-id", "in-reply-to", "references", "reply-to", "content-type", "content-transfer-encoding"}


def _list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _now(value=None):
    if isinstance(value, datetime):
        result = value
    elif value:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    else:
        result = datetime.now(timezone.utc)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _iso(value):
    return _now(value).isoformat().replace("+00:00", "Z")


def _words(value):
    try:
        return str(make_header(decode_header(str(value or ""))))
    except (LookupError, UnicodeError):
        return str(value or "")


def _b64(value):
    value = str(value or "").encode("ascii")
    return base64.urlsafe_b64decode(value + b"=" * (-len(value) % 4))


def _decode(data, charset="utf-8"):
    try:
        return data.decode(charset or "utf-8")
    except (LookupError, UnicodeDecodeError):
        return data.decode("utf-8", errors="replace")


def _headers(values):
    if isinstance(values, dict):
        values = [{"name": k, "value": v} for k, v in values.items()]
    result = {}
    for value in values or []:
        name = str(value.get("name", "")).lower()
        if name in HEADER_NAMES:
            decoded = _words(value.get("value"))
            result[name] = (result[name] + ", " + decoded) if name in result else decoded
    return result


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
        if tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, value):
        if not self.skip:
            self.parts.append(value)


def _plain(value):
    parser = _Text()
    parser.feed(value or "")
    parser.close()
    return re.sub(r"\n[ \t]*\n+", "\n\n", "".join(parser.parts)).strip()


def _addresses(value):
    return list(dict.fromkeys(address.lower() for _, address in getaddresses([_words(value)]) if address))


def _forwarded(text):
    found = []
    for line in text.splitlines():
        match = re.match(r"^\s*(?:[>│]\s*)*(?:To|Cc|收件人|抄送)\s*[:：]\s*(.+)$", line, re.I)
        if match:
            found.extend(_addresses(match.group(1)))
    return list(dict.fromkeys(found))


def build_queries(config, state=None, now=None):
    gmail = config.get("gmail", {})
    current = _now(now)
    previous = (state or {}).get("last_successful_fetch_at") or (state or {}).get("last_successful_fetch")
    if previous:
        start = _now(previous) - timedelta(hours=72)
        basis = "last_successful_fetch_minus_72h"
    elif gmail.get("start_date"):
        start, basis = _now(gmail["start_date"]), "configured_start_date"
    else:
        raise ValueError("gmail.start_date is required before the first completed fetch")
    recipients = _list(gmail.get("recipients") or gmail.get("school_addresses") or gmail.get("recipient"))
    domains = _list(gmail.get("school_domains"))
    senders = _list(gmail.get("canvas_senders"))
    courses = _list(gmail.get("course_codes") or config.get("course_codes"))
    window = f"after:{int(start.timestamp())} before:{int(current.timestamp()) + 1}"
    groups = [
        ("recipients", [term for address in recipients for term in ("to:" + str(address), "cc:" + str(address))]),
        ("senders", ["from:" + str(domain).lstrip("@") for domain in domains] + ["from:" + str(sender) for sender in senders]),
        ("courses", ['"' + str(code).replace('"', "") + '"' for code in courses]),
    ]
    return {"schema_version": 1, "window_start": _iso(start), "window_end": _iso(current), "window_basis": basis,
            "queries": [{"id": key, "query": window + " {" + " ".join(dict.fromkeys(terms)) + "}"} for key, terms in groups if terms],
            "fetch_order": ["all_search_pages", "deduplicate_metadata", "uncached_full_messages"],
            "pagination": {"must_follow_next_cursor": True, "complete_only_when_all_cursors_exhausted": True}}


def _mime_payload(part, plain, rich, attachments, errors, forwarded, path="0"):
    headers = _headers(part.get("headers", []))
    mime = str(part.get("mimeType") or part.get("mime_type") or headers.get("content-type", "text/plain").split(";")[0]).lower()
    filename = _words(part.get("filename"))
    body = part.get("body") or {}
    data = b""
    encoded = body.get("data") or body.get("base64_url_content")
    if encoded:
        try:
            data = _b64(encoded)
        except (ValueError, UnicodeError) as error:
            errors.append({"part_id": part.get("partId") or part.get("part_id") or path, "code": "body_decode_failed", "reason": type(error).__name__})
    elif body.get("content") is not None:
        data = str(body["content"]).encode("utf-8")
    attachment_id = body.get("attachmentId") or body.get("attachment_id")
    attachment = bool(filename or attachment_id)
    if attachment:
        record = {"part_id": str(part.get("partId") or part.get("part_id") or path), "attachment_id": attachment_id, "filename": filename or "attachment",
                  "mime_type": mime, "size": body.get("size", len(data)), "local_path": part.get("local_path") or body.get("local_path"),
                  "read_attachment_supported": part.get("read_attachment_supported")}
        if data:
            record["sha256"] = hashlib.sha256(data).hexdigest()
        attachments.append(record)
    if mime.startswith("text/") and data and not filename:
        charset_match = re.search(r"charset\s*=\s*[\"']?([^;\"'\s]+)", headers.get("content-type", ""), re.I)
        charset = charset_match.group(1) if charset_match else "utf-8"
        if body.get("content") is not None and not encoded:
            charset = "utf-8"
        elif headers.get("content-transfer-encoding", "").lower() == "quoted-printable":
            data = quopri.decodestring(data)
        text = _decode(data, charset)
        (rich if mime == "text/html" else plain).append(text)
    elif mime == "message/rfc822" and data:
        _mime_raw(data, plain, rich, attachments, errors, forwarded, path)
    for key in ("to", "cc"):
        forwarded.extend(_addresses(headers.get(key, "")))
    for index, child in enumerate(part.get("parts") or []):
        _mime_payload(child, plain, rich, attachments, errors, forwarded, path + "." + str(index))


def _mime_raw(raw, plain, rich, attachments, errors, forwarded, prefix="raw"):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    for index, part in enumerate(message.walk()):
        for key in ("to", "cc"):
            forwarded.extend(_addresses(part.get(key, "")))
        if part.is_multipart():
            continue
        data = part.get_payload(decode=True) or b""
        filename, mime = _words(part.get_filename()), part.get_content_type()
        if filename or part.get_content_disposition() == "attachment":
            attachments.append({"part_id": prefix + "." + str(index), "attachment_id": None, "filename": filename or "attachment",
                                "mime_type": mime, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "local_path": None})
        elif mime.startswith("text/"):
            (rich if mime == "text/html" else plain).append(_decode(data, part.get_content_charset()))
    return _headers(dict(message.items()))


def _message(payload):
    wrapper = payload if isinstance(payload, dict) else {}
    message = wrapper.get("message", wrapper)
    candidate = wrapper.get("candidate") or {}
    gmail_id = str(message.get("id") or message.get("gmail_id") or candidate.get("id") or candidate.get("gmail_id") or "")
    if not gmail_id:
        raise ValueError("A Gmail message id is required")
    plain, rich, attachments, errors, forwarded = [], [], [], [], []
    headers = _headers((message.get("payload") or {}).get("headers") or message.get("headers"))
    full = bool("raw" in message or "payload" in message and ((message.get("payload") or {}).get("parts") or (message.get("payload") or {}).get("body")) or "body_text" in message or "body_html" in message)
    if message.get("raw"):
        headers = _mime_raw(_b64(message["raw"]), plain, rich, attachments, errors, forwarded)
    elif message.get("payload"):
        _mime_payload(message["payload"], plain, rich, attachments, errors, forwarded)
    elif full:
        plain.append(str(message.get("body_text") or ""))
        rich.append(str(message.get("body_html") or ""))
        attachments.extend(deepcopy(message.get("attachments") or []))
    text, markup = "\n\n".join(dict.fromkeys(filter(None, plain))).strip(), "\n\n".join(dict.fromkeys(filter(None, rich))).strip()
    if not text and markup:
        text = _plain(markup)
    recipients = _addresses(headers.get("to", ""))
    cc = _addresses(headers.get("cc", ""))
    forwarded = list(dict.fromkeys(forwarded + _forwarded(text)))
    result = {"gmail_id": gmail_id, "thread_id": str(message.get("threadId") or message.get("thread_id") or candidate.get("threadId") or ""),
              "header_id": headers.get("message-id", "").strip(), "source_key": "gmail:message:" + gmail_id,
              "source_url": "https://mail.google.com/mail/u/0/#all/" + quote(gmail_id, safe=""),
              "subject": headers.get("subject") or message.get("subject") or candidate.get("subject") or "",
              "from": headers.get("from", message.get("from", "")), "to": recipients, "cc": cc,
              "date": headers.get("date", message.get("date", "")), "internal_date": message.get("internalDate") or message.get("internal_date"),
              "headers": headers, "forwarded_recipients": [a for a in forwarded if a not in recipients + cc],
              "body_text": text, "body_html": markup, "attachments": attachments,
              "snippet": message.get("snippet", ""), "fetched_full": full, "fetched_at": wrapper.get("fetched_at") or message.get("fetched_at"),
              "label_ids": message.get("labelIds", []), "decode_warnings": errors}
    content = {k: result[k] for k in ("gmail_id", "thread_id", "header_id", "subject", "from", "to", "cc", "date", "body_text", "body_html")}
    content["attachments"] = [{k: v for k, v in a.items() if k != "local_path"} for a in attachments]
    result["source_hash"] = digest(content)
    return result


def ingest_messages(existing, payloads, search_manifest=None):
    old = (existing or {}).get("messages", [])
    old = list(old.values()) if isinstance(old, dict) else old
    messages = {str(m["gmail_id"]): deepcopy(m) for m in old}
    warnings = []
    for payload in payloads or []:
        incoming = _message(payload)
        prior = messages.get(incoming["gmail_id"])
        if prior and prior.get("fetched_full") and not incoming["fetched_full"]:
            for field in ("label_ids", "fetched_at"):
                if incoming.get(field):
                    prior[field] = incoming[field]
        else:
            if prior:
                local = {(a.get("attachment_id"), a.get("part_id")): a.get("local_path") for a in prior.get("attachments", []) if a.get("local_path")}
                for attachment in incoming["attachments"]:
                    attachment["local_path"] = attachment.get("local_path") or local.get((attachment.get("attachment_id"), attachment.get("part_id")))
            messages[incoming["gmail_id"]] = incoming
        warnings.extend(incoming["decode_warnings"])
    manifest = deepcopy(search_manifest if search_manifest is not None else (existing or {}).get("search_manifest", {}))
    complete = manifest.get("complete") is True
    queries = manifest.get("queries") or manifest.get("searches") or []
    candidates = manifest.get("candidates") or [item for query in queries if isinstance(query, dict) for item in (query.get("response") or {}).get("emails", [])]
    excluded_ids = {str(item.get("id") or item.get("gmail_id")) if isinstance(item, dict) else str(item) for item in manifest.get("excluded") or []}
    for candidate in candidates:
        candidate = {"id": candidate} if isinstance(candidate, str) else candidate
        key = str(candidate.get("id") or candidate.get("gmail_id") or "")
        if key and key not in excluded_ids and key not in messages:
            messages[key] = _message(candidate)
    for query in queries if isinstance(queries, list) else []:
        response = query.get("response") or {}
        if query.get("next_cursor") or query.get("nextPageToken") or query.get("next_page_token") or response.get("next_page_token") or query.get("complete") is False:
            complete = False
        pages = query.get("pages") or []
        if pages and (pages[-1].get("next_cursor") or pages[-1].get("next_page_token") or pages[-1].get("nextPageToken")):
            complete = False
    queue = [key for key, value in messages.items() if not value.get("fetched_full")]
    result = {"schema_version": 1, "messages": sorted(messages.values(), key=lambda m: m["gmail_id"]), "search_manifest": manifest,
              "fetch_queue": queue, "warnings": warnings, "stats": {"messages": len(messages), "full_messages": sum(m.get("fetched_full", False) for m in messages.values()), "pending_full": len(queue), "search_complete": complete,
              "candidates": len(candidates or messages), "excluded": len(excluded_ids)}}
    successful = (existing or {}).get("last_successful_fetch_at")
    if complete and not queue and not warnings:
        successful = manifest.get("queried_at") or manifest.get("completed_at") or manifest.get("fetched_at") or max((m.get("fetched_at") or "" for m in messages.values()), default="") or successful
    if successful:
        result["last_successful_fetch_at"] = successful
    return result


def review_packet(snapshot):
    messages = []
    for message in snapshot.get("messages", []):
        if not message.get("fetched_full"):
            continue
        messages.append({k: deepcopy(message.get(k)) for k in ("gmail_id", "thread_id", "header_id", "source_key", "source_url", "source_hash", "subject", "from", "to", "cc", "date", "forwarded_recipients", "body_text", "body_html", "attachments", "decode_warnings")})
    return {"schema_version": 1, "messages": messages, "source_hash": digest([[m["gmail_id"], m["source_hash"]] for m in messages]), "pending_full": snapshot.get("fetch_queue", [])}


def _review_items(reviews):
    if isinstance(reviews, list):
        return reviews
    values = (reviews or {}).get("reviews", reviews or {})
    return [dict(value, gmail_id=value.get("gmail_id", key)) for key, value in values.items()] if isinstance(values, dict) else values


def validate_reviews(snapshot, reviews):
    messages = {m["gmail_id"]: m for m in snapshot.get("messages", [])}
    accepted, warnings, seen = [], [], set()
    for review in _review_items(reviews):
        gmail_id = str(review.get("gmail_id", ""))
        message = messages.get(gmail_id)
        code = None
        if gmail_id in seen:
            code = "duplicate_review"
        elif not message or not message.get("fetched_full"):
            code = "message_not_fully_fetched"
        elif review.get("source_hash") != message.get("source_hash"):
            code = "source_hash_mismatch"
        elif review.get("classification") not in CLASSIFICATIONS:
            code = "invalid_classification"
        elif not str(review.get("reason", "")).strip():
            code = "review_reason_missing"
        actions = review.get("actions") or []
        action_ids = set()
        for action in actions:
            if code:
                break
            if not action.get("id") or str(action["id"]) in action_ids:
                code = "action_id_missing_or_duplicate"
            elif not action.get("title") or not action.get("source_refs"):
                code = "action_title_or_source_missing"
            elif message["source_key"] not in action["source_refs"]:
                code = "action_message_source_missing"
            elif action.get("status", "required") not in {"required", "optional"}:
                code = "invalid_action_status"
            elif action.get("due"):
                try:
                    _now(action["due"])
                except (ValueError, TypeError):
                    code = "invalid_action_due"
            action_ids.add(str(action.get("id")))
        if review.get("classification") in {"out_of_scope", "non_actionable"} and actions:
            code = code or "excluded_review_has_actions"
        for resource in review.get("resources") or []:
            if not resource.get("id") or not resource.get("title") or not resource.get("source_refs"):
                code = code or "resource_id_title_or_source_missing"
        if review.get("notion_visibility") == "local_only" and not review.get("visibility_reason"):
            code = code or "local_only_reason_missing"
        if code:
            warnings.append({"gmail_id": gmail_id, "code": code})
        else:
            seen.add(gmail_id)
            accepted.append(deepcopy(review))
    for gmail_id, message in messages.items():
        if message.get("fetched_full") and gmail_id not in seen:
            warnings.append({"gmail_id": gmail_id, "code": "review_pending"})
    return {"schema_version": 1, "reviews": accepted, "legacy_scope_overrides": deepcopy(reviews.get("legacy_scope_overrides", [])) if isinstance(reviews, dict) else []}, warnings


def _record(kind, key, properties, generated, course_key=None, refs=None, requirements=None):
    result = {"source_key": key, "kind": kind, "course_key": course_key, "properties": {"Source Key": key, **properties},
              "generated_content": generated, "source_refs": refs or [], "requirement_ids": requirements or []}
    result["fingerprint"] = digest(result)
    return result


def _due_comparison(left, right):
    """Return agreement and the more precise source value, never invent a time."""
    left_date, right_date = "T" not in str(left) and " " not in str(left), "T" not in str(right) and " " not in str(right)
    if left_date or right_date:
        agree = str(left)[:10] == str(right)[:10]
        return agree, right if left_date and not right_date else left
    first = datetime.fromisoformat(str(left).replace("Z", "+00:00"))
    second = datetime.fromisoformat(str(right).replace("Z", "+00:00"))
    if (first.tzinfo is None) != (second.tzinfo is None):
        return False, left
    return first == second, left


def _merge_record(existing, incoming):
    result = deepcopy(existing)
    for name in ("source_refs", "requirement_ids"):
        result[name] = list(dict.fromkeys(_list(existing.get(name)) + _list(incoming.get(name))))
    props, extra = result.setdefault("properties", {}), incoming.get("properties", {})
    old_scope, new_scope = props.get("Scope"), extra.get("Scope")
    if new_scope:
        if old_scope and old_scope != new_scope and "Optional" in {old_scope, new_scope}:
            result.setdefault("merge_warnings", []).append({"code": "scope_conflict", "source_key": result["source_key"], "scopes": [old_scope, new_scope]})
            props["Scope"] = new_scope if old_scope == "Optional" else old_scope
        else:
            props["Scope"] = new_scope
    for name in ("Term", "Evidence Status"):
        if extra.get(name):
            props[name] = extra[name]
    sources = str(props.get("Source") or "").splitlines() + str(extra.get("Source") or "").splitlines()
    props["Source"] = "\n".join(dict.fromkeys(filter(None, sources)))
    resources = _list(props.get("Resources")) + _list(extra.get("Resources"))
    if resources:
        props["Resources"] = list(dict.fromkeys(resources))
    if extra.get("Due Conflict") == "__YES__":
        props["Due Conflict"] = "__YES__"
        props["Due Choices"] = list(dict.fromkeys(_list(props.get("Due Choices")) + _list(extra.get("Due Choices"))))
    old_due, new_due = props.get("date:Due:start") or props.get("Due"), extra.get("date:Due:start") or extra.get("Due")
    if old_due and new_due:
        agree, precise = _due_comparison(old_due, new_due)
        if not agree:
            props["Due Conflict"] = "__YES__"
            props["Due Choices"] = list(dict.fromkeys(_list(props.get("Due Choices")) + [old_due, new_due]))
        else:
            props["date:Due:start"] = precise
            props["date:Due:is_datetime"] = int("T" in str(precise))
    elif not old_due and new_due:
        props["date:Due:start"] = new_due
        props["date:Due:is_datetime"] = int("T" in str(new_due))
    generated = result.get("generated_content")
    if not isinstance(generated, dict):
        generated = {"previous_content": generated}
    generated.setdefault("reviewed_actions", [])
    action = incoming.get("generated_content", {}).get("reviewed_action")
    if action and action not in generated["reviewed_actions"]:
        generated["reviewed_actions"].append(action)
    if incoming.get("generated_content", {}).get("summary"):
        summaries = generated.setdefault("email_summaries", [])
        value = {"summary": incoming["generated_content"]["summary"], "source_refs": incoming.get("source_refs", [])}
        if value not in summaries:
            summaries.append(value)
    generated["requirement_ids"] = result["requirement_ids"]
    result["generated_content"] = generated
    result.pop("fingerprint", None)
    result["fingerprint"] = digest(result)
    return result


def build_supplement(snapshot, reviews, canvas_plan, legacy=None):
    accepted, warnings = validate_reviews(snapshot, reviews)
    messages = {m["gmail_id"]: m for m in snapshot.get("messages", [])}
    canvas = {r["source_key"]: r for r in canvas_plan.get("records", [])}
    old_records = (legacy or {}).get("records", []) if isinstance(legacy, dict) else legacy or []
    records = {r["source_key"]: deepcopy(r) for r in old_records}
    overrides, requirements, observations = {}, [], []
    term = canvas_plan.get("term") or {}
    term = (term.get("key") or term.get("label")) if isinstance(term, dict) else str(term)
    alias_map = {}
    for key, record in records.items():
        for alias in _list(record.get("source_aliases")):
            alias_map[str(alias)] = key
    scopes = {"academic": "Academic", "academic_admin": "Academic Admin", "optional": "Optional", "out_of_scope": "Historical", "non_actionable": "Academic"}
    for review in accepted["reviews"]:
        message = messages[review["gmail_id"]]
        source, classification = message["source_key"], review["classification"]
        refs = list(dict.fromkeys([source] + _list(review.get("supporting_source_refs"))))
        course_key = review.get("course_key")
        observations.extend(deepcopy(review.get("observations") or []))
        related = [r for r in records.values() if source in _list(r.get("source_refs")) or source in str(r.get("properties", {}).get("Source", "")).splitlines() or message["gmail_id"] in str(r.get("properties", {}).get("Source URL", ""))]
        if classification == "out_of_scope":
            for record in related:
                record.setdefault("properties", {})["Scope"] = "Historical"
                if not isinstance(record.get("generated_content"), dict):
                    record["generated_content"] = {"previous_content": record.get("generated_content")}
                record["generated_content"]["scope_reason"] = review["reason"]
            requirements.append({"id": source + ":review", "source_refs": refs, "task_keys": [], "status": "non_actionable", "reason": review["reason"]})
            continue
        announcement_key = review.get("canonical_announcement_key") or next((r["source_key"] for r in related if r.get("kind") == "announcements"), source)
        if review.get("notion_visibility") != "local_only":
            properties = {"Name": message["subject"] or "邮件通知", "Source URL": message["source_url"], "Source": "\n".join(refs), "Course": [course_key] if course_key else [], "Type": "Email", "Scope": scopes[classification], "Term": term, "Sync Status": "reviewed", "Has Action Items": "__YES__" if review.get("actions") else "__NO__"}
            try:
                stamp = parsedate_to_datetime(message["date"])
                properties.update({"date:Posted:start": stamp.isoformat(), "date:Posted:is_datetime": 1})
            except (ValueError, TypeError, IndexError):
                if message.get("internal_date"):
                    properties.update({"date:Posted:start": datetime.fromtimestamp(int(message["internal_date"]) / 1000, timezone.utc).isoformat(), "date:Posted:is_datetime": 1})
            announcement = _record("announcements", announcement_key, properties,
                {"summary": review.get("summary") or review["reason"], "action_items": [a["title"] for a in review.get("actions", [])], "source_hash": message["source_hash"], "source_refs": refs, "observations": review.get("observations", [])}, course_key, refs)
            if announcement_key in canvas:
                overrides[announcement_key] = _merge_record(overrides.get(announcement_key, canvas[announcement_key]), announcement)
            elif announcement_key in records:
                records[announcement_key] = _merge_record(records[announcement_key], announcement)
            elif review.get("canonical_announcement_key") and not announcement_key.startswith("gmail:"):
                warnings.append({"gmail_id": message["gmail_id"], "code": "canonical_announcement_missing", "source_key": announcement_key})
            else:
                records[announcement_key] = announcement
        for resource in review.get("resources") or []:
            key = resource.get("canonical_resource_key") or "gmail:resource:" + message["gmail_id"] + ":" + str(resource["id"])
            incoming = _record("resources", key, {"Name": resource["title"], "Source URL": resource.get("url") or message["source_url"], "Source": "\n".join(resource.get("source_refs") or refs), "Term": term, "Course": [course_key] if course_key else [], "Type": resource.get("type", "external_link"), "Scope": scopes[classification], "Download Status": "downloaded" if resource.get("local_path") else "link_only", "Local Path": resource.get("local_path"), "SHA256": resource.get("sha256")},
                               {"summary": resource.get("reason") or review["reason"], "source_refs": resource.get("source_refs") or refs}, course_key, resource.get("source_refs") or refs)
            if key in canvas:
                overrides[key] = _merge_record(overrides.get(key, canvas[key]), incoming)
            else:
                records[key] = _merge_record(records[key], incoming) if key in records else incoming
        actions = review.get("actions") or []
        if not actions:
            requirements.append({"id": source + ":review", "source_refs": refs, "task_keys": [], "status": "non_actionable" if classification != "optional" else "optional", "reason": review["reason"]})
        for action in actions:
            new_key = "gmail:task:" + message["gmail_id"] + ":" + str(action["id"])
            aliases = list(dict.fromkeys(_list(action.get("source_aliases")) + [new_key]))
            canonical = action.get("canonical_task_key")
            key = canonical or next((alias_map[a] for a in aliases if a in alias_map), None) or next((a for a in aliases if a in records), None) or new_key
            if canonical and canonical not in canvas and canonical not in records and not str(canonical).startswith("gmail:"):
                warnings.append({"gmail_id": message["gmail_id"], "action_id": action["id"], "code": "canonical_task_missing", "task_key": canonical})
                continue
            requirement_id = source + ":action:" + str(action["id"])
            status = action.get("status", "optional" if classification == "optional" else "required")
            props = {"Name": action["title"], "Source URL": message["source_url"], "Source": "\n".join(action["source_refs"]), "Course": [course_key] if course_key else [], "Term": term, "Type": action.get("type", "Email action"), "Scope": "Optional" if status == "optional" else scopes[classification], "Sync Status": "reviewed"}
            if action.get("evidence_status"):
                props["Evidence Status"] = action["evidence_status"]
            if action.get("due_conflict"):
                props["Due Conflict"] = "__YES__"
                props["Due Choices"] = action.get("due_choices", [])
            if action.get("due"):
                props.update({"date:Due:start": action["due"], "date:Due:is_datetime": int("T" in action["due"])})
            if action.get("resource_keys"):
                props["Resources"] = action["resource_keys"]
            generated = {"reviewed_action": action["title"], "source_ref": source, "source_hash": message["source_hash"], "completion_criteria": action.get("completion_criteria"), "requirement_ids": [requirement_id]}
            incoming = _record("tasks", key, props, generated, course_key, action["source_refs"], [requirement_id])
            incoming["source_aliases"] = aliases
            if key in canvas:
                overrides[key] = _merge_record(overrides.get(key, canvas[key]), incoming)
            elif key in records:
                records[key] = _merge_record(records[key], incoming)
                records[key]["source_aliases"] = list(dict.fromkeys(_list(records[key].get("source_aliases")) + aliases))
            else:
                records[key] = incoming
            for alias in aliases:
                alias_map[alias] = key
            requirements.append({"id": requirement_id, "source_refs": action["source_refs"], "task_keys": [key], "status": status, "reason": action.get("reason") or review["reason"]})
    for change in accepted.get("legacy_scope_overrides", []):
        key = change.get("source_key")
        if key in records and change.get("scope") in {"Historical", "Optional"} and change.get("reason"):
            records[key].setdefault("properties", {})["Scope"] = change["scope"]
            content = records[key].get("generated_content")
            if not isinstance(content, dict):
                content = {"previous_content": content}
            content["scope_reason"] = change["reason"]
            records[key]["generated_content"] = content
    for observation in observations:
        for key in observation.get("task_keys") or []:
            if key in overrides:
                target = overrides[key]
            elif key in records:
                target = records[key]
            elif key in canvas:
                target = overrides[key] = deepcopy(canvas[key])
            else:
                warnings.append({"code": "observation_task_missing", "task_key": key, "observation_id": observation.get("id")})
                continue
            entries = target.setdefault("observations", [])
            if observation not in entries:
                entries.append(deepcopy(observation))
            if observation.get("status") in {"confirmed_completed", "partial_completed"}:
                target.setdefault("properties", {})["Evidence Status"] = observation["status"]
    for record in list(records.values()) + list(overrides.values()):
        warnings.extend(record.pop("merge_warnings", []))
        record.pop("fingerprint", None)
        record["fingerprint"] = digest(record)
    return {"schema_version": 1, "records": list(records.values()), "requirements": requirements, "overrides": overrides, "observations": observations, "warnings": warnings,
            "stats": {"reviewed_messages": len(accepted["reviews"]), "supplement_records": len(records), "canvas_overrides": len(overrides), "requirements": len(requirements), "legacy_records_preserved": len(old_records)}}

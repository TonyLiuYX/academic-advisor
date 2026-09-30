"""Course notebooks: source-aware ingestion and a host-executed, recoverable outbox.

All durable identities and inflight operations share notion-state.json. The host
reads handwriting and executes Notion tools; this module never guesses text or
calls a cloud API. Published notebook bodies are not Canvas projections.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import re
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit, unquote
import json

from .state import begin_operations, digest, record_fingerprint

MAX_UPLOAD = 20 * 1024 * 1024
ASSET = re.compile(r"\[\[asset:([A-Za-z0-9_-]+)\]\]")


@contextmanager
def state_lock(directory):
    """Serialize short local transactions, never hold a lock during tool calls."""
    import fcntl
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".notion-state.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another local Notion state transaction is active; retry after it finishes.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def index(state):
    return state.setdefault("class_notes", {"schema_version": 1, "notebooks": {}, "batches": {}})


def page_url(page):
    return page if str(page).startswith("https://") else "https://www.notion.so/" + str(page).replace("-", "")


def marker(kind, identity):
    return f"〔{kind}：{identity}〕"


def anchor(book, evidence=False):
    label = "原稿" if evidence else "课堂笔记"
    number = book.get("evidence_version" if evidence else "entry_count", 0)
    return f"> {label}归档进度：{number}。"


def section(content, start, end):
    if content.count(start) != 1 or content.count(end) != 1:
        raise ValueError("Target section is missing or ambiguous; fetch complete content before continuing.")
    a, b = content.index(start), content.index(end)
    if b < a:
        raise ValueError("Section boundaries are out of order.")
    return content[a:b + len(end)]


def content_hash(content):
    """Notion refreshes signed asset URLs on every fetch; hash stable objects."""
    def stable(match):
        url = urlsplit(match[0])
        return urlunsplit((url.scheme, url.netloc, url.path, "", ""))
    canonical = re.sub(r'https://(?:prod-files-secure|s3)[^\s)"<>]*\.amazonaws\.com/[^\s)"<>]+', stable, content)
    return digest(canonical)


def _complete(readback, page_id=None):
    if not isinstance(readback, dict) or readback.get("truncated") is not False or readback.get("unknown_block_count") != 0:
        raise ValueError("A complete Notion readback (truncated=false, unknown_block_count=0) is required.")
    if not readback.get("page_id") or (page_id and readback["page_id"].replace("-", "") != page_id.replace("-", "")):
        raise ValueError("Readback page identity does not match the operation.")
    if not isinstance(readback.get("content"), str):
        raise ValueError("Readback must contain actual fetched page Markdown.")
    return readback["content"]


def _asset_markdown(asset):
    upload = asset.get("upload_id")
    remote = asset.get("remote_source")
    if remote:
        # Notion fetch returns native PDF attachment references in this form.
        # This is not a local file path and contains no signed download token.
        try:
            ref = json.loads(unquote(remote.removeprefix("file://")))
            valid = remote.startswith("file://%7B") and ref["source"].startswith("attachment:") and ref["permissionRecord"]["table"] == "block"
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise ValueError("Only a native Notion attachment reference from actual readback is accepted.")
        src = remote
    elif upload and re.fullmatch(r"[a-fA-F0-9-]{32,36}", upload):
        src = "file-upload://" + upload
    else:
        raise ValueError("Asset has no verified Notion upload identity.")
    if asset["suffix"] == ".pdf":
        return f'<pdf src="{src}">{asset["id"]}</pdf>'
    return f'![{asset["id"]}]({src})'


def _date(value, tz, now=None):
    if value == "today":
        current = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("--now must include a timezone.")
        return current.astimezone(ZoneInfo(tz)).date().isoformat()
    return date.fromisoformat(value).isoformat() if value else None


def notebook_record(book):
    props = {"Name": book["title"], "Source Key": book["key"], "Course": [book["course_key"]],
             "Term": book["term"], "Type": "Class notes"}
    props.update(book.get("obsidian", {}).get("properties", {}))
    return {"kind": "notes", "source_key": book["key"], "course_key": book["course_key"],
            "content_mode": "remote_append", "properties": props, "generated_content": ""}


def import_batch(state, plan, payload, directory, config, schedule=None, now=None):
    """Validate before creating cache; split source-attributed sections on dedup."""
    if config.get("notes", {}).get("backend") == "obsidian":
        raise ValueError("Use course-notes notebook append, then notes-index for Obsidian notebooks.")
    notes = index(state)
    courses = {r["source_key"]: r for r in plan.get("records", []) if r["kind"] == "courses"}
    course_key = payload.get("course_key")
    if course_key not in courses:
        raise ValueError("Choose an existing course_key; course identity must be resolved before import.")
    term = config["term"]["key"]
    key = course_key + "|notebook=" + term
    book = notes["notebooks"].get(key, {"key": key, "course_key": course_key, "term": term,
        "title": courses[course_key]["properties"]["Name"] + "｜课堂笔记", "assets": {}, "batch_ids": [],
        "entry_count": 0, "asset_count": 0, "evidence_version": 0})
    if key in state.get("inflight", {}):
        raise ValueError("Reconcile the current notebook operation before importing another batch.")
    day = _date(payload.get("date"), config["term"]["timezone"], now)
    occurrence = payload.get("occurrence_id")
    series_id = None
    class_info = None
    if occurrence:
        from .schedule import occurrences
        classes, _ = occurrences(schedule or {}, config["term"]["timezone"])
        matches = [c for c in classes if c["occurrence_id"] == occurrence and c["course_key"] == course_key]
        if len(matches) != 1 or matches[0].get("cancelled"):
            raise ValueError("occurrence_id must identify this course's confirmed, non-cancelled class.")
        if day and day != matches[0]["date"]:
            raise ValueError("Note date disagrees with the class's actual date.")
        day, series_id = matches[0]["date"], matches[0]["series_id"]
        meeting = matches[0]
        class_info = {"label": meeting.get("label") or meeting["kind"], "start": meeting.get("start"), "end": meeting.get("end")}
        for record in plan.get("records", []):
            if record.get("kind") == "tasks" and record.get("preparation", {}).get("trigger_id") == occurrence:
                remote = state.get("records", {}).get(record["source_key"], {}).get("page_id")
                if remote:
                    class_info["url"] = page_url(remote)
                    break
    revision = payload.get("revision_of")
    old_batch = notes["batches"].get(revision) if revision else None
    if revision and (not old_batch or old_batch["notebook_key"] != key or old_batch["status"] != "verified" or old_batch.get("superseded_by")):
        raise ValueError("revision_of must identify a verified batch in this notebook.")
    if revision:
        day, occurrence, series_id = old_batch["date"], old_batch.get("occurrence_id"), old_batch.get("series_id")
        class_info = deepcopy(old_batch.get("class_info"))
    sources = payload.get("sources", [])
    if not sources and not revision:
        raise ValueError("At least one ordered original source is required.")
    assets = deepcopy(old_batch["assets"]) if revision and not sources else []
    ids = {a["id"] for a in assets}
    for role, inputs in (("source", sources), ("figure", payload.get("figures", []))):
        for item in inputs:
            identity = item.get("id", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", identity) or identity in ids:
                raise ValueError("Source/figure IDs must be unique letters, numbers, hyphens or underscores.")
            ids.add(identity)
            path = Path(item["path"]).expanduser().resolve()
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".pdf"}:
                raise ValueError("Unsupported source; use a photo or scanned PDF.")
            data = path.read_bytes()
            if not data or len(data) > MAX_UPLOAD:
                raise ValueError("Empty or over-20-MiB source; prepare lossless smaller parts before importing.")
            assets.append({"id": identity, "role": role, "sha256": hashlib.sha256(data).hexdigest(),
                           "suffix": path.suffix.lower(), "pages": item.get("pages"), "input_path": str(path)})
    source_hashes = {a["id"]: a["sha256"] for a in assets if a["role"] == "source"}
    known = {h for bid in book["batch_ids"] for h in notes["batches"][bid]["source_hashes"]}
    duplicates = set()
    seen = set(known) if not revision else set()
    for identity, h in source_hashes.items():
        if h in seen and not revision:
            duplicates.add(identity)
        seen.add(h)
    new_ids = set(source_hashes) - duplicates
    if not new_ids and not revision:
        matches = [bid for bid in book["batch_ids"] if set(source_hashes.values()) & set(notes["batches"][bid]["source_hashes"])]
        return {"status": "duplicate", "batch_ids": matches, "notebook_key": key,
                "url": page_url(book["page_id"]) if book.get("page_id") else None}
    sections = payload.get("sections")
    if sections is None and isinstance(payload.get("markdown"), str):
        sections = [{"source_ids": list(source_hashes), "markdown": payload["markdown"]}]
    if not sections:
        raise ValueError("Provide source-attributed sections or markdown for the whole new batch.")
    kept, covered = [], set()
    for part in sections:
        refs = set(part.get("source_ids", []))
        if not refs or not refs <= set(source_hashes) or not str(part.get("markdown", "")).strip():
            raise ValueError("Every section needs known source_ids and non-empty faithful Markdown.")
        if refs <= duplicates:
            continue
        if refs & duplicates:
            raise ValueError("Partial overlap: split this section into old/new source-attributed sections; do not retranscribe duplicates.")
        if set(ASSET.findall(part["markdown"])) - ids:
            raise ValueError("Markdown contains an unknown asset reference.")
        if "file-upload://" in part["markdown"] or re.search(r"(?:file://|/Users/|X-Amz-|upload_headers)", part["markdown"]):
            raise ValueError("Use [[asset:ID]] placeholders, not private paths or upload URLs in authored text.")
        if "〔笔记：" in part["markdown"] or "〔笔记结束：" in part["markdown"]:
            raise ValueError("Authored text contains reserved notebook boundaries.")
        covered.update(refs)
        kept.append(part)
    if covered != new_ids:
        raise ValueError("Every new original source must be accounted for in the transcription.")
    reviews = [r for r in payload.get("review_items", []) if r.get("source_id") not in duplicates]
    if any(r.get("source_id") not in source_hashes or not r.get("message") for r in reviews):
        raise ValueError("Review items require source_id and a visible message.")
    used = set(ASSET.findall("\n".join(p["markdown"] for p in kept)))
    selected = [a for a in assets if a["id"] in new_ids or a["id"] in used or (revision and a["role"] == "source")]
    identity = digest([key, [a["sha256"] for a in selected], kept, revision, old_batch.get("body_hash") if old_batch else None,
                       day, occurrence, payload.get("topic", "课堂笔记"), reviews])[:20]
    if identity in notes["batches"]:
        return {"status": "existing", "batch_id": identity, "notebook_key": key}
    cache = Path(directory) / "notes-cache" / identity
    cache.mkdir(parents=True, exist_ok=True)
    cached = []
    for a in selected:
        a = dict(a)
        original = a.pop("input_path", None)
        if original and a["sha256"] not in book["assets"]:
            target = cache / (a["sha256"] + a["suffix"])
            shutil.copyfile(original, target)
            target.chmod(0o600)
            a["cache_path"] = str(target)
        cached.append(a)
    from .state import write_json
    write_json(cache / "draft.json", {"sections": kept})
    batch = {"id": identity, "notebook_key": key, "date": day, "occurrence_id": occurrence, "series_id": series_id,
             "topic": str(payload.get("topic") or "课堂笔记"), "class_info": class_info, "assets": cached, "review_items": reviews,
             "source_hashes": [a["sha256"] for a in cached if a["role"] == "source"], "revision_of": revision,
             "entry_id": old_batch["entry_id"] if old_batch else identity,
             "status": "prepared", "created_at": datetime.now(timezone.utc).isoformat()}
    notes["notebooks"][key] = book
    notes["batches"][identity] = batch
    book["batch_ids"].append(identity)
    return {"status": "prepared", "batch_id": identity, "notebook_key": key, "duplicate_source_ids": sorted(duplicates)}


def _draft(directory, batch):
    from .state import read_json
    return read_json(Path(directory) / "notes-cache" / batch["id"] / "draft.json")


def _body(book, batch, directory):
    by_id = {a["id"]: {**a, **book["assets"][a["sha256"]]} for a in batch["assets"]}
    def replace(match):
        return _asset_markdown(by_id[match[1]])
    content = "\n\n".join(ASSET.sub(replace, s["markdown"].strip()) for s in _draft(directory, batch)["sections"])
    title = (batch["date"] or "日期待确认") + " · " + batch["topic"]
    class_line = ""
    if batch.get("class_info"):
        info = batch["class_info"]
        class_line = "课节：" + info["label"]
        if info.get("start"):
            class_line += " · " + info["start"]
        if info.get("url"):
            class_line += f' · 对应安排：<mention-page url="{info["url"]}"/>'
        class_line += "\n\n"
    reviews = "\n\n".join("> 待确认（" + r["source_id"] + "）：" + r["message"] for r in batch["review_items"])
    return (marker("笔记", batch["entry_id"]) + "\n\n## " + title + "\n\n" + class_line + content +
            ("\n\n" + reviews if reviews else "") + f'\n\n原稿与复核：<mention-page url="{page_url(book["evidence_page_id"])}"/>\n\n' +
            marker("笔记结束", batch["entry_id"]))


def next_operation(state, config, directory, batch_id=None, readbacks=None, begin=False):
    if config.get("notes", {}).get("backend") == "obsidian" and not batch_id:
        from .obsidian_notes import index_operations
        return index_operations(state)
    notes = index(state)
    batches = [b for b in notes["batches"].values() if b["status"] != "verified" and (not batch_id or b["id"] == batch_id)]
    if batch_id and batch_id not in notes["batches"]:
        raise ValueError("Unknown batch_id")
    if not batches:
        return {"operations": [], "blocked": [], "complete": True}
    batch = batches[0]
    book = notes["notebooks"][batch["notebook_key"]]
    key = book["key"]
    active = state.get("inflight", {}).get(key)
    if active:
        return {"operations": [], "blocked": [{"reason": "inflight_requires_reconciliation", "operation": active}], "complete": False}
    pending = [b for b in book["batch_ids"] if notes["batches"][b]["status"] != "verified"]
    if pending[0] != batch["id"]:
        return {"operations": [], "blocked": [{"reason": "earlier_batch_pending", "batch_id": pending[0]}], "complete": False}
    op = {"source_key": key, "kind": "notes", "batch_id": batch["id"], "content_mode": "remote_append"}
    readbacks = readbacks or {}
    if not book.get("page_id"):
        binding = config.get("notion", {}).get("databases", {}).get("notes", {})
        course = state.get("records", {}).get(book["course_key"], {}).get("page_id")
        if not binding.get("data_source_id") or not course:
            raise ValueError("Bind Notes and publish the course before creating its notebook.")
        record = notebook_record(book)
        props = deepcopy(record["properties"]); props["Course"] = [course]
        content = "<table_of_contents/>\n\n" + marker("课程笔记", digest(key)[:20]) + "\n\n" + anchor(book) + "\n\n## 个人补充"
        op.update(notes_stage="notebook", action="create", properties=props, fingerprint=record_fingerprint(record),
                  payload={"parent": {"data_source_id": binding["data_source_id"]}, "pages": [{"properties": props, "content": content}]}, expected=content)
    elif not book.get("evidence_page_id"):
        content = marker("原稿库", digest(key)[:20]) + "\n\n" + anchor(book, True)
        op.update(notes_stage="evidence_page", action="create", payload={"parent": {"page_id": book["page_id"]},
                  "pages": [{"properties": {"title": "原稿与复核记录"}, "content": content}]}, expected=content)
    else:
        missing = next((a for a in batch["assets"] if a["sha256"] not in book["assets"]), None)
        evidence = bool(missing) or not batch.get("evidence_verified")
        page = book["evidence_page_id"] if evidence else book["page_id"]
        readback = readbacks.get("evidence" if evidence else "main")
        if readback is None:
            return {"operations": [], "blocked": [{"reason": "fetch_required", "role": "evidence" if evidence else "main", "page_id": page}], "complete": False}
        fetched = _complete(readback, page)
        op["page_id"] = page
        if missing:
            path = Path(missing["cache_path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != missing["sha256"]:
                raise ValueError("Cached asset changed after transcription.")
            old = anchor(book, True)
            if fetched.count(old) != 1:
                raise ValueError("Evidence append position changed; reconcile before writing.")
            new_book = {**book, "evidence_version": book["evidence_version"] + 1}
            start, end = marker("原稿", missing["sha256"]), marker("原稿结束", missing["sha256"])
            op.update(notes_stage="asset", action="attach", asset=missing, old_str=old, new_anchor=anchor(new_book, True),
                      start_marker=start, end_marker=end,
                      instructions="Create file upload, POST cached bytes once, replace old_str with start_marker + source metadata + suggested_markdown + end_marker + new_anchor. Fetch and verify before receipt.")
        elif not batch.get("evidence_verified"):
            old = anchor(book, True)
            if fetched.count(old) != 1:
                raise ValueError("Evidence append position changed; reconcile before writing.")
            details = [marker("批次", batch["id"]), "## " + (batch["date"] or "日期待确认") + " · " + batch["topic"]]
            details.extend(f'- {a["id"]} · 页码 {a.get("pages") or "见原稿"} · SHA-256 {a["sha256"]}' for a in batch["assets"])
            details.extend(f'> 待确认（{r["source_id"]}）：{r["message"]}' for r in batch["review_items"])
            details.append(marker("批次结束", batch["id"]))
            body = "\n\n".join(details)
            new_anchor = anchor({**book, "evidence_version": book["evidence_version"] + 1}, True)
            op.update(notes_stage="evidence_entry", action="append", expected=body, new_anchor=new_anchor,
                      payload={"page_id": page, "command": "update_content",
                               "content_updates": [{"old_str": old, "new_str": body + "\n\n" + new_anchor}]})
        else:
            body = _body(book, batch, directory)
            if batch.get("revision_of"):
                old_batch = notes["batches"][batch["revision_of"]]
                old = section(fetched, marker("笔记", batch["entry_id"]), marker("笔记结束", batch["entry_id"]))
                if content_hash(old) != old_batch["body_hash"]:
                    raise ValueError("This note section changed in Notion; preserve edits and resolve the conflict.")
                new = body
                op.update(notes_stage="revision", action="revise")
            else:
                old = anchor(book)
                if fetched.count(old) != 1 or marker("笔记", batch["entry_id"]) in fetched:
                    raise ValueError("Notebook append position changed or batch already exists; reconcile.")
                new = body + "\n\n" + anchor({**book, "entry_count": book["entry_count"] + 1})
                op.update(notes_stage="entry", action="append")
            op.update(expected=body, payload={"page_id": page, "command": "update_content",
                      "content_updates": [{"old_str": old, "new_str": new}]})
    op["operation_id"] = digest(op)
    if begin:
        begin_operations(state, [op])
    return {"operations": [op], "blocked": [], "complete": False, "begun": begin}


def commit_note_receipts(state, receipts):
    """Validate readback and atomically mutate state; caller persists before cleanup."""
    notes = index(state)
    cleanup = []
    for receipt in receipts:
        key = receipt["source_key"]
        op = state.get("inflight", {}).get(key)
        if not op or not op.get("notes_stage") or receipt.get("operation_id") != op["operation_id"]:
            raise ValueError("Receipt must match an inflight note operation.")
        if receipt.get("status") == "uploaded":
            if op["notes_stage"] != "asset":
                raise ValueError("Only an asset operation can checkpoint an upload.")
            upload = receipt.get("upload_id", "")
            if not re.fullmatch(r"[a-fA-F0-9-]{32,36}", upload):
                raise ValueError("Upload checkpoint requires the returned Notion upload_id.")
            if op.get("uploaded_id") and op["uploaded_id"] != upload:
                raise ValueError("Reconcile the previously checkpointed upload before replacing it.")
            # Byte upload is an intermediate state, never proof of attachment.
            op["uploaded_id"] = upload
            continue
        if receipt.get("status") == "definitely_not_applied":
            if receipt.get("confirmed_absent") is not True or not receipt.get("reason"):
                raise ValueError("Only a confirmed absent remote write can be retried.")
            state["inflight"].pop(key)
            continue
        if receipt.get("status") != "succeeded":
            continue
        rb = receipt.get("readback")
        fetched = _complete(rb, op.get("page_id"))
        if receipt.get("content_verified") is not True:
            raise ValueError("Compare actual content with the source/payload before recording success.")
        book, batch = notes["notebooks"][key], notes["batches"][op["batch_id"]]
        stage = op["notes_stage"]
        if stage in ("notebook", "evidence_page"):
            required = marker("课程笔记" if stage == "notebook" else "原稿库", digest(key)[:20])
            if fetched.count(required) != 1 or anchor(book, stage == "evidence_page") not in fetched:
                raise ValueError("Created page content is not verified.")
            if stage == "notebook":
                book["page_id"] = rb["page_id"]
                state["records"][key] = {"page_id": rb["page_id"], "kind": "notes", "content_mode": "remote_append",
                                         "properties": op["properties"], "fingerprint": op["fingerprint"]}
            else:
                if receipt.get("parent_page_id") != book["page_id"]:
                    raise ValueError("Evidence page must be verified under the notebook.")
                book["evidence_page_id"] = rb["page_id"]
        elif stage == "asset":
            part = section(fetched, op["start_marker"], op["end_marker"])
            if (receipt.get("attachment_verified") is not True or op["new_anchor"] not in fetched
                    or not re.search(r'!\[|<(?:pdf|file)\s', part)):
                raise ValueError("Uploading bytes is insufficient: verify the actual attached image/PDF.")
            asset = op["asset"]
            saved = {k: asset[k] for k in ("id", "sha256", "suffix", "role", "pages")}
            saved["upload_id"] = receipt.get("upload_id") or op.get("uploaded_id")
            if receipt.get("remote_source"):
                if receipt["remote_source"] not in part:
                    raise ValueError("Native attachment identity must occur in actual fetched content.")
                saved["remote_source"] = receipt["remote_source"]
            _asset_markdown(saved)
            book["assets"][asset["sha256"]] = saved
            book["asset_count"] += 1
            book["evidence_version"] += 1
        elif stage == "evidence_entry":
            section(fetched, marker("批次", batch["id"]), marker("批次结束", batch["id"]))
            if fetched.count(op["new_anchor"]) != 1:
                raise ValueError("Evidence batch append not verified.")
            batch["evidence_verified"] = True
            book["evidence_version"] += 1
        else:
            part = section(fetched, marker("笔记", batch["entry_id"]), marker("笔记结束", batch["entry_id"]))
            # Notion normalizes math, blank lines and file URLs. Host verifies
            # semantic equivalence; store the *returned* segment as hash baseline.
            if any(a["sha256"] not in book["assets"] for a in batch["assets"]):
                raise ValueError("Original attachments are not fully verified.")
            if stage == "entry":
                expected_anchor = anchor({**book, "entry_count": book["entry_count"] + 1})
                if fetched.count(expected_anchor) != 1:
                    raise ValueError("Append progress marker not verified.")
                book["entry_count"] += 1
            batch.update(status="verified", body_hash=content_hash(part), verified_at=datetime.now(timezone.utc).isoformat(),
                         page_id=book["page_id"], evidence_page_id=book["evidence_page_id"])
            if batch.get("revision_of"):
                old = notes["batches"][batch["revision_of"]]
                old["superseded_by"] = batch["id"]
            for asset in batch["assets"]:
                asset.pop("cache_path", None)
            cleanup.append(batch["id"])
        state["inflight"].pop(key)
        state.setdefault("history", []).append({"operation_id": op["operation_id"], "source_key": key,
            "action": stage, "batch_id": batch["id"], "page_id": rb["page_id"]})
    return cleanup


def cleanup_cache(directory, batch_ids):
    root = Path(directory) / "notes-cache"
    for identity in batch_ids:
        if not re.fullmatch(r"[a-f0-9]{20}", identity):
            raise ValueError("Invalid cache identity")
        path = root / identity
        if path.exists() and not path.is_symlink() and path.resolve().parent == root.resolve():
            shutil.rmtree(path)


def project_notes(plan, state):
    """Add metadata only; published bodies have a dedicated append owner."""
    notes = state.get("class_notes", {})
    courses = {r["source_key"]: r for r in plan["records"] if r["kind"] == "courses"}
    for book in notes.get("notebooks", {}).values():
        course = courses.get(book["course_key"])
        if course and not book.get("obsidian"):
            book["title"] = course["properties"]["Name"] + "｜课堂笔记"
    keys = {b["key"] for b in notes.get("notebooks", {}).values()}
    plan["records"] = [r for r in plan["records"] if r["source_key"] not in keys]
    plan["records"].extend(notebook_record(book) for book in notes.get("notebooks", {}).values())
    plan["note_entries"] = []
    for batch in notes.get("batches", {}).values():
        if batch["status"] != "verified" or batch.get("superseded_by") or notes["notebooks"][batch["notebook_key"]].get("obsidian"):
            continue
        book = notes["notebooks"][batch["notebook_key"]]
        plan["note_entries"].append({k: batch.get(k) for k in ("id", "entry_id", "date", "topic", "occurrence_id", "series_id", "class_info", "review_items", "verified_at")}
                                   | {"course_key": book["course_key"], "notebook_key": book["key"], "url": page_url(book["page_id"]),
                                      "evidence_url": page_url(book["evidence_page_id"])})
    for book in notes.get("notebooks", {}).values():
        local = book.get("obsidian")
        if not local:
            continue
        for unit in local.get("units", []):
            plan["note_entries"].append({"id":unit["id"],"entry_id":unit["id"],"date":None,
                "topic":unit["title"],"course_key":book["course_key"],"notebook_key":book["key"],
                "url":page_url(book["page_id"]) if book.get("page_id") else None,
                "verified_at":local["indexed_at"],"storage":"Obsidian","note_id":local["note_id"],
                "vault_key":local["vault_key"],"note_path":local["note_path"],"anchor":unit["anchor"],
                "obsidian_uri":local["obsidian_uri"],"link_status":local.get("properties", {}).get("Link Status", "pending"),
                "retrieval":"notes-find --note-id " + local["note_id"] + " --topic <topic> --read"})
    plan["note_entries"].sort(key=lambda b: (b.get("date") or "", b["verified_at"]), reverse=True)
    return plan


def link_preparation_notes(plan):
    """Resolve local notebooks, or link the preceding confirmed legacy class."""
    classes = plan.get("class_schedule", {}).get("classes", [])
    by_id = {c["occurrence_id"]: c for c in classes}
    for record in plan.get("records", []):
        if record.get("kind") != "tasks":
            continue
        record.pop("note_materials", None)
        local_notes = [n for n in plan.get("note_entries", []) if n.get("storage") == "Obsidian"
                       and n["course_key"] in record.get("properties", {}).get("Course", [])]
        if local_notes:
            first = local_notes[0]
            record["note_materials"] = [{"source_key":first["notebook_key"],"title":"Course notebook — select the relevant unit",
                "url":first["obsidian_uri"],"note_id":first["note_id"],"availability":"resolve_local_before_reading",
                "retrieval":first["retrieval"],"date_scope":"Thematic units; class date unconfirmed"}]
        prep = record.get("preparation", {})
        current = by_id.get(prep.get("trigger_id"))
        if not current:
            continue
        earlier = [c for c in classes if c["series_id"] == current["series_id"] and not c.get("cancelled")
                   and (c.get("start") or c["date"]) < (current.get("start") or current["date"])]
        if not earlier:
            continue
        previous = max(earlier, key=lambda c: c.get("start") or c["date"])
        found = [n for n in plan.get("note_entries", []) if n["course_key"] == current["course_key"]
                 and n.get("occurrence_id") == previous["occurrence_id"]]
        if found:
            record["note_materials"] = [{"source_key": n["notebook_key"], "title": n["date"] + " · " + n["topic"],
                "url": n["url"], "entry_id": n["entry_id"], "occurrence_id": n["occurrence_id"], "availability": "verified_in_notion"} for n in found]
    return plan

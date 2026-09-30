"""Optional course-notes adapter. No Canvas credentials or network are required."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from .learner import notion_value


def index_text(value):
    value = notion_value(value)
    if not isinstance(value, str):
        return value
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        return value[1:-1]
    return re.sub(r"(?<!\\)\[([^\]]+)\]\([^)]*\)", r"\1", value)


def core(config, action, **options):
    settings = config.get("notes", {})
    command = settings.get("command", ["math2obsidian"])
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError("notes.command must be an argv array, never shell text.")
    argv = [*command, "notebook", action, "--json"]
    if settings.get("registry"):
        argv += ["--registry", settings["registry"]]
    argv += ["--user", settings.get("user", "default")]
    for key, value in options.items():
        if value is not None:
            argv += ["--" + key.replace("_", "-"), str(value)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=60)
    except FileNotFoundError as exc:
        raise ValueError("Install course-notes / math2obsidian or configure notes.command.") from exc
    result = json.loads(proc.stdout)
    if not result.get("ok") and not result.get("artifacts", {}).get("status"):
        raise ValueError("course-notes: " + "; ".join(i["message"] for i in result.get("issues", [])))
    return result["artifacts"]


def find(config, state, *, course=None, note_id=None, term=None, topic=None, read=False):
    if not course and not note_id:
        raise ValueError("Supply --course or --note-id.")
    books = list(state.get("class_notes", {}).get("notebooks", {}).values())
    matches = [book for book in books if book.get("obsidian")
               and (not term or book["term"] == term)
               and (not note_id or book["obsidian"]["note_id"] == note_id)
               and (not course or course.casefold() in [str(v).casefold() for v in
                    [book["course_key"], book.get("course_code"), book.get("title"),
                     *book["obsidian"].get("aliases", [])]])]
    if len(matches) == 1:
        note_id = matches[0]["obsidian"]["note_id"]
    result = core(config, "locate", note_id=note_id, course=None if note_id else course,
                  term=term or config.get("term", {}).get("key"), topic=topic)
    if result.get("status") == "ready" and read:
        text = Path(result["path"]).read_text(encoding="utf-8")
        if topic:
            units = {u["id"] for u in result["units"]}
            blocks = re.finditer(r"<!-- course-notes:unit ([A-Za-z0-9-]+) -->\n(.*?)"
                                 r"\n<!-- /course-notes:unit \1 -->", text, re.S)
            text = "\n\n".join(m[2] for m in blocks if m[1] in units)
        result["content"] = text
        result["content_scope"] = "matching-units" if topic else "complete-notebook"
    return result


def index_notebook(config, state, plan, course_key, note_id, readback=None):
    courses = {r["source_key"]: r for r in plan.get("records", []) if r["kind"] == "courses"}
    if course_key not in courses:
        raise ValueError("Choose an existing course Source Key.")
    result = core(config, "reindex", note_id=note_id)
    if result.get("status") != "ready":
        for book in state.get("class_notes", {}).get("notebooks", {}).values():
            local = book.get("obsidian", {})
            if book.get("course_key") == course_key and local.get("note_id") == note_id:
                local.setdefault("properties", {})["Link Status"] = result.get("status", "missing")
                local["sync_status"] = "pending"
        return result
    term = config["term"]["key"]
    if result["term"] != term:
        raise ValueError("Resolved notebook belongs to another term.")
    key = course_key + "|notebook=" + term
    books = state.setdefault("class_notes", {"schema_version": 1, "notebooks": {}, "batches": {}})["notebooks"]
    book = books.setdefault(key, {"key": key, "course_key": course_key, "term": term,
                                  "title": courses[course_key]["properties"]["Name"] + " | Class Notes",
                                  "batch_ids": [], "assets": {}})
    # Keep remote identity and any user-edited title in existing records.
    book["course_code"] = result["course"]
    props = {"Storage": "Obsidian", "Note ID": note_id, "Vault Key": result["vault_key"],
             "Note Path": result["note_path"], "Obsidian URI": result["obsidian_uri"],
             "date:Last Indexed:start": result["indexed_at"], "date:Last Indexed:is_datetime": 1,
             "Link Status": "ready"}
    old = book.get("obsidian", {})
    book["obsidian"] = {k: result[k] for k in ("note_id", "vault_key", "note_path", "obsidian_uri",
                                               "units", "sha256", "indexed_at", "aliases")}
    book["obsidian"]["properties"] = props
    book["obsidian"]["sync_status"] = "pending"
    if readback:
        remote_props = readback.get("properties", readback)
        if book.get("page_id") and readback.get("page_id") != book["page_id"]:
            raise ValueError("Readback must identify the existing Notion page.")
        required = ("Storage", "Note ID", "Vault Key", "Note Path", "Obsidian URI", "Link Status")
        if any(index_text(remote_props.get(k)) != props[k] for k in required):
            raise ValueError("Remote readback differs from the resolved local note.")
        if not readback.get("page_id"):
            raise ValueError("A verified readback requires its remote page_id.")
        book["page_id"] = readback["page_id"]
        if remote_props.get("date:Last Indexed:start"):
            props["date:Last Indexed:start"] = remote_props["date:Last Indexed:start"]
        book["obsidian"]["sync_status"] = "verified"
        book["obsidian"]["remote_verified_at"] = result["indexed_at"]
    elif old.get("sha256") == result["sha256"] and old.get("note_path") == result["note_path"]:
        book["obsidian"]["sync_status"] = old.get("sync_status", "pending")
    return {"status": "ready", "source_key": key, "page_id": book.get("page_id"),
            "properties": props, "sync_status": book["obsidian"]["sync_status"]}


def index_operations(state):
    operations = []
    for book in state.get("class_notes", {}).get("notebooks", {}).values():
        local = book.get("obsidian", {})
        if local.get("sync_status") == "pending":
            operations.append({"notes_stage": "obsidian_index", "action": "update_properties"
                               if book.get("page_id") else "create_index_page",
                               "page_id": book.get("page_id"), "source_key": book["key"],
                               "note_id": local["note_id"], "course_key": book["course_key"],
                               "properties": local["properties"] if book.get("page_id") else {
                                   "Name": book["title"], "Term": book["term"],
                                   "Source Key": book["key"], "Type": "Class notes",
                                   **local["properties"]},
                               "recovery": "Fetch current page; update metadata; notes-index --readback."})
    return {"operations": operations, "blocked": [], "backend": "obsidian"}

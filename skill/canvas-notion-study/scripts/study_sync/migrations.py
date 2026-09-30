"""Reversible, preflighted v1 -> v2 migration of local JSON state."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
from typing import Mapping
from uuid import uuid4

from .learner import normalise_learner
from .state import read_json, write_json

CURRENT_VERSION = 2


def migrate_document(document: Mapping, kind: str = "state") -> dict:
    result = deepcopy(dict(document))
    version = result.get("schema_version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1 or version > CURRENT_VERSION:
        raise ValueError(f"Unsupported {kind} schema version {version}; no migration performed")
    if kind == "learner":
        return normalise_learner(result)
    result["schema_version"] = CURRENT_VERSION
    return result


def migrate_state_dir(path: str | Path, dry_run: bool = True) -> dict:
    root = Path(path).expanduser().resolve()
    # Validate every affected file before writing even the backup directory.
    documents, changes = {}, []
    for filename, kind in (("notion-state.json", "state"), ("learner.json", "learner"), ("state-schema.json", "marker")):
        target = root / filename
        if target.exists():
            before = read_json(target)
            if not isinstance(before, Mapping):
                raise ValueError(f"{filename} must contain a JSON object")
            after = migrate_document(before, kind)
            documents[filename] = (before, after)
            if before != after:
                changes.append({"file": filename, "from": before.get("schema_version", 1), "to": CURRENT_VERSION})
    if "learner.json" not in documents:
        documents["learner.json"] = (None, normalise_learner(None))
        changes.append({"file": "learner.json", "from": None, "to": CURRENT_VERSION})
    if "state-schema.json" not in documents:
        documents["state-schema.json"] = (None, {"schema_version": CURRENT_VERSION, "storage": "local_json"})
        changes.append({"file": "state-schema.json", "from": None, "to": CURRENT_VERSION})
    report = {"dry_run": dry_run, "target_version": CURRENT_VERSION, "state_dir": str(root), "changes": changes,
              "backup_dir": None, "applied": False, "preserves": ["source_keys", "page_ids", "personal_fields", "history", "inflight"]}
    if dry_run or not changes:
        return report
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = root / "migration-backups" / (stamp + "-" + uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    manifest = {"from_version": 1, "to_version": CURRENT_VERSION, "files": [], "created_files": []}
    for filename, (before, _) in documents.items():
        if before is None:
            manifest["created_files"].append(filename)
            continue
        source = root / filename
        shutil.copy2(source, backup / filename)
        (backup / filename).chmod(0o600)
        manifest["files"].append({"name": filename, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    write_json(backup / "manifest.json", manifest)
    for change in changes:
        filename = change["file"]
        write_json(root / filename, documents[filename][1])
    report.update({"backup_dir": str(backup), "applied": True})
    return report

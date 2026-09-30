"""Synthetic evidence, readback, archive hashes and reversible migration."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill/canvas-notion-study/scripts"))
from study_sync.audit import audit_plan, audit_files, compare_baseline
from study_sync.migrations import migrate_state_dir
from study_sync.state import write_json, read_json


class AuditTests(unittest.TestCase):
    def test_requirement_coverage_and_notional_page_reconciliation(self):
        plan = {"records": [{"kind": "tasks", "source_key": "task:a", "requirement_ids": ["req:a"], "properties": {"Source": "source:a"}}]}
        requirements = {"requirements": [{"id": "req:a", "source_refs": ["source:a"]}]}
        remote = {"results": [{"id": "page-a", "properties": {"Source Key": "task:a"}}], "has_more": False}
        report = audit_plan(plan, requirements, remote, {"records": {"task:a": {"page_id": "page-a"}}})
        self.assertTrue(report["ok"])
        self.assertEqual(report["covered_required_count"], 1)
        requirements["requirements"].append({"id": "req:b", "source_refs": ["source:b"], "task_keys": ["task:missing"]})
        report = audit_plan(plan, requirements, remote)
        self.assertFalse(report["ok"])
        self.assertIn("requirement_task_missing", {i["code"] for i in report["issues"]})

    def test_duplicate_id_and_partial_readback_are_explicit(self):
        task = {"kind": "tasks", "source_key": "a", "properties": {"Source": "source:a"}}
        report = audit_plan({"records": [task, deepcopy(task)]}, [], {"results": [{"id": "p1", "source_key": "a"}, {"id": "p2", "source_key": "a"}], "has_more": True})
        self.assertEqual(report["duplicate_source_keys"][0]["count"], 2)
        self.assertEqual(report["notion"]["status"], "partial")
        self.assertEqual(len(report["notion"]["duplicate_source_keys"]), 1)
        self.assertFalse(report["ok"])

    def test_hash_all_files_and_baseline_differences(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / "synthetic.txt"
            file.write_text("Synthetic course material")
            sha = hashlib.sha256(file.read_bytes()).hexdigest()
            snapshot = {"courses": [{"id": 1, "files": [{"id": 7, "local_path": str(file), "sha256": sha, "download_status": "downloaded"}]}]}
            before = audit_files(snapshot)
            self.assertTrue(before["ok"])
            file.write_text("Changed material")
            after = audit_files(snapshot, before)
            self.assertEqual(after["counts"], {"hash_mismatch": 1})
            self.assertEqual(len(after["baseline_diff"]["changed"]), 1)
            file.unlink()
            self.assertEqual(audit_files(snapshot)["counts"], {"file_missing": 1})
            snapshot["courses"][0]["files"].append({"id": 8, "download_status": "failed"})
            self.assertEqual(audit_files(snapshot)["file_count"], 2)
            diff = compare_baseline([{"id": "a"}, {"id": "b"}], [{"id": "a"}, {"id": "c"}])
            self.assertEqual(diff["added"], ["b"])
            self.assertEqual(diff["missing"], ["c"])

    def test_course_file_index_deduplicates_nested_metadata_and_attachments(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root)/"reading.txt";file.write_text("Synthetic reading")
            sha = hashlib.sha256(file.read_bytes()).hexdigest()
            archived = {"id": 7, "archive": {"local_path": str(file), "sha256": sha, "download_status": "downloaded"},
                        "metadata": {"id": 7, "download_status": "not_downloaded"}, "source_raw": {"id": 7, "download_status": "pending"}}
            snapshot = {"courses": [{"id": 1, "files": [archived, {"id": 7, "download_status": "pending"}, {"id": 8, "download_status": "unavailable"}],
                                     "assignments": [{"id": 20, "attachments": [archived]}]}]}
            report = audit_files(snapshot)
            self.assertEqual(report["file_count"], 2)
            self.assertEqual(report["counts"], {"verified": 1, "not_downloaded": 1})

    def test_v1_migration_dryrun_backup_preservation_and_newer_refusal(self):
        with tempfile.TemporaryDirectory() as root:
            state = {"schema_version": 1, "records": {"a": {"page_id": "keep-url", "personal": "keep"}}, "inflight": {}, "history": [{"action": "created"}]}
            path = Path(root) / "notion-state.json"
            write_json(path, state)
            original = path.read_bytes()
            preview = migrate_state_dir(root)
            self.assertTrue(preview["dry_run"])
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((Path(root) / "learner.json").exists())
            applied = migrate_state_dir(root, dry_run=False)
            self.assertTrue(applied["applied"])
            self.assertEqual((Path(applied["backup_dir"]) / "notion-state.json").read_bytes(), original)
            migrated = read_json(path)
            self.assertEqual(migrated["schema_version"], 2)
            self.assertEqual(migrated["records"], state["records"])
            self.assertEqual(migrated["history"], state["history"])
            self.assertEqual(migrate_state_dir(root, False)["changes"], [])
            write_json(Path(root) / "learner.json", {"schema_version": 3})
            before_refusal = path.read_bytes()
            with self.assertRaises(ValueError):
                migrate_state_dir(root, False)
            self.assertEqual(path.read_bytes(), before_refusal)


if __name__ == "__main__":
    unittest.main()

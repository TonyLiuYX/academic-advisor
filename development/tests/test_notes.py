"""Behavior tests for notebook delivery, recovery and mentor integration."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from study_sync.notes import (import_batch, next_operation, commit_note_receipts, cleanup_cache, content_hash,
                              project_notes, link_preparation_notes, marker, state_lock)
from study_sync.state import empty_state, prepare_operations, commit_receipts, digest, write_json, rendered_content
from study_sync.v1 import build_v1_plan
from study_sync.context import build_context

COURSE = "https://canvas.test|user=7|type=course|id=1"


class NotebookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"term": {"key": "fall", "timezone": "America/Toronto"},
                       "notion": {"databases": {"notes": {"data_source_id": "notes-db"}}}}
        self.plan = {"term": self.config["term"], "records": [
            {"kind": "courses", "source_key": COURSE, "properties": {"Name": "MAT186"}},
            {"kind": "notes", "source_key": "syllabus", "properties": {"Name": "syllabus"}, "generated_content": "keep"}],
            "databases": {"notes": {"properties": {"Course": {"type": "relation"}}}}, "stats": {}}
        self.state = empty_state()
        self.state["records"][COURSE] = {"page_id": "course-page", "kind": "courses"}
        self.pages = {}

    def payload(self, name="p1", text=None, day="2026-09-16"):
        path = self.root / (name + ".png")
        path.write_bytes(b"test image content " + name.encode())
        return {"course_key": COURSE, "date": day, "topic": "Limits", "sources": [{"id": name, "path": str(path), "pages": "1"}],
                "sections": [{"source_ids": [name], "markdown": text or "$$\n\\lim_{x\\to0} x=0\n$$"}]}

    def ingest(self, payload):
        return import_batch(self.state, self.plan, payload, self.root, self.config)

    def readbacks(self):
        notes = self.state.get("class_notes", {})
        books = list(notes.get("notebooks", {}).values())
        if not books:
            return {}
        book = books[0]
        return {role: {"page_id": book[field], "content": self.pages[book[field]], "truncated": False, "unknown_block_count": 0}
                for role, field in (("main", "page_id"), ("evidence", "evidence_page_id")) if book.get(field)}

    def next(self, batch=None):
        return next_operation(self.state, self.config, self.root, batch, self.readbacks(), True)

    def execute(self, op, commit=True):
        stage = op["notes_stage"]
        extra = {}
        if stage in ("notebook", "evidence_page"):
            page = "main-page" if stage == "notebook" else "evidence-page"
            self.pages[page] = op["payload"]["pages"][0]["content"]
            if stage == "evidence_page":
                extra["parent_page_id"] = "main-page"
        elif stage == "asset":
            page = op["page_id"]
            uploaded = "12345678-1234-1234-1234-123456789abc"
            fragment = op["start_marker"] + '\n\n![original](attachment:verified-file)\n\n' + op["end_marker"] + "\n\n" + op["new_anchor"]
            self.pages[page] = self.pages[page].replace(op["old_str"], fragment)
            extra = {"upload_id": uploaded, "attachment_verified": True}
        else:
            page = op["page_id"]
            edit = op["payload"]["content_updates"][0]
            self.assertEqual(self.pages[page].count(edit["old_str"]), 1)
            self.pages[page] = self.pages[page].replace(edit["old_str"], edit["new_str"])
        receipt = {"source_key": op["source_key"], "operation_id": op["operation_id"], "status": "succeeded",
                   "content_verified": True, "readback": {"page_id": page, "content": self.pages[page], "truncated": False, "unknown_block_count": 0}, **extra}
        if commit:
            cleanup_cache(self.root, commit_note_receipts(self.state, [receipt]))
        return receipt

    def deliver(self, bid):
        stages = []
        for _ in range(20):
            result = self.next(bid)
            if result.get("complete"):
                return stages
            self.assertFalse(result["blocked"])
            op = result["operations"][0]
            stages.append(op["notes_stage"])
            self.execute(op)
        self.fail("Delivery did not terminate")

    def test_full_delivery_and_cleanup_preserves_user_original(self):
        payload = self.payload(text="**Definition**\n\n[[asset:p1]]")
        payload["review_items"] = [{"source_id": "p1", "message": "Exponent unclear"}]
        bid = self.ingest(payload)["batch_id"]
        self.assertEqual(self.deliver(bid), ["notebook", "evidence_page", "asset", "evidence_entry", "entry"])
        self.assertIn("Exponent unclear", self.pages["main-page"])
        self.assertIn("Exponent unclear", self.pages["evidence-page"])
        self.assertTrue(Path(payload["sources"][0]["path"]).exists())
        self.assertFalse((self.root / "notes-cache" / bid).exists())
        self.assertFalse(self.state["inflight"])
        self.assertNotIn("Definition", json.dumps(self.state))

    def test_second_day_append_and_personal_text_preserved(self):
        a = self.ingest(self.payload())["batch_id"]; self.deliver(a)
        original = self.pages["main-page"]
        self.pages["main-page"] += "\nMy personal remarks."
        b = self.ingest(self.payload("p2", "Second lecture", "2026-09-17"))["batch_id"]
        self.assertEqual(self.deliver(b), ["asset", "evidence_entry", "entry"])
        self.assertEqual(self.pages["main-page"].count(marker("笔记", a)), 1)
        self.assertIn("My personal remarks.", self.pages["main-page"])
        self.assertIn("Second lecture", self.pages["main-page"])

    def test_reorder_duplicate_and_partial_overlap(self):
        p = self.payload(); q = self.payload("p2", "Two")
        p["sources"] += q["sources"]; p["sections"] += q["sections"]
        bid = self.ingest(p)["batch_id"]; self.deliver(bid)
        reverse = deepcopy(p); reverse["sources"].reverse()
        self.assertEqual(self.ingest(reverse)["status"], "duplicate")
        third = self.payload("p3", "Three")
        combined = deepcopy(p); combined["sources"] += third["sources"]; combined["sections"] += third["sections"]
        result = self.ingest(combined)
        self.assertEqual(result["duplicate_source_ids"], ["p1", "p2"])
        self.deliver(result["batch_id"])
        self.assertEqual(self.pages["main-page"].count("Two"), 1)
        self.assertEqual(self.pages["main-page"].count("Three"), 1)

    def test_mixed_old_new_section_requires_source_split(self):
        p = self.payload(); bid = self.ingest(p)["batch_id"]; self.deliver(bid)
        q = self.payload("p2"); q["sources"] += p["sources"]
        q["sections"] = [{"source_ids": ["p1", "p2"], "markdown": "Mixed"}]
        with self.assertRaisesRegex(ValueError, "Partial overlap"):
            self.ingest(q)

    def test_timeout_reconcile_commits_without_duplicate(self):
        bid = self.ingest(self.payload())["batch_id"]
        op = self.next(bid)["operations"][0]
        receipt = self.execute(op, commit=False)
        self.assertEqual(self.next(bid)["blocked"][0]["reason"], "inflight_requires_reconciliation")
        commit_note_receipts(self.state, [receipt])
        self.deliver(bid)
        self.assertEqual(self.pages["main-page"].count(marker("笔记", bid)), 1)

    def test_upload_and_truncated_readback_cannot_claim_success(self):
        bid = self.ingest(self.payload())["batch_id"]
        self.execute(self.next(bid)["operations"][0]); self.execute(self.next(bid)["operations"][0])
        op = self.next(bid)["operations"][0]; receipt = self.execute(op, commit=False)
        receipt["attachment_verified"] = False
        with self.assertRaisesRegex(ValueError, "Uploading bytes"):
            commit_note_receipts(deepcopy(self.state), [receipt])
        receipt["attachment_verified"] = True; receipt["readback"]["truncated"] = True
        with self.assertRaisesRegex(ValueError, "complete Notion readback"):
            commit_note_receipts(deepcopy(self.state), [receipt])
        self.assertTrue((self.root / "notes-cache" / bid).exists())

    def test_generic_receipt_cannot_bypass_notes_verification(self):
        bid = self.ingest(self.payload())["batch_id"]
        op = self.next(bid)["operations"][0]
        with self.assertRaisesRegex(ValueError, "notes-receipts"):
            commit_receipts(self.state, [{"source_key": op["source_key"], "operation_id": op["operation_id"], "status": "succeeded", "page_id": "fake"}])

    def test_explicit_revision_reuses_originals_and_preserves_other_batches(self):
        a = self.ingest(self.payload(text="Original"))["batch_id"]; self.deliver(a)
        b = self.ingest(self.payload("p2", "Keep me"))["batch_id"]; self.deliver(b)
        revised = {"course_key": COURSE, "revision_of": a, "topic": "Correction", "sections": [{"source_ids": ["p1"], "markdown": "Corrected"}]}
        rid = self.ingest(revised)["batch_id"]
        self.assertEqual(self.deliver(rid), ["evidence_entry", "revision"])
        self.assertIn("Keep me", self.pages["main-page"])
        self.assertIn("Corrected", self.pages["main-page"])
        self.assertNotIn("Original", self.pages["main-page"])
        self.assertEqual(len(project_notes(deepcopy(self.plan), self.state)["note_entries"]), 2)
        with self.assertRaisesRegex(ValueError, "revision_of"):
            self.ingest(revised)

    def test_manual_edit_blocks_revision_but_not_new_append(self):
        a = self.ingest(self.payload(text="Original"))["batch_id"]; self.deliver(a)
        self.pages["main-page"] = self.pages["main-page"].replace("Original", "Edited in Notion")
        b = self.ingest(self.payload("p2", "New"))["batch_id"]; self.deliver(b)
        rid = self.ingest({"course_key": COURSE, "revision_of": a, "markdown": "Correction"})["batch_id"]
        self.execute(self.next(rid)["operations"][0])
        with self.assertRaisesRegex(ValueError, "changed in Notion"):
            self.next(rid)
        self.assertIn("Edited in Notion", self.pages["main-page"])

    def test_plan_rebuild_preserves_notebook_and_syllabus(self):
        bid = self.ingest(self.payload())["batch_id"]; self.deliver(bid)
        p = project_notes(deepcopy(self.plan), self.state)
        out = prepare_operations(p, self.state, self.config["notion"]["databases"], kind="notes")
        self.assertFalse(any(o["source_key"].endswith("notebook=fall") for o in out["operations"]))
        snapshot = {"canvas_origin": "https://canvas.test", "user": {"id": 7}, "term": self.config["term"],
                    "courses": [{"id": 1, "name": "MAT186", "mode": "course"}]}
        write_json(self.root / "notion-state.json", self.state)
        rebuilt = build_v1_plan(snapshot, {}, self.config, {}, self.root)
        notebook = next(r for r in rebuilt["records"] if r.get("content_mode") == "remote_append")
        self.assertEqual(notebook["generated_content"], "")
        self.assertEqual(rebuilt["note_entries"][0]["id"], bid)

    def test_context_and_correct_previous_class_only(self):
        plan = deepcopy(self.plan)
        plan["class_schedule"] = {"classes": [
            {"occurrence_id": "lec:1", "series_id": "lec", "course_key": COURSE, "date": "2026-09-16"},
            {"occurrence_id": "lec:2", "series_id": "lec", "course_key": COURSE, "date": "2026-09-18"},
            {"occurrence_id": "lab:1", "series_id": "lab", "course_key": COURSE, "date": "2026-09-17"}]}
        plan["note_entries"] = [{"id": "n1", "entry_id": "n1", "course_key": COURSE, "notebook_key": "book", "date": "2026-09-16",
                                 "occurrence_id": "lec:1", "url": "https://notion.so/main", "topic": "Limits"}]
        for oid in ("lec:2", "lab:1"):
            plan["records"].append({"kind": "tasks", "source_key": oid, "properties": {"Name": oid}, "preparation": {"trigger_id": oid}})
        link_preparation_notes(plan)
        self.assertEqual(plan["records"][-2]["note_materials"][0]["entry_id"], "n1")
        self.assertIn('<mention-page url="https://notion.so/main"/>', rendered_content(plan["records"][-2]))
        self.assertNotIn("note_materials", plan["records"][-1])
        learner = {"personal": {"lec:2": {"Done": True}}}; saved = deepcopy(learner)
        context = build_context(plan, learner, now="2026-09-17T12:00:00-04:00")
        self.assertEqual(context["recent_notes"][0]["id"], "n1")
        self.assertEqual(learner, saved)

    def test_today_uses_instance_timezone_and_unknown_date_stays_unknown(self):
        p = self.payload(day="today")
        result = import_batch(self.state, self.plan, p, self.root, self.config, now="2026-09-17T01:00:00+00:00")
        self.assertEqual(self.state["class_notes"]["batches"][result["batch_id"]]["date"], "2026-09-16")
        result = self.ingest(self.payload("p2", day=None))
        self.assertIsNone(self.state["class_notes"]["batches"][result["batch_id"]]["date"])

    def test_invalid_course_or_class_cannot_be_imported(self):
        p = self.payload(); p["course_key"] = "other"
        with self.assertRaisesRegex(ValueError, "course_key"):
            self.ingest(p)
        p["course_key"] = COURSE; p["occurrence_id"] = "unknown"
        with self.assertRaisesRegex(ValueError, "occurrence_id"):
            self.ingest(p)

    def test_concurrent_local_transaction_does_not_overwrite_state(self):
        with state_lock(self.root):
            with self.assertRaisesRegex(ValueError, "Another local"):
                with state_lock(self.root):
                    pass

    def test_refreshed_notion_image_signature_is_not_a_user_edit(self):
        a = '![source](https://prod-files-secure.s3.us-west-2.amazonaws.com/workspace/object/source.png?X-Amz-Date=1&X-Amz-Signature=a)'
        b = a.replace('Date=1', 'Date=2').replace('Signature=a', 'Signature=b')
        self.assertEqual(content_hash(a), content_hash(b))
        self.assertNotEqual(content_hash(a), content_hash(a.replace('/object/', '/different/')))
        self.assertNotEqual(content_hash(a), content_hash(a + '\nUser comment'))

    def test_uploaded_checkpoint_recovers_identity_without_claiming_complete(self):
        bid = self.ingest(self.payload())["batch_id"]
        self.execute(self.next(bid)["operations"][0]); self.execute(self.next(bid)["operations"][0])
        op = self.next(bid)["operations"][0]
        upload = "12345678-1234-1234-1234-123456789abc"
        commit_note_receipts(self.state, [{"source_key": op["source_key"], "operation_id": op["operation_id"],
                                          "status": "uploaded", "upload_id": upload}])
        pending = self.next(bid)["blocked"][0]["operation"]
        self.assertEqual(pending["uploaded_id"], upload)
        self.assertEqual(self.state["class_notes"]["batches"][bid]["status"], "prepared")
        receipt = self.execute(op, commit=False); receipt.pop("upload_id")
        commit_note_receipts(self.state, [receipt])
        self.deliver(bid)

    def test_native_pdf_readback_recovers_lost_upload_identity(self):
        from urllib.parse import quote
        p = self.payload(); pdf = self.root / "original.pdf"; pdf.write_bytes(b"%PDF synthetic original")
        p["sources"][0]["path"] = str(pdf)
        p["sections"][0]["markdown"] = "Original table\n\n[[asset:p1]]"
        bid = self.ingest(p)["batch_id"]
        self.execute(self.next(bid)["operations"][0]); self.execute(self.next(bid)["operations"][0])
        op = self.next(bid)["operations"][0]
        ref = "file://" + quote(json.dumps({"source": "attachment:object:original.pdf",
                                           "permissionRecord": {"table": "block", "id": "block-id", "spaceId": "space"}}))
        self.pages["evidence-page"] = self.pages["evidence-page"].replace(op["old_str"],
            op["start_marker"] + f'\n<pdf src="{ref}"></pdf>\n' + op["end_marker"] + "\n" + op["new_anchor"])
        receipt = {"source_key": op["source_key"], "operation_id": op["operation_id"], "status": "succeeded",
                   "content_verified": True, "attachment_verified": True, "remote_source": ref,
                   "readback": self.readbacks()["evidence"]}
        invalid = deepcopy(receipt); invalid["remote_source"] = "file:///Users/example/original.pdf"
        with self.assertRaises(ValueError):
            commit_note_receipts(deepcopy(self.state), [invalid])
        commit_note_receipts(self.state, [receipt]); self.deliver(bid)
        self.assertIn(ref, self.pages["main-page"])

    def test_multiple_courses_keep_independent_notebooks_and_dedup(self):
        p = self.payload(); first = self.ingest(p)["batch_id"]; self.deliver(first)
        other = COURSE + "2"
        self.plan["records"].append({"kind": "courses", "source_key": other, "properties": {"Name": "PHY180"}})
        self.state["records"][other] = {"page_id": "other-course", "kind": "courses"}
        p["course_key"] = other
        second = self.ingest(p)
        self.assertEqual(second["status"], "prepared")
        self.assertEqual(len(self.state["class_notes"]["notebooks"]), 2)
        op = self.next(second["batch_id"])["operations"][0]
        self.assertEqual(op["properties"]["Course"], ["other-course"])
        self.assertIn("PHY180", op["properties"]["Name"])

    def test_long_notebook_append_preserves_existing_content(self):
        first = self.ingest(self.payload())["batch_id"]; self.deliver(first)
        old = "\n".join(f"Lecture {i}: personal annotation preserved." for i in range(12000))
        self.pages["main-page"] = self.pages["main-page"].replace("## 个人补充", old + "\n## 个人补充")
        second = self.ingest(self.payload("p2", "New lecture"))["batch_id"]; self.deliver(second)
        self.assertIn(old, self.pages["main-page"])
        self.assertEqual(self.pages["main-page"].count(marker("笔记", first)), 1)

    def test_confirmed_class_metadata_and_existing_preparation_link(self):
        schedule = {"series": [{"id": "lec", "course_key": COURSE, "kind": "lecture", "label": "MAT186 LEC0101",
                               "confirmed": True, "source_refs": ["https://canvas.test/syllabus"],
                               "occurrences": [{"date": "2026-09-16"}], "start_time": "10:00", "end_time": "11:00"}]}
        self.plan["records"].append({"kind": "tasks", "source_key": "prep", "properties": {"Name": "Existing preparation"},
                                     "preparation": {"trigger_id": "lec:2026-09-16"}})
        self.state["records"]["prep"] = {"page_id": "12345678-1234-1234-1234-123456789abc", "kind": "tasks"}
        p = self.payload(); p["occurrence_id"] = "lec:2026-09-16"
        bid = import_batch(self.state, self.plan, p, self.root, self.config, schedule=schedule)["batch_id"]
        self.deliver(bid)
        self.assertIn("MAT186 LEC0101", self.pages["main-page"])
        self.assertIn("2026-09-16T10:00:00-04:00", self.pages["main-page"])
        self.assertIn('url="https://www.notion.so/12345678123412341234123456789abc"', self.pages["main-page"])
        self.assertEqual(sum(r["kind"] == "tasks" for r in self.plan["records"]), 1)

    def test_normal_sync_updates_notebook_properties_without_body(self):
        from study_sync.state import begin_operations
        bid = self.ingest(self.payload())["batch_id"]; self.deliver(bid)
        body = self.pages["main-page"]
        self.plan["records"][0]["properties"]["Name"] = "MAT186 renamed"
        plan = project_notes(deepcopy(self.plan), self.state)
        result = prepare_operations(plan, self.state, self.config["notion"]["databases"], kind="notes")
        op = next(o for o in result["operations"] if o.get("content_mode") == "remote_append")
        self.assertEqual(op["action"], "update_properties")
        self.assertNotIn("content", op)
        self.assertNotIn("baseline_region", op)
        self.assertEqual(op["generated_content"], "")
        self.assertEqual(op["properties"]["Name"], "MAT186 renamed｜课堂笔记")
        begin_operations(self.state, [op])
        commit_receipts(self.state, [{"source_key": op["source_key"], "operation_id": op["operation_id"],
                                     "status": "succeeded", "page_id": "main-page"}])
        self.assertEqual(self.state["records"][op["source_key"]]["content_mode"], "remote_append")
        self.assertEqual(self.pages["main-page"], body)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

from study_sync.obsidian_notes import index_notebook, index_operations, find
from study_sync.notes import project_notes, link_preparation_notes


class ObsidianNotesTests(unittest.TestCase):
    def setUp(self):
        self.config = {"term":{"key":"fall"},"notes":{"backend":"obsidian"}}
        self.key = "course-example|notebook=fall"
        self.state = {"class_notes":{"notebooks":{self.key:{"key":self.key,"course_key":"course-example",
            "term":"fall","title":"User title","page_id":"remote-page","batch_ids":[]}},"batches":{}}}
        self.plan = {"records":[{"kind":"courses","source_key":"course-example","properties":{"Name":"Structures"}},
            {"kind":"tasks","source_key":"task-1","properties":{"Course":["course-example"]}}]}
        self.local = {"status":"ready","term":"fall","course":"ENG101","note_id":"note-1",
            "vault_key":"study","note_path":"Courses/fall/ENG101/ENG101.md","obsidian_uri":"obsidian://example",
            "units":[{"id":"u1","title":"Equilibrium","anchor":"^u1"}],"sha256":"abc",
            "indexed_at":"2026-09-28T00:00:00Z","aliases":["Structures"]}

    def test_pending_recovery_keeps_page_identity(self):
        with patch("study_sync.obsidian_notes.core",return_value=self.local):
            result = index_notebook(self.config,self.state,self.plan,"course-example","note-1")
            self.assertEqual(result["page_id"],"remote-page")
            self.assertEqual(index_operations(self.state)["operations"][0]["action"],"update_properties")
            with self.assertRaises(ValueError):
                index_notebook(self.config,self.state,self.plan,"course-example","note-1",{"page_id":"wrong"})
            result = index_notebook(self.config,self.state,self.plan,"course-example","note-1",
                {"page_id":"remote-page","properties":result["properties"]})
            self.assertEqual(result["sync_status"],"verified")
            self.assertFalse(index_operations(self.state)["operations"])
        self.assertEqual(self.state["class_notes"]["notebooks"][self.key]["title"],"User title")

    def test_context_and_preparation_use_local_identity_without_invented_dates(self):
        with patch("study_sync.obsidian_notes.core",return_value=self.local):
            index_notebook(self.config,self.state,self.plan,"course-example","note-1")
        plan = link_preparation_notes(project_notes(self.plan,self.state))
        note = plan["note_entries"][0]
        self.assertEqual(note["storage"],"Obsidian")
        self.assertIsNone(note["date"])
        self.assertIn("--read",note["retrieval"])
        material = plan["records"][1]["note_materials"][0]
        self.assertEqual(material["note_id"],"note-1")
        self.assertEqual(material["availability"],"resolve_local_before_reading")

    def test_find_resolves_each_time_instead_of_trusting_cached_path(self):
        with patch("study_sync.obsidian_notes.core",return_value=self.local):
            index_notebook(self.config,self.state,self.plan,"course-example","note-1")
        with patch("study_sync.obsidian_notes.core",return_value={"status":"missing"}) as call:
            self.assertEqual(find(self.config,self.state,course="Structures")["status"],"missing")
            self.assertEqual(call.call_args.kwargs["note_id"],"note-1")

    def test_missing_file_marks_index_and_context_pending_until_recovered(self):
        with patch("study_sync.obsidian_notes.core",return_value=self.local):
            result = index_notebook(self.config,self.state,self.plan,"course-example","note-1")
            index_notebook(self.config,self.state,self.plan,"course-example","note-1",
                {"page_id":"remote-page","properties":result["properties"]})
        with patch("study_sync.obsidian_notes.core",return_value={"status":"missing"}):
            index_notebook(self.config,self.state,self.plan,"course-example","note-1")
        operation = index_operations(self.state)["operations"][0]
        self.assertEqual(operation["properties"]["Link Status"],"missing")
        self.assertEqual(project_notes(self.plan,self.state)["note_entries"][0]["link_status"],"missing")
        with patch("study_sync.obsidian_notes.core",return_value=self.local):
            result = index_notebook(self.config,self.state,self.plan,"course-example","note-1")
        self.assertEqual(result["sync_status"],"pending")
        self.assertEqual(result["properties"]["Link Status"],"ready")

    def test_notion_markdown_autolinks_do_not_corrupt_locator_readback(self):
        with patch("study_sync.obsidian_notes.core",return_value=self.local):
            result = index_notebook(self.config,self.state,self.plan,"course-example","note-1")
            props = dict(result["properties"])
            props["Note Path"] = "Courses/fall/ENG101/[ENG101.md](http://ENG101.md)"
            props["Obsidian URI"] = "`obsidian://example`"
            result = index_notebook(self.config,self.state,self.plan,"course-example","note-1",
                {"page_id":"remote-page","properties":props})
            self.assertEqual(result["sync_status"],"verified")
            props["Note Path"] = "Courses/fall/OTHER/OTHER.md"
            with self.assertRaises(ValueError):
                index_notebook(self.config,self.state,self.plan,"course-example","note-1",
                    {"page_id":"remote-page","properties":props})


if __name__ == "__main__":
    unittest.main()

"""Ordinary local export flows using synthetic course content only."""
import copy
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

from study_sync.exporting import export_workspace


class Links(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.urls = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.urls.extend(value for key, value in attrs if key in {"href", "src"} and value)


class ExportTests(unittest.TestCase):
    def fixture(self, root):
        original = root / "files" / "file-42" / "v1" / "Lecture notes.pdf"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"synthetic original document bytes")
        file = {
            "id": 42, "display_name": "Lecture notes.pdf",
            "local_path": str(original), "sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
            "download_status": "downloaded", "extraction_status": "not_requested",
            "source_url": "https://canvas.test/courses/10/files/42",
            "discovered_from": [{"kind": "announcement", "object_id": "12", "source_url": "https://canvas.test/courses/10/discussion_topics/12", "field": "attachment"}],
        }
        attachment = {"id": 42, "display_name": "Lecture notes.pdf", "url": "https://canvas.test/files/42/download"}
        course = {
            "id": 10, "name": "Introduction to physics", "course_code": "PHY101", "mode": "course",
            "files": [file],
            "announcements": [{
                "id": 12, "title": "Prepare for Tuesday", "posted_at": "2026-09-07T09:00:00Z",
                "source_url": "https://canvas.test/courses/10/discussion_topics/12",
                "message": '<p>Read <a href="/courses/10/files/42/download">the notes</a> before class.</p>',
                "attachment": attachment,
                "replies": [{"id": 20, "user_name": "Teacher", "message": "<p>Focus on section 2.</p>",
                             "replies": [{"id": 21, "user": {"display_name": "Student"}, "message": "<p>Thank you.</p>", "attachments": [attachment]}]}],
            }],
            "pages": [{"page_id": 30, "title": "Week one", "source_url": "https://canvas.test/courses/10/pages/week-one", "body": "<h2>Reading plan</h2><p>Read chapters 1 and 2.</p>"}],
            "coverage": {"status": "complete", "entries": [{"kind": "announcements", "status": "fresh", "page_count": 2}]},
        }
        return {"term": {"label": "2026 Fall", "key": "2026-fall"}, "canvas_origin": "https://canvas.test", "courses": [course]}, {"records": []}

    def test_html_course_announcements_replies_and_current_attachments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot, plan = self.fixture(root)
            result = export_workspace(snapshot, plan, root)
            self.assertEqual(Path(result["index"]).name, "README.md")
            self.assertEqual(Path(result["calendar"]).name, "tasks.ics")
            self.assertTrue(Path(result["html_index"]).is_file())
            manifest = json.loads(Path(result["manifest"]).read_text())
            course = manifest["courses"][0]
            self.assertEqual(course["coverage"], snapshot["courses"][0]["coverage"])
            self.assertEqual(manifest["files"][0]["discovered_from"], snapshot["courses"][0]["files"][0]["discovered_from"])
            shortcut = Path(manifest["files"][0]["current_path"])
            self.assertEqual(shortcut.parent.name, "current")
            self.assertTrue(shortcut.is_symlink())
            self.assertEqual(shortcut.read_bytes(), Path(manifest["files"][0]["local_path"]).read_bytes())
            announcement_path = Path(course["announcements"][0]["local_path"])
            announcement = announcement_path.read_text()
            self.assertIn("Focus on section 2.", announcement)
            self.assertIn("Thank you.", announcement)
            local_attachment_links = [u for u in Links(announcement).urls if u.startswith("../current/")]
            self.assertEqual(len(local_attachment_links), 3)
            self.assertTrue(all((announcement_path.parent / unquote(u)).resolve() == shortcut.resolve() for u in local_attachment_links))
            self.assertIn("Read chapters 1 and 2.", Path(course["pages"][0]["local_path"]).read_text())
            for page in root.rglob("*.html"):
                for url in Links(page.read_text()).urls:
                    if not urlsplit(url).scheme and not url.startswith("#"):
                        self.assertTrue((page.parent / unquote(url)).exists(), (page, url))
            repeated = export_workspace(snapshot, plan, root)
            self.assertEqual(repeated, result)
            self.assertEqual(len(list(shortcut.parent.iterdir())), 1)

    def test_updated_and_removed_files_clean_generated_links_keep_personal_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot, plan = self.fixture(root)
            first = export_workspace(snapshot, plan, root)
            first_manifest = json.loads(Path(first["manifest"]).read_text())
            old_original = Path(first_manifest["files"][0]["local_path"])
            old_shortcut = Path(first_manifest["files"][0]["current_path"])
            personal = old_shortcut.parent / "my-notes.txt"
            personal.write_text("my own notes")
            personal_link = old_shortcut.parent / "my-original.pdf"
            personal_link.symlink_to(old_original)
            updated = copy.deepcopy(snapshot)
            new_original = root / "files" / "file-42" / "v2" / "Revised notes.pdf"
            new_original.parent.mkdir(parents=True)
            new_original.write_bytes(b"revised document")
            updated["courses"][0]["files"][0].update({
                "display_name": "Revised notes.pdf", "local_path": str(new_original),
                "sha256": hashlib.sha256(new_original.read_bytes()).hexdigest(),
            })
            result = export_workspace(updated, plan, root)
            manifest = json.loads(Path(result["manifest"]).read_text())
            latest = Path(manifest["files"][0]["current_path"])
            self.assertFalse(old_shortcut.is_symlink())
            self.assertEqual(latest.resolve(), new_original.resolve())
            self.assertTrue(old_original.is_file())
            self.assertEqual(personal.read_text(), "my own notes")
            self.assertEqual(personal_link.resolve(), old_original.resolve())
            updated["courses"][0]["files"] = []
            export_workspace(updated, plan, root)
            self.assertFalse(latest.is_symlink())
            self.assertTrue(personal.is_file())
            self.assertTrue(personal_link.is_symlink())

    def test_user_file_at_generated_name_is_retained_and_missing_file_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot, plan = self.fixture(root)
            current = root / "courses" / "PHY101-10" / "current"
            current.mkdir(parents=True)
            personal = current / "42-Lecture notes.pdf"
            personal.write_text("my saved copy")
            snapshot["courses"][0]["files"].append({"id": 43, "display_name": "Not available.pdf", "download_status": "failed", "discovered_from": []})
            result = export_workspace(snapshot, plan, root)
            manifest = json.loads(Path(result["manifest"]).read_text())
            self.assertEqual(personal.read_text(), "my saved copy")
            self.assertNotEqual(Path(manifest["files"][0]["current_path"]), personal)
            self.assertIsNone(manifest["files"][1]["current_path"])
            self.assertEqual((result["files"], result["verified"]), (2, 1))
            self.assertIn("未保存成功", Path(manifest["courses"][0]["html_index"]).read_text())


if __name__ == "__main__":
    unittest.main()

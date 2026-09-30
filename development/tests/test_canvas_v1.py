from __future__ import annotations

import io
import hashlib
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from study_sync.canvas import CanvasClient, CanvasDownloadTooLarge, _collect_course, _safe_list
from study_sync.archive import archive_file


TERM = {"key": "2026-fall", "start": "2026-08-01", "end": "2027-01-01"}


class StreamResponse(io.BytesIO):
    status = 200

    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size < 0:
            raise AssertionError("download must use bounded reads")
        return super().read(size)


class CollectionV1Tests(unittest.TestCase):
    def test_formal_announcements_full_range_supplement_replies_and_attachments(self):
        calls = []

        def transport(url, **kwargs):
            path = urlsplit(url).path
            query = parse_qs(urlsplit(url).query)
            calls.append((path, query))
            if path == "/api/v1/announcements":
                if query.get("page") == ["2"]:
                    return [{"id": 2, "message": "Older accessible notice", "discussion_subentry_count": 0}]
                self.assertEqual(query["start_date"], ["1970-01-01T00:00:00Z"])
                self.assertEqual(query["end_date"], ["2100-01-01T00:00:00Z"])
                return (200, {"Link": '<https://canvas.test/api/v1/announcements?page=2>; rel="next"'},
                        [{"id": 1, "message": "<p>Read the attachment</p>", "discussion_subentry_count": 3,
                          "attachments": [{"id": 50, "filename": "guide.txt", "url": "https://canvas.test/files/50/download"}]}])
            if path.endswith("/discussion_topics"):
                self.assertEqual(query["only_announcements"], ["true"])
                return [{"id": 1, "message": "<p>Read the attachment</p>", "discussion_subentry_count": 3},
                        {"id": 3, "message": "Supplement", "discussion_subentry_count": 0}]
            if path.endswith("/discussion_topics/1/entries"):
                if query.get("page") == ["2"]:
                    return [{"id": 102, "message": "<p>Second entry</p>"}]
                return (200, {"Link": '<https://canvas.test/api/v1/courses/10/discussion_topics/1/entries?page=2>; rel="next"'},
                        [{"id": 101, "message": "<p>First entry</p>", "has_more_replies": True,
                          "recent_replies": [{"id": 103, "message": "Old preview"}]}])
            if path.endswith("/entries/101/replies"):
                return [{"id": 103, "message": "<p>Read this reply</p>", "attachment": {
                    "filename": "reply.txt", "url": "https://canvas.test/files/51/download"}}]
            if path in {"/api/v1/files/50", "/api/v1/files/51"}:
                fid = int(path.rsplit("/", 1)[-1])
                return {"id": fid, "filename": f"{fid}.txt", "url": f"https://canvas.test/files/{fid}/download"}
            if path in {"/files/50/download", "/files/51/download"}:
                return StreamResponse(b"Study material")
            return []

        client = CanvasClient("https://canvas.test", "test", transport=transport, retries=0)
        with tempfile.TemporaryDirectory() as directory:
            course, _ = _collect_course(client, {"id": 10, "name": "Physics"}, "course", TERM,
                                        {"extract_documents": False, "max_file_size": None}, directory, None)
            self.assertEqual([a["id"] for a in course["announcements"]], [1, 2, 3])
            self.assertEqual(course["announcements"][0]["replies"][0]["replies"][0]["text"], "Read this reply")
            self.assertEqual({f["id"] for f in course["files"]}, {50, 51})
            self.assertTrue(all(Path(f["local_path"]).is_file() for f in course["files"]))
            reply_file = next(f for f in course["files"] if f["id"] == 51)
            self.assertEqual(reply_file["discovered_from"][0]["field"], "replies.replies.attachment")
            self.assertEqual(course["coverage"]["status"], "fresh")
            read = next(e for e in course["coverage"]["entries"] if e["endpoint"] == "/api/v1/announcements")
            self.assertEqual(read["page_count"], 2)
            self.assertEqual(read["ids"], ["1", "2"])
            self.assertTrue(read["pagination_complete"])
        self.assertEqual(sum(path == "/files/51/download" for path, _ in calls), 1)

    def test_hub_downloads_referenced_attachments_without_scanning_files_or_pages(self):
        paths = []

        def transport(url, **kwargs):
            path = urlsplit(url).path
            paths.append(path)
            if path == "/api/v1/announcements":
                self.assertEqual(parse_qs(urlsplit(url).query)["start_date"], [TERM["start"]])
                return [{"id": 1, "message": '<a href="/courses/20/files/60">File</a><a href="/courses/20/pages/a">Page</a>',
                         "discussion_subentry_count": 0}]
            if path.endswith("/assignments"):
                return [{"id": 2, "attachments": [{"id": 61, "filename": "form.txt"}]}]
            if path.startswith("/api/v1/files/"):
                fid = path.rsplit("/", 1)[-1]
                return {"id": int(fid), "filename": f"{fid}.txt", "url": f"https://canvas.test/files/{fid}/download"}
            if path.endswith("/download"):
                return StreamResponse(b"Hub required form")
            if path == "/api/v1/calendar_events":
                return []
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as directory:
            client = CanvasClient("https://canvas.test", "test", transport=transport, retries=0)
            course, _ = _collect_course(client, {"id": 20, "name": "Hub", "syllabus_body": '<a href="/files/999">irrelevant</a>'},
                                        "hub", TERM, {"extract_documents": False}, directory, None)
            self.assertEqual({f["id"] for f in course["files"]}, {60, 61})
            self.assertTrue(all(f["download_status"] == "downloaded" for f in course["files"]))
        self.assertNotIn("/api/v1/courses/20/files", paths)
        self.assertFalse(any("/pages" in path or "/modules" in path for path in paths))

    def test_same_course_page_queue_handles_cycles_and_hidden_linked_files(self):
        paths = []

        def transport(url, **kwargs):
            path = urlsplit(url).path
            paths.append(path)
            if path.endswith("/front_page"):
                return {"page_id": 1, "url": "home", "body": '<a href="/courses/10/pages/a">A</a>'}
            if path.endswith("/pages/a"):
                return {"page_id": 2, "url": "a", "body": '<a href="b">B</a><a href="/courses/11/pages/other">Other</a>'}
            if path.endswith("/pages/b"):
                return {"page_id": 3, "url": "b", "body": '<a href="a">A</a><a href="/files/80">Reading</a>'}
            if path == "/api/v1/files/80":
                return {"id": 80, "filename": "reading.txt", "url": "https://canvas.test/files/80/download"}
            if path.endswith("/files/80/download"):
                return StreamResponse(b"reading")
            return []

        with tempfile.TemporaryDirectory() as directory:
            client = CanvasClient("https://canvas.test", "test", transport=transport, retries=0)
            course, _ = _collect_course(client, {"id": 10, "name": "Math", "default_view": "wiki",
                "syllabus_body": '<a href="/courses/10/pages/a">A</a>'}, "course", TERM,
                {"extract_documents": False}, directory, None)
            self.assertEqual({p["page_id"] for p in course["pages"]}, {1, 2, 3})
            self.assertEqual(course["files"][0]["id"], 80)
            self.assertEqual(course["files"][0]["discovered_from"][0]["kind"], "pages")
        self.assertEqual(paths.count("/api/v1/courses/10/pages/a"), 1)
        self.assertEqual(paths.count("/api/v1/courses/10/pages/b"), 1)
        self.assertFalse(any("/courses/11/" in path for path in paths))

    def test_coverage_retains_partial_pages_and_marks_old_fallback_stale(self):
        def transport(url, **kwargs):
            if "page=2" in url:
                return (503, {}, [])
            return (200, {"Link": '<https://canvas.test/items?page=2>; rel="next"'}, [{"id": 1}])

        client = CanvasClient("https://canvas.test", "test", transport=transport, retries=0)
        values, ok = _safe_list(client, "/items", {}, warnings=[], scope="test", code="unavailable",
                                fallback=[{"id": 2, "retrieved_at": "2026-08-01T00:00:00Z"}])
        self.assertFalse(ok)
        self.assertEqual([v["id"] for v in values], [1, 2])
        self.assertEqual([v["coverage_status"] for v in values], ["fresh", "stale"])
        entry = client.coverage_log[-1]
        self.assertEqual(entry["status"], "stale")
        self.assertFalse(entry["pagination_complete"])
        self.assertEqual(entry["fresh_ids"], ["1"])
        self.assertEqual(values[1]["retrieved_at"], "2026-08-01T00:00:00Z")

    def test_failed_endpoint_without_prior_is_unavailable(self):
        client = CanvasClient("https://canvas.test", "test", transport=lambda *a, **k: (403, {}, []), retries=0)
        values, ok = _safe_list(client, "/items", {}, warnings=[], scope="test", code="unavailable")
        self.assertFalse(ok)
        self.assertEqual(values, [])
        self.assertEqual(client.coverage_log[-1]["status"], "unavailable")


class StreamingArchiveV1Tests(unittest.TestCase):
    def test_unlimited_stream_hash_and_metadata_reuse(self):
        data = b"abc123" * 500000
        responses = []

        def transport(url, **kwargs):
            response = StreamResponse(data, {"Content-Length": str(len(data))})
            responses.append(response)
            return response

        client = CanvasClient("https://canvas.test", "test", transport=transport, retries=0)
        record = {"id": 1, "filename": "lecture.bin", "url": "https://canvas.test/files/1/download", "updated_at": "2026-09-01"}
        with tempfile.TemporaryDirectory() as directory:
            first = archive_file(client, record, directory, max_file_size=None, extract_documents=False)
            second = archive_file(client, record, directory, previous=first, max_file_size=None, extract_documents=False)
            self.assertEqual(first["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(first["download_status"], "downloaded")
            self.assertEqual(second["download_status"], "reused")
            self.assertEqual(first["local_path"], second["local_path"])
        self.assertEqual(len(responses), 1)
        self.assertGreater(len(responses[0].read_sizes), 2)
        self.assertLessEqual(max(responses[0].read_sizes), 1024 * 1024)

    def test_stream_limit_preserves_destination_and_removes_temporary_files(self):
        response = StreamResponse(b"0123456789")
        client = CanvasClient("https://canvas.test", "test", transport=lambda *a, **k: response, retries=0)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "file.bin"
            destination.write_bytes(b"previous")
            with self.assertRaises(CanvasDownloadTooLarge):
                client.download_to("/files/1", destination, max_bytes=5)
            self.assertEqual(destination.read_bytes(), b"previous")
            self.assertEqual(list(Path(directory).iterdir()), [destination])
        self.assertEqual(response.read_sizes, [6])

    def test_streamed_valid_fixed_pdf_extracts_known_text(self):
        pdf = (Path(__file__).parent / "fixtures" / "syllabus.pdf").read_bytes()
        client = CanvasClient("https://canvas.test", "test", transport=lambda *a, **k: StreamResponse(pdf), retries=0)
        with tempfile.TemporaryDirectory() as directory:
            item = archive_file(client, {"id": 1, "url": "https://canvas.test/files/1/download", "content-type": "application/pdf"}, directory)
            self.assertEqual(item["extraction_status"], "extracted")
            self.assertIn("Synthetic Canvas material", item["extracted_text"])
            self.assertEqual(item["download_status"], "downloaded")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from study_sync.canvas import (
    CanvasClient,
    CanvasDownloadTooLarge,
    CanvasOriginError,
    collect_snapshot,
    extract_html_file_refs,
    extract_html_text,
    _normalize_file_record,
    _collect_course,
    extract_external_resource_refs,
)


class FakeResponse:
    def __init__(self, body, *, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = body

    def read(self):
        if isinstance(self._body, (dict, list)):
            return json.dumps(self._body).encode()
        return self._body

    def close(self):
        return None


class ReadTrackingResponse:
    def __init__(self, body, *, headers=None):
        self.status = 200
        self.headers = headers or {}
        self.body = body
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body if size < 0 else self.body[:size]

    def close(self):
        return None


class CanvasClientTests(unittest.TestCase):
    def test_pagination_and_same_origin(self):
        calls = []

        def transport(url, headers=None, timeout=None):
            calls.append((url, dict(headers or {}), timeout))
            if urlsplit(url).path == "/api/v1/items":
                if "page=2" in url:
                    return FakeResponse([{"id": 2}])
                return FakeResponse(
                    [{"id": 1}],
                    headers={"Link": '<https://canvas.test/api/v1/items?page=2>; rel="next"'},
                )
            raise AssertionError(url)

        client = CanvasClient("https://canvas.test", "secret", transport=transport, retries=0)
        self.assertEqual(client.list("/api/v1/items", {"per_page": 1}), [{"id": 1}, {"id": 2}])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["Authorization"], "Bearer secret")
        with self.assertRaises(CanvasOriginError):
            client.get("https://evil.test/api/v1/items")

    def test_retry_is_controlled(self):
        calls = []

        def transport(url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return FakeResponse({"error": "busy"}, status=503)
            return FakeResponse({"ok": True})

        client = CanvasClient("https://canvas.test", "token", transport=transport, retries=1, backoff_seconds=0)
        self.assertEqual(client.get("/api/v1/ping"), {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_raw_canvas_object_with_content_is_not_response_envelope(self):
        payload = {
            "id": 42,
            "display_name": "slides.pdf",
            "url": "https://canvas.test/files/42/download?verifier=fixture",
            "content": "Canvas metadata field",
        }

        def transport(url, **kwargs):
            return payload

        client = CanvasClient("https://canvas.test", "token", transport=transport, retries=0)
        self.assertEqual(client.get("/api/v1/files/42"), payload)

    def test_file_metadata_api_url_is_not_used_as_download_bytes(self):
        client = CanvasClient("https://canvas.test", "token", transport=lambda url, **kwargs: FakeResponse({}), retries=0)
        normalized = _normalize_file_record(
            client,
            10,
            {"id": 42, "display_name": "slides.pdf", "url": "https://canvas.test/api/v1/files/42"},
        )
        self.assertEqual(normalized["download_url"], "https://canvas.test/courses/10/files/42/download")
        self.assertEqual(normalized["source_url"], "https://canvas.test/courses/10/files/42")

    def test_byte_limit_is_checked_before_archive(self):
        def transport(url, **kwargs):
            return FakeResponse(b"012345", headers={"Content-Length": "6"})

        client = CanvasClient("https://canvas.test", "token", transport=transport, retries=2, backoff_seconds=0)
        with self.assertRaises(CanvasDownloadTooLarge):
            client.get_bytes("/files/1", max_bytes=3)

    def test_content_length_limit_rejects_before_read(self):
        response = ReadTrackingResponse(b"012345", headers={"Content-Length": "6"})
        client = CanvasClient("https://canvas.test", "token", transport=lambda url, **kwargs: response, retries=0)
        with self.assertRaises(CanvasDownloadTooLarge):
            client.get_bytes("/files/1", max_bytes=3)
        self.assertEqual(response.read_sizes, [])

    def test_unknown_length_download_reads_only_limit_plus_one(self):
        response = ReadTrackingResponse(b"012345")
        client = CanvasClient("https://canvas.test", "token", transport=lambda url, **kwargs: response, retries=0)
        with self.assertRaises(CanvasDownloadTooLarge):
            client.get_bytes("/files/1", max_bytes=3)
        self.assertEqual(response.read_sizes, [4])

    def test_external_cdn_download_has_no_canvas_authorization(self):
        seen = []

        def transport(url, headers=None, **kwargs):
            seen.append((url, dict(headers or {})))
            return FakeResponse(b"cdn")

        client = CanvasClient("https://canvas.test", "token", transport=transport, retries=0)
        with self.assertRaises(CanvasOriginError):
            client.get_bytes("https://cdn.test/file")
        self.assertEqual(client.get_bytes("https://cdn.test/file", allow_external=True), b"cdn")
        self.assertNotIn("Authorization", seen[-1][1])

    def test_stdlib_opener_follows_same_origin_redirect(self):
        requests = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib handler hook
                requests.append((self.path, self.headers.get("Authorization")))
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/finish")
                    self.end_headers()
                    return
                if self.path == "/finish":
                    body = b"pdf"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, format, *args):  # noqa: A002 - stdlib hook
                return None

        server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = CanvasClient(
                f"http://127.0.0.1:{server.server_port}",
                "token",
                retries=0,
            )
            self.assertEqual(client.get_bytes("/start", allow_external=True), b"pdf")
            self.assertEqual([path for path, _ in requests], ["/start", "/finish"])
            self.assertEqual(requests[1][1], "Bearer token")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_owner_is_verified_before_course_enumeration(self):
        calls = []

        def transport(url, **kwargs):
            calls.append(urlsplit(url).path)
            if calls[-1] == "/api/v1/users/self/profile":
                return FakeResponse({"id": 7})
            raise AssertionError("course enumeration must not run after owner mismatch")

        client = CanvasClient("https://canvas.test", "token", transport=transport, retries=0)
        with self.assertRaisesRegex(ValueError, "identity differs"):
            collect_snapshot(client, {"owner_id": 8, "term": {"start": "2026-08-01"}}, tempfile.gettempdir())
        self.assertEqual(calls, ["/api/v1/users/self/profile"])

    def test_html_text_and_file_refs(self):
        value = "<h1>Read&nbsp;this</h1><script>ignore()</script><p>Due <b>Friday</b></p>"
        self.assertEqual(extract_html_text(value), "Read this Due Friday")
        refs = extract_html_file_refs(
            '<a href="/files/123/download">slides.pdf</a><img src="https://evil.test/files/9/x">',
            "https://canvas.test",
        )
        self.assertEqual([ref["source_id"] for ref in refs], ["file:123"])
        self.assertEqual(refs[0]["id"], 123)
        self.assertEqual(refs[0]["source_url"], "https://canvas.test/files/123")

    def test_collect_snapshot_collects_term_data_and_archives_files(self):
        requests = []

        def transport(url, headers=None, timeout=None):
            requests.append((url, dict(headers or {})))
            parts = urlsplit(url)
            path = parts.path
            query = parse_qs(parts.query)
            if path == "/api/v1/users/self/profile":
                return FakeResponse({"id": 7, "name": "Student", "time_zone": "America/Toronto"})
            if path == "/api/v1/courses":
                self.assertEqual(query.get("enrollment_state"), ["active"])
                return FakeResponse(
                    [
                        {
                            "id": 10,
                            "name": "Physics I",
                            "course_code": "PHY180H1",
                            "term": {"name": "2026 Fall", "start_at": "2026-08-01", "end_at": "2027-01-01"},
                            "syllabus_body": '<p><a href="/files/42/download">syllabus.pdf</a></p>',
                        },
                        {"id": 11, "name": "Engineering Hub", "term": {"start_at": "2026-08-01", "end_at": "2027-01-01"}},
                    ],
                )
            if path == "/api/v1/announcements":
                expected_start = "1970-01-01T00:00:00Z" if query.get("context_codes[]") == ["course_10"] else "2026-08-01"
                self.assertEqual(query.get("start_date"), [expected_start])
                return FakeResponse([{"id": 1, "message": "<p>Hello</p>"}])
            if path.endswith("/assignments"):
                self.assertEqual(query.get("override_assignment_dates"), ["true"])
                return FakeResponse([{"id": 2, "name": "Lab", "description": "<p>Read</p>", "submission": {"submitted": False}}])
            if path == "/api/v1/calendar_events":
                return FakeResponse([{"id": 3, "title": "Lecture"}])
            if path.endswith("/modules"):
                return FakeResponse([{"id": 4, "name": "Week 1", "items": []}])
            if path.endswith("/modules/4/items"):
                return FakeResponse([{"id": 5, "title": "Readings"}])
            if path.endswith("/pages"):
                return FakeResponse([{"page_id": "welcome", "url": "welcome", "body": "<p>Start</p>"}])
            if path == "/api/v1/courses/10/files":
                return FakeResponse(
                    [
                        {"id": 42, "display_name": "syllabus.pdf", "url": "https://canvas.test/files/42/download", "size": 3},
                        {"id": 43, "display_name": "large.pdf", "url": "https://canvas.test/files/43/download", "size": 100},
                    ]
                )
            if path == "/files/42/download":
                return FakeResponse(b"pdf")
            raise AssertionError(f"unhandled {url}")

        client = CanvasClient("https://canvas.test", "token", transport=transport, retries=0)
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = collect_snapshot(
                client,
                {
                    "term": {"key": "2026-fall", "start": "2026-08-01", "end": "2027-01-01"},
                    "download_files": True,
                    "extract_documents": False,
                    "max_file_size": 3,
                },
                temp_dir,
            )
            self.assertEqual(snapshot["user"]["id"], 7)
            self.assertEqual([course["mode"] for course in snapshot["courses"]], ["course", "hub"])
            formal = snapshot["courses"][0]
            self.assertEqual(formal["announcements"][0]["text"], "Hello")
            self.assertEqual(formal["assignments"][0]["submission"]["submitted"], False)
            self.assertEqual(formal["files"][0]["download_status"], "downloaded")
            self.assertTrue(Path(formal["files"][0]["local_path"]).is_file())
            oversized = next(item for item in formal["files"] if item["id"] == 43)
            self.assertEqual(oversized["download_status"], "too_large")
            self.assertFalse(any("/files/43/download" in url for url, _ in requests))
            # Hub collection remains intentionally lightweight.
            hub = snapshot["courses"][1]
            self.assertEqual(hub["modules"], [])
            self.assertFalse(any("/courses/11/modules" in url for url, _ in requests))

    def test_module_items_supply_files_and_pages_when_indexes_fail(self):
        def transport(url, **kwargs):
            path = urlsplit(url).path
            if path == "/api/v1/users/self/profile":
                return FakeResponse({"id": 7, "name": "Student"})
            if path == "/api/v1/courses":
                return FakeResponse([{"id": 10, "name": "Physics I", "course_code": "PHY180H1", "term": {"name": "2026 Fall", "start_at": "2026-08-01", "end_at": "2027-01-01"}}])
            if path in {"/api/v1/announcements", "/api/v1/calendar_events", "/api/v1/courses/10/assignments"}:
                return FakeResponse([])
            if path == "/api/v1/courses/10/modules":
                return FakeResponse([{"id": 4, "items": [{"id": 40, "type": "File", "content_id": 42, "title": "slides.pdf"}, {"id": 41, "type": "Page", "page_url": "office-hours", "title": "Office hours"}]}])
            if path == "/api/v1/courses/10/modules/4/items":
                return FakeResponse([{"id": 40, "type": "File", "content_id": 42, "title": "slides.pdf"}, {"id": 41, "type": "Page", "page_url": "office-hours", "title": "Office hours"}])
            if path == "/api/v1/courses/10/pages":
                return FakeResponse([], status=404)
            if path == "/api/v1/courses/10/pages/office-hours":
                return FakeResponse({"page_id": "office-hours", "url": "office-hours", "body": "<p>Thursday</p>"})
            if path == "/api/v1/courses/10/files":
                return FakeResponse([], status=403)
            if path == "/courses/10/files/42/download":
                return FakeResponse(b"pdf")
            raise AssertionError(path)

        client = CanvasClient("https://canvas.test", "token", transport=transport, retries=0)
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = collect_snapshot(
                client,
                {"term": {"key": "2026-fall", "start": "2026-08-01", "end": "2027-01-01"}, "extract_documents": False},
                temp_dir,
            )
        course = snapshot["courses"][0]
        self.assertEqual([item["id"] for item in course["files"]], [42])
        self.assertEqual(course["files"][0]["download_status"], "downloaded")
        self.assertEqual(course["pages"][0]["text"], "Thursday")

    def test_file_metadata_refresh_replaces_prior_json_archive(self):
        def transport(url, **kwargs):
            path = urlsplit(url).path
            if path == "/api/v1/users/self/profile":
                return FakeResponse({"id": 7, "name": "Student"})
            if path == "/api/v1/courses":
                return FakeResponse([{"id": 10, "name": "Physics I", "course_code": "PHY180H1", "term": {"name": "2026 Fall", "start_at": "2026-08-01", "end_at": "2027-01-01"}}])
            if path in {"/api/v1/announcements", "/api/v1/calendar_events", "/api/v1/courses/10/assignments", "/api/v1/courses/10/pages"}:
                return FakeResponse([])
            if path == "/api/v1/courses/10/modules":
                return FakeResponse([{"id": 4, "items": [{"id": 40, "type": "File", "content_id": 42, "title": "file-42"}]}])
            if path == "/api/v1/courses/10/modules/4/items":
                return FakeResponse([{"id": 40, "type": "File", "content_id": 42, "title": "file-42"}])
            if path == "/api/v1/courses/10/files":
                return FakeResponse([], status=403)
            if path == "/api/v1/files/42":
                return FakeResponse({"id": 42, "display_name": "slides.pdf", "filename": "slides.pdf", "content-type": "application/pdf", "url": "https://canvas.test/api/v1/files/42"})
            if path == "/courses/10/files/42/download":
                return FakeResponse(b"%PDF-1.7\n")
            raise AssertionError(path)

        client = CanvasClient("https://canvas.test", "token", transport=transport, retries=0)
        with tempfile.TemporaryDirectory() as temp_dir:
            from study_sync.archive import archive_bytes

            old = archive_bytes(b'{"id":42}', temp_dir, "file:42", "file-42", extract_documents=False)
            old["extraction_status"] = "unsupported"
            snapshot = collect_snapshot(
                client,
                {"term": {"key": "2026-fall", "start": "2026-08-01", "end": "2027-01-01"}, "extract_documents": False},
                temp_dir,
                previous={"courses": [{"id": 10, "files": [old]}]},
            )
            file_record = snapshot["courses"][0]["files"][0]
            self.assertEqual(file_record["display_name"], "slides.pdf")
            self.assertEqual(file_record["content_type"], "application/pdf")
            self.assertEqual(file_record["download_status"], "downloaded")
            self.assertTrue(Path(file_record["local_path"]).read_bytes().startswith(b"%PDF-"))

    def test_wiki_homepage_supplies_readings_and_external_notes_when_indexes_fail(self):
        from study_sync.projection import build_plan
        from study_sync.review import evidence_packet

        front = {"page_id": 71, "url": "esc103", "title": "ESC103", "front_page": True,
                 "body": '<a href="/courses/10/files/42">Pre-reading</a><a href="https://notes.example/notebook?section=1"><b>Lecture</b> Notes</a>'}
        for listed in (False, True):
            with self.subTest(homepage_already_in_index=listed), tempfile.TemporaryDirectory() as temp_dir:
                calls = []
                def transport(url, **kwargs):
                    self.assertEqual(urlsplit(url).hostname, "canvas.test")
                    path = urlsplit(url).path
                    calls.append(path)
                    if path.endswith("/front_page"):
                        return FakeResponse(front)
                    if path.endswith("/pages"):
                        return FakeResponse([{"page_id": 71, "url": "esc103"}] if listed else [], status=200 if listed else 404)
                    if path == "/api/v1/courses/10/files":
                        return FakeResponse([], status=403)
                    if path == "/api/v1/files/42":
                        return FakeResponse({"id": 42, "display_name": "PreReading.pdf", "content-type": "application/pdf", "url": "https://canvas.test/courses/10/files/42/download"})
                    if path == "/courses/10/files/42/download":
                        return FakeResponse(b"%PDF-1.7\n")
                    if path in {"/api/v1/announcements", "/api/v1/calendar_events", "/api/v1/courses/10/assignments", "/api/v1/courses/10/modules"}:
                        return FakeResponse([])
                    raise AssertionError(path)

                client = CanvasClient("https://canvas.test", "token", transport=transport, retries=0)
                term = {"key": "2026-fall", "start": "2026-08-01", "end": "2027-01-01"}
                course, _ = _collect_course(client, {"id": 10, "name": "ESC103", "course_code": "ESC103H1", "default_view": "wiki"}, "course", term, {"extract_documents": False}, temp_dir, None)
                self.assertEqual([p["page_id"] for p in course["pages"]], [71])
                self.assertTrue(Path(course["files"][0]["local_path"]).read_bytes().startswith(b"%PDF-"))
                self.assertEqual(course["resources"][0]["title"], "Lecture Notes")
                self.assertEqual(course["resources"][0]["download_status"], "link_only")
                self.assertEqual(calls.count("/api/v1/courses/10/front_page"), 1)
                snapshot = {"canvas_origin": client.base_url, "user": {"id": 7}, "term": term, "courses": [course]}
                plan = build_plan(snapshot)
                self.assertTrue(any(r["properties"].get("Type") == "external_link" for r in plan["records"]))
                packet = evidence_packet(snapshot)
                self.assertIn("ESC103", json.dumps(packet))
                self.assertIn(front["body"], json.dumps(packet, ensure_ascii=False).replace('\\"', '"'))

    def test_external_resource_index_rejects_unsafe_links_and_keeps_labels(self):
        refs = extract_external_resource_refs(
            '<a href="https://[broken">Bad</a><a href="javascript:alert(1)">Bad</a><a href="https://user:password@outside.test/a">Bad</a>'
            '<a href="/courses/10/pages/a">Internal</a><a href="https://notes.test/n?id=1">Lecture Notes</a>'
            '<a href="https://notes.test/n?id=1#section">Duplicate</a>',
            "https://canvas.test", "https://canvas.test/courses/10/pages/home")
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["title"], "Lecture Notes")
        self.assertEqual(refs[0]["source_url"], "https://notes.test/n?id=1")
        self.assertEqual(refs[0]["extraction_status"], "not_fetched")


if __name__ == "__main__":
    unittest.main()

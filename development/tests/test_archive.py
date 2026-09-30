from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from study_sync.archive import (
    archive_bytes,
    archive_file_path,
    download_and_archive,
    extract_document_text,
    safe_component,
)


class ByteClient:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.calls = []

    def get_bytes(self, url, max_bytes=None):
        self.calls.append((url, max_bytes))
        if self.error:
            raise self.error
        return self.value


class ArchiveTests(unittest.TestCase):
    def test_safe_paths_hash_and_duplicate_reuse(self):
        self.assertNotIn("/", safe_component("../notes/../../x"))
        with tempfile.TemporaryDirectory() as temp_dir:
            path = archive_file_path(temp_dir, "file:17", "../notes.pdf")
            self.assertTrue(str(path).startswith(str(Path(temp_dir))))
            client = ByteClient(b"hello")
            record = {"id": 17, "display_name": "notes.pdf", "url": "https://canvas.test/files/17"}
            first = download_and_archive(client, record, temp_dir, extract_documents=False)
            second = download_and_archive(client, record, temp_dir, extract_documents=False)
            self.assertEqual(first["sha256"], "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
            self.assertEqual(second["download_status"], "reused")
            self.assertEqual(len(client.calls), 1)

    def test_failed_refresh_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            good_client = ByteClient(b"old")
            record = {"id": 18, "display_name": "old.txt", "url": "https://canvas.test/files/18"}
            old = download_and_archive(good_client, record, temp_dir, extract_documents=False)
            old["warning"] = "download failed (CanvasOriginError)"
            bad_client = ByteClient(error=OSError("offline"))
            refreshed = download_and_archive(bad_client, {**record, "updated_at": "later"}, temp_dir, previous=old, extract_documents=False)
            self.assertEqual(refreshed["download_status"], "preserved")
            self.assertEqual(refreshed["local_path"], old["local_path"])
            self.assertIn("download failed", refreshed["warning"])
            self.assertNotEqual(refreshed["warning"], old["warning"])
            self.assertEqual(Path(old["local_path"]).read_bytes(), b"old")

    def test_missing_download_url_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old = download_and_archive(
                ByteClient(b"old"),
                {"id": 181, "display_name": "notes.txt", "url": "https://canvas.test/files/181/download"},
                temp_dir,
                extract_documents=False,
            )
            current = download_and_archive(
                ByteClient(error=OSError("offline")),
                {"id": 181, "display_name": "notes.txt"},
                temp_dir,
                previous=old,
                extract_documents=False,
            )
            self.assertEqual(current["download_status"], "preserved")
            self.assertEqual(current["local_path"], old["local_path"])
            self.assertEqual(Path(old["local_path"]).read_bytes(), b"old")

    def test_size_limit_does_not_write_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = archive_bytes(b"1234", temp_dir, "file:19", "large.bin", max_file_size=3)
            self.assertEqual(result["download_status"], "too_large")
            self.assertFalse((Path(temp_dir) / "files").exists())

    def test_declared_oversize_skips_network_and_preserves_prior(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old = download_and_archive(
                ByteClient(b"old"),
                {"id": 191, "display_name": "lecture.pdf", "url": "https://canvas.test/files/191/download"},
                temp_dir,
                extract_documents=False,
            )
            client = ByteClient(error=AssertionError("oversize metadata must prevent download"))
            result = download_and_archive(
                client,
                {
                    "id": 191,
                    "display_name": "lecture.pdf",
                    "url": "https://canvas.test/files/191/download",
                    "size": 100,
                },
                temp_dir,
                previous=old,
                max_file_size=3,
                extract_documents=False,
            )
            self.assertEqual(result["download_status"], "too_large")
            self.assertEqual(result["local_path"], old["local_path"])
            self.assertEqual(Path(old["local_path"]).read_bytes(), b"old")
            self.assertEqual(client.calls, [])

    def test_success_clears_prior_download_warning_for_download_and_reuse(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record = {
                "id": 192,
                "display_name": "lecture.txt",
                "url": "https://canvas.test/files/192/download",
                "updated_at": "2026-09-08T10:00:00Z",
                "size": 3,
            }
            first = download_and_archive(ByteClient(b"old"), record, temp_dir, extract_documents=False)
            prior_failed = dict(first, download_status="failed", warning="download failed (CanvasOriginError)")

            fresh = download_and_archive(
                ByteClient(b"new"),
                {**record, "updated_at": "2026-09-09T10:00:00Z"},
                temp_dir,
                previous=prior_failed,
                extract_documents=False,
            )
            self.assertEqual(fresh["download_status"], "downloaded")
            self.assertNotIn("warning", fresh)

            retry_client = ByteClient(error=OSError("offline"))
            reused = download_and_archive(
                retry_client,
                {**record, "updated_at": "2026-09-09T10:00:00Z"},
                temp_dir,
                previous=dict(fresh, download_status="failed", warning="download failed (CanvasOriginError)"),
                extract_documents=False,
            )
            self.assertEqual(reused["download_status"], "reused")
            self.assertNotIn("warning", reused)
            self.assertEqual(retry_client.calls, [])

    def test_extensionless_pdf_uses_mime_and_signature_and_safe_extension(self):
        with patch("study_sync.archive._extract_pdf", return_value=("PDF text", "extracted")):
            text, status = extract_document_text(b"%PDF-1.7\n", "file-42", "application/pdf")
            self.assertEqual((text, status), ("PDF text", "extracted"))
            self.assertEqual(extract_document_text(b"%PDF-1.7\n", "file-42")[1], "extracted")
            with tempfile.TemporaryDirectory() as temp_dir:
                record = archive_bytes(
                    b"%PDF-1.7\n",
                    temp_dir,
                    "file:42",
                    "file-42",
                    content_type="application/pdf",
                )
                self.assertEqual(record["extraction_status"], "extracted")
                self.assertTrue(record["local_path"].endswith(".pdf"))

    def test_prior_metadata_json_is_not_reused_as_document_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch("study_sync.archive._extract_pdf", return_value=("PDF text", "extracted")):
            old = archive_bytes(b'{"id": 42}', temp_dir, "file:42", "file-42", extract_documents=False)
            old.update({"extraction_status": "unsupported"})
            client = ByteClient(b"%PDF-1.7\n")
            refreshed = download_and_archive(
                client,
                {"id": 42, "display_name": "file-42", "content-type": "application/pdf", "url": "https://canvas.test/files/42"},
                temp_dir,
                previous=old,
            )
            self.assertEqual(refreshed["download_status"], "downloaded")
            self.assertEqual(refreshed["extraction_status"], "extracted")
            self.assertEqual(len(client.calls), 1)

    def test_updated_canvas_version_keeps_old_bytes_in_immutable_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_client = ByteClient(b"old")
            old = download_and_archive(
                old_client,
                {
                    "id": 23,
                    "display_name": "reading.txt",
                    "url": "https://canvas.test/files/23/download",
                    "updated_at": "2026-09-01T10:00:00Z",
                    "size": 3,
                },
                temp_dir,
                extract_documents=False,
            )
            new_client = ByteClient(b"new")
            current = download_and_archive(
                new_client,
                {
                    "id": 23,
                    "display_name": "reading.txt",
                    "url": "https://canvas.test/files/23/download",
                    "updated_at": "2026-09-02T10:00:00Z",
                    "size": 3,
                },
                temp_dir,
                previous=old,
                extract_documents=False,
            )
            self.assertEqual(old["download_status"], "downloaded")
            self.assertEqual(current["download_status"], "downloaded")
            self.assertNotEqual(old["local_path"], current["local_path"])
            self.assertEqual(Path(old["local_path"]).read_bytes(), b"old")
            self.assertEqual(Path(current["local_path"]).read_bytes(), b"new")
            self.assertNotEqual(old["version_token"], current["version_token"])

            # A same-version collection reuses the immutable current path and
            # does not need a network request.
            retry_client = ByteClient(error=OSError("offline"))
            reused = download_and_archive(
                retry_client,
                {
                    "id": 23,
                    "display_name": "reading.txt",
                    "url": "https://canvas.test/files/23/download",
                    "updated_at": "2026-09-02T10:00:00Z",
                    "size": 3,
                },
                temp_dir,
                previous=current,
                extract_documents=False,
            )
            self.assertEqual(reused["download_status"], "reused")
            self.assertEqual(reused["local_path"], current["local_path"])
            self.assertEqual(retry_client.calls, [])

    def test_missing_canvas_version_uses_content_hash_and_preserves_old_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old = download_and_archive(
                ByteClient(b"old"),
                {"id": 24, "display_name": "notes.txt", "url": "https://canvas.test/files/24/download"},
                temp_dir,
                extract_documents=False,
            )
            current = download_and_archive(
                ByteClient(b"new"),
                {"id": 24, "display_name": "notes.txt", "url": "https://canvas.test/files/24/download"},
                temp_dir,
                previous=old,
                extract_documents=False,
            )
            self.assertNotEqual(old["local_path"], current["local_path"])
            self.assertEqual(Path(old["local_path"]).read_bytes(), b"old")
            self.assertEqual(Path(current["local_path"]).read_bytes(), b"new")
            self.assertEqual(old["version_token"], old["sha256"][:20])
            self.assertEqual(current["version_token"], current["sha256"][:20])


if __name__ == "__main__":
    unittest.main()

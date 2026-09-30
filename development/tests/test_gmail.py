import base64
import unittest

from study_sync.gmail import build_queries, ingest_messages, review_packet, validate_reviews, build_supplement, _merge_record


def b64(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def mail(message_id="abc", body="Read chapter 2.", subject="PHY101 preparation"):
    return {"fetched_at": "2026-09-14T10:00:00Z", "message": {
        "id": message_id, "threadId": "thread-1", "payload": {
            "mimeType": "multipart/mixed", "headers": [
                {"name": "Subject", "value": subject}, {"name": "To", "value": "Student <student@school.test>"},
                {"name": "Message-ID", "value": "<mail-" + message_id + "@school.test>"},
                {"name": "Authentication-Results", "value": "not retained"},
            ],
            "parts": [{"partId": "0", "mimeType": "text/plain", "body": {"data": b64(body)}},
                      {"partId": "1", "mimeType": "application/pdf", "filename": "reading.pdf", "body": {"attachmentId": "attachment-1", "size": 12}}],
        }}}


def review(snapshot, classification="academic", actions=None):
    message = snapshot["messages"][0]
    return {"reviews": [{"gmail_id": message["gmail_id"], "source_hash": message["source_hash"],
                         "classification": classification, "reason": "Instructor course preparation.",
                         "actions": actions if actions is not None else [{"id": "reading", "title": "Read chapter 2", "source_refs": [message["source_key"]]}]}]}


class GmailTests(unittest.TestCase):
    def test_queries_are_complementary_and_overlap_last_success(self):
        config = {"gmail": {"start_date": "2026-09-01", "recipients": ["student@school.test"],
                            "school_domains": ["school.test"], "canvas_senders": ["notifications@canvas.test"], "course_codes": ["PHY101"]}}
        first = build_queries(config, now="2026-09-14T12:00:00Z")
        self.assertEqual([q["id"] for q in first["queries"]], ["recipients", "senders", "courses"])
        self.assertIn("cc:student@school.test", first["queries"][0]["query"])
        self.assertNotIn("to:", first["queries"][1]["query"])
        repeat = build_queries(config, {"last_successful_fetch_at": "2026-09-13T12:00:00Z"}, now="2026-09-14T12:00:00Z")
        self.assertEqual(repeat["window_start"], "2026-09-10T12:00:00Z")

    def test_mime_decoding_forwarded_recipient_attachment_and_cache(self):
        payload = mail(body="---------- Forwarded message ----------\nTo: <schoolbox@school.test>\n\nRead chapter 2.", subject="=?utf-8?b?6K++56iL6YCa55+l?=")
        snapshot = ingest_messages(None, [payload, payload], {"complete": True, "completed_at": "2026-09-14T11:00:00Z"})
        message = snapshot["messages"][0]
        self.assertEqual(message["subject"], "课程通知")
        self.assertEqual(message["forwarded_recipients"], ["schoolbox@school.test"])
        self.assertEqual(message["attachments"][0]["attachment_id"], "attachment-1")
        self.assertNotIn("authentication-results", message["headers"])
        self.assertEqual(snapshot["stats"]["messages"], 1)
        self.assertIn("last_successful_fetch_at", snapshot)
        second = ingest_messages(snapshot, [{"message": {"id": "abc", "labelIds": ["READ"]}}])
        self.assertEqual(second["messages"][0]["source_hash"], message["source_hash"])
        self.assertEqual(second["messages"][0]["body_text"], message["body_text"])
        self.assertTrue(second["messages"][0]["fetched_full"])

    def test_connector_snake_schema_decoded_body_and_attachment(self):
        payload = {"fetched_at": "2026-09-14T10:00:00Z", "message": {"id": "snake", "thread_id": "thread-snake", "payload": {
            "mime_type": "multipart/mixed", "headers": [{"name": "Subject", "value": "Already decoded"}], "parts": [
                {"part_id": "0", "mime_type": "text/plain", "parts": None, "body": {"content": "Read chapter 3.", "base64_url_content": None}},
                {"part_id": "1", "mime_type": "text/html", "parts": None, "body": {"base64_url_content": b64("<p>Read chapter 3.</p>")}},
                {"part_id": "2", "mime_type": "application/pdf", "filename": "chapter.pdf", "read_attachment_supported": True, "body": {"attachment_id": "att-snake", "size": 123}},
            ]}}}
        message = ingest_messages(None, [payload])["messages"][0]
        self.assertEqual(message["body_text"], "Read chapter 3.")
        self.assertEqual(message["thread_id"], "thread-snake")
        self.assertEqual(message["attachments"][0]["attachment_id"], "att-snake")
        self.assertTrue(message["attachments"][0]["read_attachment_supported"])

    def test_raw_rfc_multipart_and_html_only_have_readable_text(self):
        raw = "From: Teacher <teacher@school.test>\r\nTo: student@school.test\r\nSubject: Reading\r\nMIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=part\r\n\r\n--part\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\nRead=20chapter=202.\r\n--part\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Read <b>chapter 2</b>.</p>\r\n--part--\r\n"
        snapshot = ingest_messages(None, [{"id": "raw-1", "threadId": "t", "raw": b64(raw)}])
        self.assertIn("Read chapter 2.", snapshot["messages"][0]["body_text"])
        self.assertIn("<b>chapter 2</b>", snapshot["messages"][0]["body_html"])
        payload = mail()
        payload["message"]["payload"]["parts"] = [{"mimeType": "text/html", "body": {"data": b64("<p>Office hours<br>Friday</p>")}}]
        self.assertIn("Friday", ingest_messages(None, [payload])["messages"][0]["body_text"])

    def test_metadata_queue_and_incomplete_pagination_do_not_advance_cursor(self):
        snapshot = ingest_messages(None, [{"message": {"id": "new", "snippet": "Preview"}}], {"complete": True, "queries": [{"id": "one", "next_cursor": "page2"}]})
        self.assertEqual(snapshot["fetch_queue"], ["new"])
        self.assertFalse(snapshot["stats"]["search_complete"])
        self.assertNotIn("last_successful_fetch_at", snapshot)
        self.assertEqual(review_packet(snapshot)["messages"], [])
        metadata = ingest_messages(None, [], {"complete": True, "candidates": [{"id": "needed"}, {"id": "excluded"}], "excluded": [{"id": "excluded", "reason": "Outside requested scope."}]})
        self.assertEqual(metadata["fetch_queue"], ["needed"])
        self.assertNotIn("last_successful_fetch_at", metadata)

    def test_reviews_invalidate_changed_source_and_require_explicit_actions(self):
        snapshot = ingest_messages(None, [mail()])
        reviews = review(snapshot)
        accepted, warnings = validate_reviews(snapshot, reviews)
        self.assertEqual(len(accepted["reviews"]), 1)
        self.assertEqual(warnings, [])
        changed = ingest_messages(snapshot, [mail(body="Read chapter 3.")])
        accepted, warnings = validate_reviews(changed, reviews)
        self.assertFalse(accepted["reviews"])
        self.assertTrue(any(w["code"] == "source_hash_mismatch" for w in warnings))
        missing = review(snapshot, actions=[{"id": "reading", "title": "Read"}])
        self.assertFalse(validate_reviews(snapshot, missing)[0]["reviews"])

    def test_independent_actions_canvas_merge_alias_and_historical_retention(self):
        snapshot = ingest_messages(None, [mail()])
        source = snapshot["messages"][0]["source_key"]
        canvas = {"records": [{"source_key": "canvas:task:1", "kind": "tasks", "course_key": None,
                               "properties": {"Name": "Reading", "Source": "canvas:assignment:1", "date:Due:start": "2026-09-15"},
                               "generated_content": {}}]}
        actions = [{"id": "reading", "title": "Read chapter 2", "source_refs": [source], "canonical_task_key": "canvas:task:1", "due": "2026-09-16"},
                   {"id": "form", "title": "Submit consent", "source_refs": [source], "source_aliases": ["published-old-form"]}]
        legacy = {"records": [{"source_key": "published-old-form", "kind": "tasks", "properties": {"Name": "Consent", "Personal Notes": "keep"}, "generated_content": {}, "source_refs": [source]},
                              {"source_key": "unknown-old", "kind": "tasks", "properties": {"Name": "History"}, "generated_content": {}}]}
        result = build_supplement(snapshot, review(snapshot, actions=actions), canvas, legacy)
        self.assertEqual(len(result["requirements"]), 2)
        self.assertIn("canvas:task:1", result["overrides"])
        self.assertEqual(result["overrides"]["canvas:task:1"]["properties"]["Due Conflict"], "__YES__")
        keys = [r["source_key"] for r in result["records"]]
        self.assertIn("published-old-form", keys)
        self.assertNotIn("gmail:task:abc:form", keys)
        self.assertIn("unknown-old", keys)
        old = next(r for r in result["records"] if r["source_key"] == "published-old-form")
        self.assertEqual(old["properties"]["Personal Notes"], "keep")
        excluded = build_supplement(snapshot, review(snapshot, "out_of_scope", []), canvas, legacy)
        self.assertEqual(next(r for r in excluded["records"] if r["source_key"] == "published-old-form")["properties"]["Scope"], "Historical")
        self.assertNotIn("Scope", next(r for r in excluded["records"] if r["source_key"] == "unknown-old")["properties"])
        self.assertEqual(excluded["requirements"][0]["status"], "non_actionable")

    def test_due_semantics_and_existing_optional_scope(self):
        original = {"source_key": "old", "kind": "tasks", "properties": {"date:Due:start": "2026-09-15T10:00:00-04:00"}, "generated_content": {}}
        def update(due):
            return _merge_record(original, {"properties": {"date:Due:start": due, "Scope": "Optional"}, "generated_content": {}})
        same = update("2026-09-15T14:00:00Z")
        self.assertNotIn("Due Conflict", same["properties"])
        self.assertEqual(same["properties"]["Scope"], "Optional")
        self.assertEqual(update("2026-09-15T15:00:00Z")["properties"]["Due Conflict"], "__YES__")
        original["properties"]["date:Due:start"] = "2026-09-15"
        exact = update("2026-09-15T10:00:00-04:00")
        self.assertEqual(exact["properties"]["date:Due:start"], "2026-09-15T10:00:00-04:00")
        original["properties"]["Scope"] = "Academic"
        conflict = update("2026-09-15")
        self.assertEqual(conflict["properties"]["Scope"], "Academic")
        self.assertEqual(conflict["merge_warnings"][0]["code"], "scope_conflict")

    def test_canvas_announcement_alias_and_local_only_keep_source_ledger(self):
        snapshot = ingest_messages(None, [mail()])
        reviews = review(snapshot)
        reviews["reviews"][0]["canonical_announcement_key"] = "canvas-announcement"
        canvas = {"term": {"key": "2026-fall"}, "records": [{"source_key": "canvas-announcement", "kind": "announcements",
                  "properties": {"Name": "Official notice", "date:Posted:start": "2026-09-10"}, "generated_content": {"summary": "Official content"}}]}
        result = build_supplement(snapshot, reviews, canvas)
        self.assertIn("canvas-announcement", result["overrides"])
        self.assertFalse(any(r["kind"] == "announcements" for r in result["records"]))
        self.assertEqual(result["overrides"]["canvas-announcement"]["generated_content"]["summary"], "Official content")
        self.assertEqual(result["overrides"]["canvas-announcement"]["properties"]["date:Posted:start"], "2026-09-10")
        local = review(snapshot, "non_actionable", [])
        local["reviews"][0].update({"notion_visibility": "local_only", "visibility_reason": "Receipt already available in source ledger."})
        result = build_supplement(snapshot, local, {"records": []})
        self.assertEqual(result["records"], [])
        self.assertEqual(len(result["requirements"]), 1)

    def test_completion_evidence_and_resource_alias_do_not_create_extra_tasks(self):
        snapshot = ingest_messages(None, [mail()])
        reviews = review(snapshot, "non_actionable", [])
        reviews["reviews"][0].update({
            "notion_visibility": "local_only", "visibility_reason": "Receipt and resource are already represented.",
            "observations": [{"id": "online-passed", "status": "partial_completed", "task_keys": ["canvas-training"],
                              "source_refs": ["gmail:message:abc"], "summary": "Online portion passed.", "remaining_work": "Attend in-person portion."}],
            "resources": [{"id": "notes", "title": "Shared notes", "source_refs": ["gmail:message:abc"],
                           "canonical_resource_key": "canvas-notes", "url": "https://docs.example/notes"}],
        })
        canvas = {"term": {"key": "fall"}, "records": [
            {"kind": "tasks", "source_key": "canvas-training", "properties": {"Name": "Training"}, "generated_content": {}},
            {"kind": "resources", "source_key": "canvas-notes", "properties": {"Name": "Notes", "Source URL": "https://docs.example/notes"}, "generated_content": {}},
        ]}
        result = build_supplement(snapshot, reviews, canvas)
        self.assertEqual(result["records"], [])
        self.assertEqual(set(result["overrides"]), {"canvas-training", "canvas-notes"})
        training = result["overrides"]["canvas-training"]
        self.assertEqual(training["properties"]["Evidence Status"], "partial_completed")
        self.assertEqual(training["observations"][0]["remaining_work"], "Attend in-person portion.")
        self.assertNotIn("Done", training["properties"])
        self.assertEqual(result["overrides"]["canvas-notes"]["properties"]["Term"], "fall")


if __name__ == "__main__":
    unittest.main()

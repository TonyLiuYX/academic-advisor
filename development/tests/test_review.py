import copy
import sys
import unittest
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[2] / "skill/canvas-notion-study/scripts"
sys.path.insert(0, str(SCRIPT_ROOT))

from study_sync.projection import build_plan  # noqa: E402
from study_sync.review import evidence_packet, validate_enrichments  # noqa: E402


def snapshot_fixture():
    return {
        "schema_version": 1,
        "canvas_origin": "https://canvas.example.edu",
        "generated_at": "2026-09-08T12:00:00Z",
        "user": {"id": 77, "name": "Synthetic Student", "time_zone": "America/Toronto"},
        "term": {
            "key": "2026-fall",
            "label": "26fall",
            "start": "2026-08-01",
            "end": "2027-01-01",
            "timezone": "America/Toronto",
        },
        "courses": [
            {
                "id": 101,
                "name": "BIO101 Cell Biology",
                "course_code": "BIO101H1 F LEC0101",
                "mode": "course",
                "html_url": "https://canvas.example.edu/courses/101",
                "syllabus_body": "<h1>BIO101 syllabus</h1>\n<p>Labs require safety training.</p>",
                "version": "course-v1",
                "files": [
                    {
                        "id": 900,
                        "display_name": "BIO101 Syllabus and Basic Information.pdf",
                        "version": "file-v3",
                        "updated_at": "2026-09-01T12:00:00Z",
                        "extracted_text": "Syllabus file: grading and attendance rules.",
                        "download_status": "downloaded",
                        "local_path": "/private/synthetic/syllabus.pdf",
                        "source_url": "https://canvas.example.edu/files/900",
                    },
                    {
                        "id": 901,
                        "display_name": "Office Hours and Teaching Team.docx",
                        "version": "file-v2",
                        "extracted_text": "Professor Ada Lovelace; office hours Tuesday 14:00.",
                        "download_status": "reused",
                        "local_path": "/private/synthetic/team.docx",
                        "source_url": "https://canvas.example.edu/files/901",
                    },
                    {
                        "id": 902,
                        "display_name": "Week 1 lecture slides.pdf",
                        "version": "file-v8",
                        "extracted_text": "This is a lecture slide deck and should not be review evidence.",
                        "download_status": "downloaded",
                        "local_path": "/private/synthetic/slides.pdf",
                    },
                ],
                "pages": [
                    {
                        "page_id": "course-information",
                        "title": "Course Information and Requirements",
                        "version": "page-v4",
                        "body": "Basic information: read one chapter before each lab.",
                        "source_url": "https://canvas.example.edu/courses/101/pages/course-information",
                    },
                    {
                        "page_id": "week-1",
                        "title": "Week 1 lecture",
                        "version": "page-v1",
                        "body": "Lecture content that is not a syllabus or teaching-team source.",
                    },
                ],
                "calendar_events": [
                    {
                        "id": 3001,
                        "title": "Office hours begin",
                        "start_at": "2026-09-21T09:00:00-04:00",
                        "end_at": "2026-09-21T10:00:00-04:00",
                        "location": "BA 1130",
                        "description": "Weekly office hours begin this week.",
                        "updated_at": "calendar-v1",
                        "generated_at": "2026-09-08T12:00:00Z",
                    }
                ],
                "announcements": [
                    {
                        "id": 7001,
                        "title": "Lab consent form",
                        "message": "Submit the consent form before the first lab.\nBring photo ID.",
                        "posted_at": "2026-09-05T09:00:00Z",
                        "updated_at": "announcement-v1",
                        "html_url": "https://canvas.example.edu/courses/101/discussion_topics/7001",
                    },
                    {
                        "id": 7002,
                        "title": "Reading reminder",
                        "body": "Read chapter 2 before class.",
                        "posted_at": "2026-09-06T09:00:00Z",
                        "updated_at": "announcement-v2",
                        "html_url": "https://canvas.example.edu/courses/101/discussion_topics/7002",
                    },
                ],
            },
            {
                "id": 202,
                "name": "Student Success Hub",
                "course_code": "",
                "mode": "hub",
                "html_url": "https://canvas.example.edu/courses/202",
                "syllabus_body": None,
                "files": [],
                "pages": [],
                "announcements": [
                    {
                        "id": 8001,
                        "title": "Orientation checklist",
                        "message": "Complete the accessibility orientation this week.",
                        "updated_at": "announcement-v1",
                    }
                ],
            },
        ],
    }


class ReviewEvidenceTests(unittest.TestCase):
    def test_reply_full_text_and_pagination_status_are_in_evidence(self):
        snapshot = snapshot_fixture()
        course = snapshot["courses"][0]
        notice = course["announcements"][0]
        notice["replies_status"] = "stale"
        notice["replies"] = [{"id": 81, "message": "<p>Bring the signed consent form.</p>", "text": "Bring the signed consent form.",
                              "replies": [{"id": 82, "message": "Deadline moved to Thursday.", "coverage_status": "fresh"}]}]
        course["coverage"] = {"entries": [{"endpoint": "/api/v1/courses/101/discussion_topics/7001/entries", "status": "stale",
                                          "page_count": 2, "pagination_complete": False, "ids": ["81"]}]}
        packet = evidence_packet(snapshot)
        evidence = packet["announcements"]["7001"]
        self.assertEqual(evidence["replies"][0]["full_text"], notice["replies"][0]["message"])
        self.assertEqual(evidence["replies"][0]["replies"][0]["full_text"], "Deadline moved to Thursday.")
        self.assertEqual(evidence["reply_coverage"]["status"], "stale")
        self.assertEqual(evidence["reply_coverage"]["entries"][0]["page_count"], 2)
        self.assertFalse(evidence["reply_coverage"]["entries"][0]["pagination_complete"])

    def test_reply_change_invalidates_notice_and_course_without_hashing_run_status(self):
        snapshot = snapshot_fixture()
        notice = snapshot["courses"][0]["announcements"][0]
        before = evidence_packet(snapshot)
        notice["replies"] = []
        notice["replies_status"] = "fresh"
        self.assertEqual(evidence_packet(snapshot)["announcements"]["7001"]["source_hash"], before["announcements"]["7001"]["source_hash"])
        notice["replies"] = [{"id": 81, "message": "Submit on Wednesday.", "updated_at": "2026-09-01T12:00:00Z"}]
        reviewed = evidence_packet(snapshot)
        enrichments = {"announcements": {"7001": {"reviewed": True, "summary": "Wednesday", "source_hash": reviewed["announcements"]["7001"]["source_hash"]}},
                       "courses": {"101": {"reviewed": True, "summary": "Wednesday", "source_hash": reviewed["courses"]["101"]["source_hash"]}}}
        notice["replies"][0]["coverage_status"] = "stale"
        notice["replies"][0]["retrieved_at"] = "2026-09-02T12:00:00Z"
        notice["replies_status"] = "stale"
        self.assertEqual(evidence_packet(snapshot)["announcements"]["7001"]["source_hash"], reviewed["announcements"]["7001"]["source_hash"])
        notice["replies"][0]["message"] = "Submit on Thursday."
        validated, warnings = validate_enrichments(snapshot, enrichments)
        self.assertFalse(validated["announcements"]["7001"]["reviewed"])
        self.assertFalse(validated["courses"]["101"]["reviewed"])
        self.assertEqual({w["object_id"] for w in warnings}, {"101", "7001"})

    def test_packet_uses_canonical_dicts_and_covers_all_announcement_text(self):
        packet = evidence_packet(snapshot_fixture())
        self.assertEqual(set(packet), {"schema_version", "term", "courses", "announcements", "warnings"})
        self.assertEqual(set(packet["courses"]), {"101", "202"})
        self.assertEqual(set(packet["announcements"]), {"7001", "7002", "8001"})
        self.assertEqual(
            packet["announcements"]["7001"]["text"],
            "Submit the consent form before the first lab.\nBring photo ID.",
        )
        self.assertEqual(packet["announcements"]["7002"]["text"], "Read chapter 2 before class.")
        self.assertEqual(packet["courses"]["101"]["syllabus"]["text"], snapshot_fixture()["courses"][0]["syllabus_body"])
        calendar = packet["courses"]["101"]["calendar_events"]
        self.assertEqual(len(calendar), 1)
        self.assertEqual(calendar[0]["title"], "Office hours begin")
        self.assertEqual(calendar[0]["start_at"], "2026-09-21T09:00:00-04:00")
        self.assertEqual(calendar[0]["location"], "BA 1130")
        self.assertEqual(calendar[0]["updated_at"], "calendar-v1")
        self.assertNotIn("generated_at", calendar[0])

        document_titles = {item["title"] for item in packet["courses"]["101"]["documents"]}
        page_titles = {item["title"] for item in packet["courses"]["101"]["pages"]}
        self.assertEqual(document_titles, {"BIO101 Syllabus and Basic Information.pdf", "Office Hours and Teaching Team.docx"})
        self.assertEqual(page_titles, {"Course Information and Requirements"})
        self.assertNotIn("Week 1 lecture slides.pdf", document_titles)
        self.assertNotIn("Week 1 lecture", page_titles)

    def test_hash_excludes_run_and_archive_metadata_but_tracks_source_version(self):
        first = snapshot_fixture()
        baseline = evidence_packet(first)
        changed_run_metadata = copy.deepcopy(first)
        changed_run_metadata["generated_at"] = "2026-09-09T12:00:00Z"
        changed_run_metadata["courses"][0]["files"][0]["download_status"] = "reused"
        changed_run_metadata["courses"][0]["files"][0]["local_path"] = "/another/local/path.pdf"
        changed_run_metadata["courses"][0]["files"][0]["display_name"] = "renamed.pdf"
        changed_run_metadata["courses"][0]["files"][1]["download_status"] = "downloaded"
        changed_run_metadata["courses"][0]["files"][1]["local_path"] = "/another/team.docx"
        rerun = evidence_packet(changed_run_metadata)
        self.assertEqual(baseline["courses"]["101"]["source_hash"], rerun["courses"]["101"]["source_hash"])
        self.assertEqual(baseline["courses"]["101"]["documents"][0]["source_hash"], rerun["courses"]["101"]["documents"][0]["source_hash"])
        self.assertEqual(
            {item["id"] for item in baseline["courses"]["101"]["documents"]},
            {item["id"] for item in rerun["courses"]["101"]["documents"]},
        )
        self.assertEqual(baseline["announcements"]["7001"]["source_hash"], rerun["announcements"]["7001"]["source_hash"])

        changed_source = copy.deepcopy(first)
        changed_source["courses"][0]["announcements"][0]["message"] = "Updated consent instructions."
        changed_source["courses"][0]["announcements"][0]["updated_at"] = "announcement-v2"
        changed_source["courses"][0]["files"][0]["version"] = "file-v4"
        changed = evidence_packet(changed_source)
        self.assertNotEqual(baseline["announcements"]["7001"]["source_hash"], changed["announcements"]["7001"]["source_hash"])
        self.assertNotEqual(baseline["courses"]["101"]["source_hash"], changed["courses"]["101"]["source_hash"])

        changed_calendar_metadata = copy.deepcopy(first)
        changed_calendar_metadata["courses"][0]["calendar_events"][0]["generated_at"] = "2026-09-09T12:00:00Z"
        self.assertEqual(
            baseline["courses"]["101"]["source_hash"],
            evidence_packet(changed_calendar_metadata)["courses"]["101"]["source_hash"],
        )

    def test_calendar_change_invalidates_course_hash(self):
        first = snapshot_fixture()
        baseline = evidence_packet(first)
        changed = copy.deepcopy(first)
        changed["courses"][0]["calendar_events"][0]["description"] = "Office hours moved to a new room."
        changed["courses"][0]["calendar_events"][0]["updated_at"] = "calendar-v2"
        rerun = evidence_packet(changed)
        self.assertNotEqual(
            baseline["courses"]["101"]["calendar_events"][0]["source_hash"],
            rerun["courses"]["101"]["calendar_events"][0]["source_hash"],
        )
        self.assertNotEqual(baseline["courses"]["101"]["source_hash"], rerun["courses"]["101"]["source_hash"])

    def test_announcement_title_change_invalidates_even_when_body_is_unchanged(self):
        first = snapshot_fixture()
        baseline = evidence_packet(first)
        changed = copy.deepcopy(first)
        changed["courses"][0]["announcements"][0]["title"] = "Lab consent form — deadline changed"
        rerun = evidence_packet(changed)
        self.assertNotEqual(
            baseline["announcements"]["7001"]["source_hash"],
            rerun["announcements"]["7001"]["source_hash"],
        )
        enrichments = {
            "announcements": {
                "7001": {
                    "reviewed": True,
                    "source_hash": baseline["announcements"]["7001"]["source_hash"],
                    "summary": "Old title-aware review.",
                }
            }
        }
        filtered, warnings = validate_enrichments(changed, enrichments)
        self.assertFalse(filtered["announcements"]["7001"]["reviewed"])
        self.assertEqual(warnings[0]["code"], "enrichment_source_changed")

    def test_relevant_page_title_change_invalidates_course_even_when_body_is_unchanged(self):
        first = snapshot_fixture()
        baseline = evidence_packet(first)
        changed = copy.deepcopy(first)
        changed["courses"][0]["pages"][0]["title"] = "Course Information and Requirements — revised"
        rerun = evidence_packet(changed)
        self.assertNotEqual(
            baseline["courses"]["101"]["pages"][0]["source_hash"],
            rerun["courses"]["101"]["pages"][0]["source_hash"],
        )
        self.assertNotEqual(baseline["courses"]["101"]["source_hash"], rerun["courses"]["101"]["source_hash"])

    def test_validate_reuses_matching_review_and_invalidates_stale_or_missing_hash(self):
        snapshot = snapshot_fixture()
        packet = evidence_packet(snapshot)
        enrichments = {
            "courses": {
                "101": {
                    "reviewed": True,
                    "source_hash": packet["courses"]["101"]["source_hash"],
                    "syllabus_summary": "Reviewed and current.",
                    "important_info": ["Bring a calculator."],
                    "user_notes": "My private planning note.",
                },
                "202": {
                    "reviewed": True,
                    "syllabus_summary": "This must become pending because hash is absent.",
                },
            },
            "announcements": {
                "7001": {
                    "reviewed": True,
                    "source_hash": packet["announcements"]["7001"]["source_hash"],
                    "summary": "Current announcement.",
                    "action_items": [{"text": "Submit it"}],
                },
                "7002": {
                    "reviewed": True,
                    "source_hash": "stale-hash",
                    "summary": "Old summary must not survive.",
                    "action_items": [{"text": "Old action"}],
                },
            },
        }
        filtered, warnings = validate_enrichments(snapshot, enrichments)
        self.assertTrue(filtered["courses"]["101"]["reviewed"])
        self.assertEqual(filtered["courses"]["101"]["source_hash"], packet["courses"]["101"]["source_hash"])
        self.assertEqual(filtered["courses"]["101"]["user_notes"], "My private planning note.")
        self.assertFalse(filtered["courses"]["202"]["reviewed"])
        self.assertEqual(filtered["courses"]["202"]["status"], "pending_review")
        self.assertNotIn("syllabus_summary", filtered["courses"]["202"])
        self.assertFalse(filtered["announcements"]["7002"]["reviewed"])
        self.assertEqual(filtered["announcements"]["7002"]["status"], "pending_review")
        self.assertNotIn("summary", filtered["announcements"]["7002"])
        self.assertNotIn("action_items", filtered["announcements"]["7002"])
        self.assertEqual(
            {(warning["code"], warning["object_id"]) for warning in warnings},
            {
                ("enrichment_source_hash_missing", "202"),
                ("enrichment_source_changed", "7002"),
            },
        )

    def test_changed_course_source_invalidates_course_but_not_unrelated_announcement(self):
        snapshot = snapshot_fixture()
        packet = evidence_packet(snapshot)
        enrichments = {
            "courses": {"101": {"reviewed": True, "source_hash": packet["courses"]["101"]["source_hash"], "syllabus_summary": "Old course summary."}},
            "announcements": {"7001": {"reviewed": True, "source_hash": packet["announcements"]["7001"]["source_hash"], "summary": "Current notice."}},
        }
        changed = copy.deepcopy(snapshot)
        changed["courses"][0]["pages"][0]["body"] = "Changed requirements."
        filtered, warnings = validate_enrichments(changed, enrichments)
        self.assertFalse(filtered["courses"]["101"]["reviewed"])
        self.assertNotIn("syllabus_summary", filtered["courses"]["101"])
        self.assertTrue(filtered["announcements"]["7001"]["reviewed"])
        self.assertEqual([warning["code"] for warning in warnings], ["enrichment_source_changed"])
        self.assertEqual(warnings[0]["object_id"], "101")

    def test_changed_announcement_source_invalidates_course_and_announcement(self):
        snapshot = snapshot_fixture()
        packet = evidence_packet(snapshot)
        enrichments = {
            "courses": {
                "101": {
                    "reviewed": True,
                    "source_hash": packet["courses"]["101"]["source_hash"],
                    "syllabus_summary": "Current course summary.",
                }
            },
            "announcements": {
                "7001": {
                    "reviewed": True,
                    "source_hash": packet["announcements"]["7001"]["source_hash"],
                    "summary": "Old announcement summary.",
                }
            },
        }
        changed = copy.deepcopy(snapshot)
        changed["courses"][0]["announcements"][0]["message"] += " Updated wording."
        filtered, warnings = validate_enrichments(changed, enrichments)
        self.assertFalse(filtered["courses"]["101"]["reviewed"])
        self.assertNotIn("syllabus_summary", filtered["courses"]["101"])
        self.assertFalse(filtered["announcements"]["7001"]["reviewed"])
        self.assertNotIn("summary", filtered["announcements"]["7001"])
        self.assertEqual([warning["object_id"] for warning in warnings], ["101", "7001"])

    def test_stale_course_events_are_removed_and_not_projected(self):
        snapshot = snapshot_fixture()
        enrichments = {
            "courses": {
                "101": {
                    "reviewed": True,
                    "source_hash": "stale-course-hash",
                    "events": [
                        {
                            "id": "old-office-hours",
                            "title": "Old reviewed office hours",
                            "start_at": "2026-09-22T09:00:00-04:00",
                        }
                    ],
                }
            }
        }
        filtered, warnings = validate_enrichments(snapshot, enrichments)
        self.assertFalse(filtered["courses"]["101"]["reviewed"])
        self.assertNotIn("events", filtered["courses"]["101"])
        self.assertEqual(warnings[0]["code"], "enrichment_source_changed")

        plan = build_plan(snapshot, enrichments=filtered)
        reviewed_event_ids = {
            record.get("generated_content", {}).get("reviewed_event_id")
            for record in plan["records"]
            if record.get("kind") == "timetable"
        }
        self.assertNotIn("old-office-hours", reviewed_event_ids)

    def test_course_filter_limits_both_courses_and_announcements(self):
        packet = evidence_packet(snapshot_fixture(), course_id=101)
        self.assertEqual(set(packet["courses"]), {"101"})
        self.assertEqual(set(packet["announcements"]), {"7001", "7002"})

    def test_projection_trusted_api_accepts_source_hash_without_validation(self):
        snapshot = snapshot_fixture()
        packet = evidence_packet(snapshot)
        enrichments = {
            "courses": {"101": {"reviewed": True, "source_hash": packet["courses"]["101"]["source_hash"], "syllabus_summary": "Trusted API input."}},
            "announcements": {},
        }
        plan = build_plan(snapshot, enrichments=enrichments)
        course = next(record for record in plan["records"] if record["kind"] == "courses")
        self.assertEqual(course["generated_content"]["syllabus_summary"], "Trusted API input.")


if __name__ == "__main__":
    unittest.main()

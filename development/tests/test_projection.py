from __future__ import annotations

import copy
import pathlib
import sys
import unittest


SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[2] / "skill/canvas-notion-study/scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from study_sync.projection import (  # noqa: E402
    DATABASE_KINDS,
    NO,
    PENDING_MARKER,
    YES,
    build_plan,
    render_generated_content,
    tasks_to_ics,
)


def _fixture_snapshot() -> dict:
    return {
        "schema_version": 1,
        "canvas_origin": "https://canvas.example.edu/",
        "generated_at": "2026-09-08T12:00:00Z",
        "user": {"id": 7, "name": "Student", "time_zone": "America/Toronto"},
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
                "name": "Formal course",
                "course_code": "CIV101H1",
                "mode": "course",
                "html_url": "https://canvas.example.edu/courses/101",
                "syllabus_body": "<p>Raw syllabus body must not become a guessed summary.</p>",
                "assignments": [
                    {
                        "id": 1,
                        "name": "No due date",
                        "html_url": "https://canvas.example.edu/courses/101/assignments/1",
                        "workflow_state": "published",
                        "submission": {"workflow_state": "submitted"},
                        "text": "Original assignment instructions for task one.",
                    },
                    {
                        "id": 2,
                        "name": "Conflicting dates",
                        "description": '<p>Read the <a href="/courses/101/files/701/download">syllabus PDF</a>.</p>',
                        "text": "Original assignment instructions for task two.",
                        "due_at": "2026-09-10T17:00:00-04:00",
                        "all_dates": [
                            {"due_at": "2026-09-10T17:00:00-04:00"},
                            {"due_at": "2026-09-11T17:00:00-04:00"},
                        ],
                        "attachments": [
                            {
                                "id": 501,
                                "display_name": "worksheet.pdf",
                                "version": 2,
                                "local_path": "/archive/501-v2.pdf",
                                "download_status": "downloaded",
                            }
                        ],
                    },
                ],
                "files": [
                    {
                        "id": 701,
                        "display_name": "syllabus.pdf",
                        "version": 1,
                        "local_path": "/archive/701-v1.pdf",
                        "sha256": "abc",
                    },
                    {
                        "id": 701,
                        "display_name": "syllabus.pdf",
                        "version": 2,
                        "local_path": "/archive/701-v2.pdf",
                        "sha256": "def",
                    },
                ],
                "pages": [
                    {"id": 601, "title": "Course rules", "text": "Original Canvas page text."},
                ],
                "announcements": [
                    {
                        "id": 301,
                        "title": "Reviewed notice",
                        "posted_at": "2026-09-08T09:00:00Z",
                        "html_url": "https://canvas.example.edu/announcements/301",
                    }
                ],
                "calendar_events": [
                    {
                        "id": 801,
                        "title": "Lecture",
                        "start_at": "2026-09-09T10:00:00-04:00",
                        "end_at": "2026-09-09T11:00:00-04:00",
                        "location_name": "Room 101",
                        "description": "<p>Bring the lab manual.</p>",
                    }
                ],
            },
            {
                "id": 900,
                "name": "Student Hub",
                "mode": "hub",
                "html_url": "https://canvas.example.edu/courses/900",
                "assignments": [{"id": 902, "name": "Hub task"}],
                "announcements": [{"id": 901, "title": "Hub notice"}],
            },
            {
                "id": 999,
                "name": "Ignored past course",
                "mode": "ignore",
                "assignments": [{"id": 998, "name": "Must not appear"}],
            },
        ],
    }


def _records(plan: dict, kind: str) -> list[dict]:
    return [record for record in plan["records"] if record["kind"] == kind]


def _by_canvas_id(plan: dict, kind: str, canvas_id: str) -> dict:
    for record in _records(plan, kind):
        if record["properties"].get("Canvas ID") == canvas_id or record["properties"].get("Assignment ID") == canvas_id or record["properties"].get("Announcement ID") == canvas_id:
            return record
    raise AssertionError(f"No {kind} record for {canvas_id}")


class ProjectionTests(unittest.TestCase):
    def test_formal_only_hub_retention_and_assignment_semantics(self) -> None:
        enrichment = {
            "courses": {
                "101": {
                    "reviewed": True,
                    "syllabus_summary": "Reviewed syllabus summary.",
                    "important_info": ["Office hours are Thursdays."],
                    "user_notes": "Keep my private note separate.",
                }
            },
            "announcements": {
                "301": {
                    "reviewed": True,
                    "summary": "Submit the worksheet.",
                    "action_items": [{"text": "Submit worksheet", "assignment_id": "2", "due_date": "2026-09-12"}],
                    "user_notes": "Private announcement note.",
                }
            },
        }
        plan = build_plan(_fixture_snapshot(), enrichment)

        course = _records(plan, "courses")[0]
        self.assertEqual([record["properties"]["Canvas ID"] for record in _records(plan, "courses")], ["101"])

        tasks = _records(plan, "tasks")
        assignment_task = _by_canvas_id(plan, "tasks", "1")
        conflict_task = _by_canvas_id(plan, "tasks", "2")
        hub_task = _by_canvas_id(plan, "tasks", "902")
        self.assertEqual(len(tasks), 3, "the reviewed action linked to assignment 2 must not duplicate it")
        self.assertNotIn("Due", assignment_task["properties"])
        self.assertEqual(assignment_task["properties"]["Has Attachments"], NO)
        self.assertEqual(assignment_task["properties"]["Canvas Status"], "submitted")
        self.assertEqual(
            assignment_task["properties"]["Course"],
            ["https://canvas.example.edu|user=7|type=course|id=101"],
        )
        self.assertEqual(conflict_task["properties"]["Due Conflict"], YES)
        self.assertNotIn("Due", conflict_task["properties"])
        self.assertNotIn("date:Due:start", conflict_task["properties"])
        self.assertIn("Submit worksheet", conflict_task["generated_content"]["reviewed_actions"])
        self.assertIn("assignment_original", conflict_task["generated_content"])
        self.assertEqual(conflict_task["properties"]["Has Attachments"], YES)
        self.assertTrue(any("|id=701|version=1" in key for key in conflict_task["properties"]["Resources"]))
        self.assertFalse(
            any(
                record["properties"].get("Canvas ID") == "701" and record["properties"].get("Type") == "attachment"
                for record in _records(plan, "resources")
            )
        )
        self.assertIn(
            next(record for record in _records(plan, "announcements") if record["properties"]["Canvas ID"] == "301")["source_key"],
            conflict_task["properties"]["Source"],
        )
        self.assertNotIn("Course", hub_task["properties"])
        self.assertEqual(hub_task["properties"]["Source Space"], "Student Hub")
        self.assertTrue(all("Done" not in record["properties"] and "Planned" not in record["properties"] for record in tasks))

        hub_announcement = _by_canvas_id(plan, "announcements", "901")
        self.assertEqual(hub_announcement["generated_content"]["summary"], PENDING_MARKER)
        self.assertEqual(hub_announcement["generated_content"]["summary_status"], "pending_review")
        self.assertEqual(hub_announcement["properties"]["Sync Status"], "pending_review")

        formal_announcement = _by_canvas_id(plan, "announcements", "301")
        self.assertEqual(formal_announcement["properties"]["date:Posted:is_datetime"], 1)

        resources = _records(plan, "resources")
        resource_versions = {record["properties"].get("Version") for record in resources if record["properties"].get("Canvas ID") == "701"}
        self.assertEqual(resource_versions, {"1", "2"})
        attachment = _by_canvas_id(plan, "resources", "501")
        self.assertEqual(attachment["properties"]["Local Path"], "/archive/501-v2.pdf")
        self.assertEqual(attachment["properties"]["Type"], "attachment")
        page = _by_canvas_id(plan, "resources", "601")
        self.assertEqual(page["generated_content"]["page_original"], "Original Canvas page text.")

        self.assertEqual(
            _records(plan, "resources")[0]["properties"]["Course"],
            ["https://canvas.example.edu|user=7|type=course|id=101"],
        )

        syllabus_note = _records(plan, "notes")[0]
        self.assertEqual(syllabus_note["generated_content"]["syllabus_summary"], "Reviewed syllabus summary.")
        self.assertEqual(syllabus_note["user_content"]["user_notes"], "Keep my private note separate.")
        rendered_note = render_generated_content(syllabus_note)
        self.assertIn("大纲摘要", rendered_note)
        self.assertNotIn("source_key", rendered_note)

        rendered_task = render_generated_content(conflict_task)
        self.assertIn("- Conflicting dates", rendered_task)
        self.assertIn("完成请使用 Notion 的 Done 属性", rendered_task)
        self.assertIn("作业原文", rendered_task)
        self.assertIn("日期冲突", rendered_task)

        timetable = _records(plan, "timetable")[0]
        rendered_event = render_generated_content(timetable)
        self.assertIn("Room 101", rendered_event)
        self.assertIn("Bring the lab manual", rendered_event)
        self.assertIn("America/Toronto", timetable["display_timezone"])

    def test_pending_enrichment_does_not_turn_raw_text_into_summary(self) -> None:
        snapshot = _fixture_snapshot()
        snapshot["courses"][0]["announcements"][0]["message"] = "A very long raw Canvas message."
        plan = build_plan(snapshot, {"announcements": {"301": {"summary": "unreviewed text"}}})
        announcement = _by_canvas_id(plan, "announcements", "301")
        self.assertEqual(announcement["generated_content"]["summary"], PENDING_MARKER)
        self.assertEqual(announcement["generated_content"]["action_items"], [PENDING_MARKER])
        self.assertEqual(len(_records(plan, "tasks")), 3, "there must be no inferred action task")

        course = _records(plan, "courses")[0]
        self.assertEqual(course["generated_content"]["syllabus_summary"], PENDING_MARKER)
        self.assertNotIn("Raw syllabus body", course["generated_content"]["syllabus_summary"])

        # all_dates can contain group/section alternatives; the effective
        # due_at remains the only date when no reviewed announcement disagrees.
        assignment = _by_canvas_id(plan, "tasks", "2")
        self.assertEqual(assignment["properties"]["Due Conflict"], NO)
        self.assertEqual(assignment["properties"]["date:Due:start"], "2026-09-10T17:00:00-04:00")
        self.assertEqual(assignment["properties"]["date:Due:is_datetime"], 1)

    def test_rerun_stability_schema_views_and_comparison(self) -> None:
        snapshot = _fixture_snapshot()
        enrichment = {
            "reviewed": True,
            "courses": {"101": {"syllabus_summary": "S"}},
            "announcements": {
                "901": {
                    "summary": "Hub action",
                    "action_items": [{"text": "Attend hub event", "due_date": "2026-10-02"}],
                }
            },
        }
        first = build_plan(snapshot, enrichment)
        second = build_plan(copy.deepcopy(snapshot), copy.deepcopy(enrichment))
        self.assertEqual(first, second)
        self.assertEqual(set(first["databases"]), set(DATABASE_KINDS))
        self.assertIn("cross_course_source_index", first["views"])
        self.assertIn("term_filtered", first["views"])
        self.assertEqual(first["comparison"]["record_keys"], sorted(first["comparison"]["record_keys"]))
        self.assertTrue(first["comparison"]["plan_hash_sha256"])

        changed_timestamp = copy.deepcopy(snapshot)
        changed_timestamp["generated_at"] = "2026-09-08T12:05:00Z"
        changed = build_plan(changed_timestamp, enrichment)
        self.assertEqual(first["comparison"]["plan_hash_sha256"], changed["comparison"]["plan_hash_sha256"])

        ics = tasks_to_ics(first)
        self.assertIn("SUMMARY:Attend hub event", ics)
        self.assertIn("DTSTART;VALUE=DATE:20261002", ics)
        hub_action = next(record for record in _records(first, "tasks") if record["properties"].get("Announcement ID") == "901")
        self.assertEqual(hub_action["properties"]["date:Due:is_datetime"], 0)
        self.assertIn("SUMMARY:Conflicting dates", ics)

    def test_source_keys_include_origin_user_type_id_and_version(self) -> None:
        plan = build_plan(_fixture_snapshot())
        task = _by_canvas_id(plan, "tasks", "1")
        self.assertEqual(
            task["source_key"],
            "https://canvas.example.edu|user=7|type=assignment|id=1",
        )
        versioned = [record for record in _records(plan, "resources") if record["properties"].get("Canvas ID") == "701"]
        self.assertTrue(all("|version=" in record["source_key"] for record in versioned))

    def test_course_title_code_and_syllabus_resource_view_fields(self) -> None:
        snapshot = _fixture_snapshot()
        snapshot["courses"][0]["name"] = "CIV101H1 F LEC0101: Introduction to Civil Systems"
        snapshot["courses"][0]["course_code"] = "CIV101H1 F LEC0101"
        plan = build_plan(snapshot)
        course = _records(plan, "courses")[0]["properties"]
        self.assertEqual(course["Name"], "Introduction to Civil Systems")
        self.assertEqual(course["Course Code"], "CIV101H1")
        syllabus = [record for record in _records(plan, "resources") if record["properties"].get("Canvas ID") == "701"]
        self.assertTrue(all(record["properties"]["Type"] == "syllabus" for record in syllabus))
        self.assertTrue(all(record["properties"]["Syllabus"] == YES for record in syllabus))

    def test_ics_uses_explicit_utc_and_handles_dst_without_guessing(self) -> None:
        records = []
        for index, due in enumerate(
            (
                "2026-09-14T23:59:00-04:00",
                "2026-11-01T01:30:00-04:00",
                "2026-11-01T01:30:00-05:00",
            ),
            start=1,
        ):
            records.append(
                {
                    "kind": "tasks",
                    "source_key": f"task-{index}",
                    "display_timezone": "America/Toronto",
                    "properties": {
                        "Name": f"Deadline {index}",
                        "Source Key": f"task-{index}",
                        "date:Due:start": due,
                        "Due Conflict": NO,
                    },
                    "generated_content": {},
                }
            )
        records.append(
            {
                "kind": "tasks",
                "source_key": "naive-without-zone",
                "properties": {
                    "Name": "Unknown local time",
                    "Source Key": "naive-without-zone",
                    "date:Due:start": "2026-09-14T23:59:00",
                    "Due Conflict": NO,
                },
                "generated_content": {},
            }
        )
        ics = tasks_to_ics({"records": records})
        self.assertIn("DTSTART:20260915T035900Z", ics)
        self.assertIn("DTSTART:20261101T053000Z", ics)
        self.assertIn("DTSTART:20261101T063000Z", ics)
        self.assertNotIn("Unknown local time", ics)

    def test_module_pdf_marks_syllabus_available_without_body(self) -> None:
        snapshot = _fixture_snapshot()
        course = snapshot["courses"][0]
        course["syllabus_body"] = ""
        course["files"] = []
        course["pages"] = []
        course["modules"] = [{"items": [{"type": "File", "title": "Course Outline.pdf", "content_id": 703}]}]
        plan = build_plan(snapshot)
        course_record = _records(plan, "courses")[0]
        self.assertEqual(course_record["properties"]["Syllabus Available"], YES)
        self.assertEqual(_records(plan, "notes")[0]["generated_content"]["syllabus_summary"], PENDING_MARKER)

    def test_reviewed_course_actions_and_events_fill_canvas_gaps(self) -> None:
        snapshot = _fixture_snapshot()
        enrichment = {
            "courses": {
                "101": {
                    "reviewed": True,
                    "actions": [
                        {
                            "id": "exam-1",
                            "text": "复习期中考试",
                            "type": "exam",
                            "due_date": "2026-10-01",
                            "source_url": "https://canvas.example.edu/files/esc101.pdf",
                            "source_ref": "ESC101_20269_Syllabus.pdf p.2",
                        },
                        {
                            "id": "linked-2",
                            "text": "按公告要求准备作业二",
                            "type": "assignment_followup",
                            "assignment_id": "2",
                            "due_date": "2026-09-12",
                            "source_url": "https://canvas.example.edu/files/esc101.pdf",
                            "source_ref": "ESC101_20269_Syllabus.pdf p.3",
                        },
                    ],
                    "events": [
                        {
                            "id": "slides-1",
                            "title": "考试复习讲座",
                            "start_at": "2026-09-14",
                            "all_day": True,
                            "location_name": "ESCL Classroom",
                            "source_url": "https://canvas.example.edu/files/esc101.pdf",
                            "source_ref": "ESC101_20269_Course_Outline.pdf p.4",
                        }
                    ],
                }
            }
        }
        plan = build_plan(snapshot, enrichment)

        standalone = next(
            record
            for record in _records(plan, "tasks")
            if record["properties"].get("Type") == "exam"
        )
        self.assertEqual(
            standalone["source_key"],
            "https://canvas.example.edu|user=7|type=course_action|id=101:exam-1",
        )
        self.assertEqual(standalone["properties"]["date:Due:start"], "2026-10-01")
        self.assertEqual(standalone["properties"]["date:Due:is_datetime"], 0)
        self.assertEqual(standalone["properties"]["Source URL"], "https://canvas.example.edu/files/esc101.pdf")
        rendered_standalone = render_generated_content(standalone)
        self.assertIn("ESC101_20269_Syllabus.pdf p.2", rendered_standalone)
        self.assertIn("https://canvas.example.edu/files/esc101.pdf", rendered_standalone)

        merged = _by_canvas_id(plan, "tasks", "2")
        self.assertIn(
            "https://canvas.example.edu|user=7|type=course_action|id=101:linked-2",
            merged["properties"]["Source"],
        )
        self.assertEqual(merged["properties"]["Due Conflict"], YES)
        self.assertNotIn("date:Due:start", merged["properties"])
        self.assertIn("按公告要求准备作业二", merged["generated_content"]["reviewed_actions"])
        self.assertEqual(
            merged["generated_content"]["reviewed_action_sources"][0]["source_ref"],
            "ESC101_20269_Syllabus.pdf p.3",
        )
        rendered_merged = render_generated_content(merged)
        self.assertIn("ESC101_20269_Syllabus.pdf p.3", rendered_merged)
        self.assertIn("日期冲突", rendered_merged)

        reviewed_event = next(
            record
            for record in _records(plan, "timetable")
            if record["properties"].get("Type") == "reviewed_event"
        )
        self.assertEqual(
            reviewed_event["source_key"],
            "https://canvas.example.edu|user=7|type=reviewed_event|id=101:slides-1",
        )
        self.assertEqual(reviewed_event["properties"]["date:Start:start"], "2026-09-14")
        self.assertEqual(reviewed_event["properties"]["date:Start:is_datetime"], 0)
        self.assertEqual(reviewed_event["properties"]["All Day"], YES)
        rendered_event = render_generated_content(reviewed_event)
        self.assertIn("ESCL Classroom", rendered_event)
        self.assertIn("ESC101_20269_Course_Outline.pdf p.4", rendered_event)
        self.assertIn("https://canvas.example.edu/files/esc101.pdf", rendered_event)

        unreviewed = build_plan(
            snapshot,
            {
                "courses": {
                    "101": {
                        "reviewed": False,
                        "actions": enrichment["courses"]["101"]["actions"],
                        "events": enrichment["courses"]["101"]["events"],
                    }
                }
            },
        )
        self.assertFalse(any("course_action" in record["source_key"] for record in _records(unreviewed, "tasks")))
        self.assertFalse(any("reviewed_event" in record["source_key"] for record in _records(unreviewed, "timetable")))

    def test_renderer_uses_course_limit_and_keeps_source_link_after_excerpt(self) -> None:
        snapshot = _fixture_snapshot()
        long_info = [f"重要信息 {index}: " + ("内容 " * 260) for index in range(10)]
        snapshot["courses"][0]["pages"][0]["text"] = "原始页面 " * 700
        plan = build_plan(
            snapshot,
            {
                "courses": {
                    "101": {
                        "reviewed": True,
                        "syllabus_summary": "课程摘要",
                        "important_info": long_info,
                    }
                }
            },
        )
        course = _records(plan, "courses")[0]
        rendered_course = render_generated_content(course)
        self.assertIn("重要信息", rendered_course)
        self.assertIn("原文过长，以上为节选", rendered_course)
        self.assertIn("来源：[Canvas 页面](https://canvas.example.edu/courses/101)", rendered_course)

        page = _by_canvas_id(plan, "resources", "601")
        rendered_page = render_generated_content(page)
        self.assertIn("页面原文（节选）", rendered_page)

    def test_resource_source_text_preserves_math_numbering_and_bullets(self) -> None:
        original = "2. Compute |12−5i|.\n3. Verify |z| = 13.\n4. Keep this question number.\n• Bring the worksheet."
        for field in ("extracted_text", "page_original", "resource_original"):
            with self.subTest(field=field):
                record = {"kind": "resources", "properties": {"Name": "Source worksheet", "Source URL": "https://canvas.example.edu/files/7"},
                          "generated_content": {field: original}}
                rendered = render_generated_content(record)
                self.assertIn("### 资源资料\nSource worksheet", rendered)
                self.assertIn("```text\n" + original + "\n```", rendered)
                self.assertTrue(rendered.endswith("来源：[Canvas 页面](https://canvas.example.edu/files/7)"))
                self.assertEqual(record["generated_content"][field], original)
        task = {"kind": "tasks", "properties": {"Name": "Existing task"}, "generated_content": {"assignment_original": original}}
        self.assertNotIn("```text", render_generated_content(task))

    def test_resource_source_fences_are_literal_and_cannot_close_outer_block(self) -> None:
        original = "2. Keep the following source example:\n```python\nx = abs(12-5j)\n```\n4. Then evaluate |x|."
        record = {"kind": "resources", "properties": {"Name": "Code example"}, "generated_content": {"extracted_text": original}}
        rendered = render_generated_content(record)
        self.assertIn("````text\n" + original + "\n````", rendered)
        self.assertNotIn("\\|", rendered)
        self.assertNotIn("\\`", rendered)

    def test_resource_excerpt_closes_literal_block_before_notice_and_source(self) -> None:
        original = "2. Evaluate |12−5i|; retain this raw line.\n" * 200
        record = {"kind": "resources", "properties": {"Name": "Long source", "Source URL": "https://canvas.example.edu/files/8"},
                  "generated_content": {"extracted_text": original}}
        rendered = render_generated_content(record)
        self.assertIn("### 资源原文（节选）", rendered)
        self.assertIn("```text\n2. Evaluate |12−5i|", rendered)
        self.assertEqual(rendered.count("```"), 2)
        self.assertIn("\n```\n> 原文过长，以上为节选", rendered)
        self.assertTrue(rendered.endswith("来源：[Canvas 页面](https://canvas.example.edu/files/8)"))
        self.assertEqual(record["generated_content"]["extracted_text"], original)

    def test_resource_version_token_and_archive_fallback_are_stable(self) -> None:
        snapshot = _fixture_snapshot()
        course = snapshot["courses"][0]
        course["files"].extend(
            [
                {
                    "source_id": "702",
                    "filename": "lecture-slides.pdf",
                    "version_token": "archive-v7",
                    "version": 99,
                    "updated_at": "2026-09-08T14:00:00Z",
                    "size": 12345,
                },
                {
                    "id": 703,
                    "display_name": "reading.pdf",
                    "updated_at": "2026-09-08T15:00:00Z",
                    "size": 67890,
                },
            ]
        )
        course["assignments"][0]["attachments"] = [
            {
                "id": 703,
                "display_name": "reading.pdf",
                "updated_at": "2026-09-08T15:00:00Z",
                "size": 67890,
            }
        ]
        plan = build_plan(snapshot)
        versioned = _by_canvas_id(plan, "resources", "702")
        self.assertEqual(versioned["properties"]["Version"], "archive-v7")
        self.assertIn("|version=archive-v7", versioned["source_key"])

        fallback_records = [
            record for record in _records(plan, "resources") if record["properties"].get("Canvas ID") == "703"
        ]
        self.assertEqual(len(fallback_records), 1, "attachment and course file should share the derived revision key")
        fallback = fallback_records[0]
        self.assertTrue(fallback["properties"]["Version"].startswith("hash-"))
        self.assertIn("|version=hash-", fallback["source_key"])

        rerun = build_plan(copy.deepcopy(snapshot))
        self.assertEqual(plan, rerun)

    def test_basic_information_and_separator_variants_are_syllabus_resources(self) -> None:
        snapshot = _fixture_snapshot()
        course = snapshot["courses"][0]
        course["syllabus_body"] = ""
        course["files"] = [
            {"id": 704, "display_name": "ESC194F_Basic_Information_2026.pdf"},
            {"id": 705, "display_name": "ESC194F-Course_Outline-2026.pdf"},
        ]
        course["pages"] = []
        plan = build_plan(snapshot)
        course_record = _records(plan, "courses")[0]
        self.assertEqual(course_record["properties"]["Syllabus Available"], YES)
        syllabus_records = {
            record["properties"]["Canvas ID"]: record
            for record in _records(plan, "resources")
            if record["properties"].get("Canvas ID") in {"704", "705"}
        }
        self.assertEqual(set(syllabus_records), {"704", "705"})
        self.assertTrue(all(record["properties"]["Type"] == "syllabus" for record in syllabus_records.values()))
        self.assertTrue(all(record["properties"]["Syllabus"] == YES for record in syllabus_records.values()))


if __name__ == "__main__":
    unittest.main()

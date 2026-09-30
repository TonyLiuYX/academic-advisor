"""Ordinary forward use: personal readback -> week -> sessions -> next chat."""
from copy import deepcopy
import json
import re
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill/canvas-notion-study/scripts"))
from study_sync.learner import empty_learner, import_personal
from study_sync.planning import build_week_plan, study_start, study_finish, mentor_records, task_candidates
from study_sync.context import build_context

NOW = "2026-09-14T19:00:00-04:00"


def fixture():
    plan = {"term": {"key": "synthetic-26fall"}, "timezone": "America/Toronto", "records": [
        {"kind": "courses", "source_key": "https://canvas.test|user=7|type=course|id=1", "properties": {"Name": "Sample Course"}},
        {"kind": "resources", "source_key": "resource:1", "properties": {"Name": "Quiz material", "Source URL": "https://canvas.test/file/1"}},
    ]}
    for key, title, due in (("quiz", "Tuesday Quiz", "2026-09-15"), ("homework", "Friday Homework", "2026-09-18"), ("form", "Undated consent", None)):
        plan["records"].append({"kind": "tasks", "source_key": key, "properties": {
            "Name": title, "Source": key, "Source URL": "https://canvas.test/" + key, "Resources": ["resource:1"],
            "date:Due:start": due, "Canvas Status": "unsubmitted", "Type": "quiz" if key == "quiz" else "assignment"}})
    learner = empty_learner()
    learner["profile"] = {"timezone": "America/Toronto", "preferences": "Short study blocks"}
    return plan, learner


class MentorWorkflowTests(unittest.TestCase):
    def test_personal_readback_preserves_missing_and_explicitly_clears(self):
        plan, learner = fixture()
        learner["personal"]["quiz"] = {"Done": True, "Priority": "High", "Personal Notes": "Keep my notes"}
        updated, report = import_personal(learner, {"results": [{"source_key": "quiz", "properties": {
            "Done": {"type": "checkbox", "checkbox": False}, "Planned": {"type": "date", "date": None}}}], "has_more": False}, now=NOW)
        self.assertFalse(updated["personal"]["quiz"]["Done"])
        self.assertEqual(updated["personal"]["quiz"]["Personal Notes"], "Keep my notes")
        self.assertIsNone(updated["personal"]["quiz"]["Planned"])
        self.assertTrue(report["complete"])
        cleared, _ = import_personal(updated, [{"source_key": "quiz", "properties": {"Personal Notes": ""}}], now=NOW)
        self.assertEqual(cleared["personal"]["quiz"]["Personal Notes"], "")
        self.assertEqual(learner["personal"]["quiz"]["Done"], True)

    def test_notion_wrapper_markdown_identity_and_partial_readback(self):
        key = "https://canvas.test|user=7|type=assignment|id=1"
        page = "0123456789abcdef0123456789abcdef"
        payload = {"content": [{"type": "text", "text": json.dumps({"results": [{
            "url": "https://www.notion.so/" + page, "properties": {"Source Key": "[https://canvas.test\\|user=7\\|type=assignment\\|id=1](" + key + ")", "Done": "__YES__"}}], "has_more": True})}]}
        updated, report = import_personal(None, payload, {"records": {key: {"page_id": "01234567-89ab-cdef-0123-456789abcdef"}}}, now=NOW)
        self.assertTrue(updated["personal"][key]["Done"])
        self.assertFalse(report["complete"])
        updated, report = import_personal(updated, [{"url": "https://notion.so/"+page, "properties": {"Priority": "Low"}}], {"records": {key: {"page_id": page}}}, now=NOW)
        self.assertEqual(updated["personal"][key]["Priority"], "Low")

    def test_partially_autolinked_mail_source_key_keeps_suffix(self):
        key = "gmail|user=student@example.edu|type=tasks|id=account-check"
        rendered = "[gmail\\|user=student@example.edu](mailto:gmail|user=student@example.edu)|type=tasks|id=account-check"
        updated, report = import_personal(None, {"results": [{"url": "https://notion.so/abcdefabcdefabcdefabcdefabcdefab", "properties": {"Source Key": rendered, "Done": "__NO__"}}], "has_more": False},
                                          {"records": {key: {"page_id": "abcdefab-cdef-abcd-efab-cdefabcdefab"}}}, now=NOW)
        self.assertIn(key, updated["personal"])
        self.assertEqual(report["conflicts"], [])
        self.assertTrue(report["read_complete"] and report["applied_complete"])
        _, conflict = import_personal(None, {"results": [{"source_key": "wrong", "id": "abcdefabcdefabcdefabcdefabcdefab", "properties": {"Done": True}}], "has_more": False},
                                      {"records": {key: {"page_id": "abcdefabcdefabcdefabcdefabcdefab"}}}, now=NOW)
        self.assertTrue(conflict["read_complete"])
        self.assertFalse(conflict["applied_complete"])

    def test_older_readback_does_not_revert_local_feedback(self):
        _, learner = fixture()
        learner["personal"]["quiz"] = {"Done": True, "updated_at": "2026-09-14T23:00:00+00:00"}
        updated, report = import_personal(learner, [{"source_key": "quiz", "last_edited_time": "2026-09-14T21:00:00Z", "properties": {"Done": False}}], now=NOW)
        self.assertTrue(updated["personal"]["quiz"]["Done"])
        self.assertEqual(report["stale"], ["quiz"])

    def test_unknown_time_outputs_complete_draft(self):
        plan, learner = fixture()
        week = build_week_plan(plan, learner, now=NOW)
        self.assertEqual(week["status"], "draft")
        self.assertIsNone(week["budget_minutes"])
        self.assertEqual(week["scheduled"], [])
        self.assertEqual({t["id"] for t in week["tasks"]}, {"quiz", "homework", "form"})
        self.assertEqual(len(week["unscheduled"]), 3)
        self.assertIn("personal_timetable_unknown_no_class_times_inferred", week["warnings"])
        self.assertTrue(all(step["source_refs"] for t in week["tasks"] for step in t["steps"]))

    def test_zero_and_insufficient_capacity_preserve_every_task(self):
        plan, learner = fixture()
        zero = build_week_plan(plan, learner, availability=0, now=NOW)
        self.assertEqual(zero["scheduled_minutes"], 0)
        self.assertEqual(len(zero["unscheduled"]), 3)
        small = build_week_plan(plan, learner, availability=50, now=NOW)
        self.assertEqual(small["budget_minutes"], 40)
        self.assertEqual(small["scheduled_minutes"], 40)
        self.assertEqual(len(small["unscheduled"]), 3)
        self.assertTrue(all(t["minutes"] > 0 for t in small["unscheduled"]))

    def test_slots_union_and_explicit_user_plan_are_preserved(self):
        plan, learner = fixture()
        learner["personal"]["homework"] = {"Planned": {"start": "2026-09-16"}}
        slots = [{"start": "2026-09-16T14:00:00-04:00", "end": "2026-09-16T15:00:00-04:00"},
                 {"start": "2026-09-16T14:30:00-04:00", "end": "2026-09-16T16:00:00-04:00"}]
        week = build_week_plan(plan, learner, availability=slots, now=NOW)
        self.assertEqual(week["available_minutes"], 120)
        self.assertEqual(week["budget_minutes"], 96)
        self.assertLessEqual(week["scheduled_minutes"], 96)
        homework = next(t for t in week["tasks"] if t["id"] == "homework")
        self.assertEqual(homework["planned"], {"start": "2026-09-16"})
        self.assertTrue(all(s["start"][:10] == "2026-09-16" for s in week["scheduled"]))

    def test_known_slots_respect_deadline_and_explicit_plan_time(self):
        plan, learner = fixture()
        learner["personal"]["homework"] = {"Planned": {"start": "2026-09-16T15:00:00-04:00"}}
        week = build_week_plan(plan, learner, availability=[{"start": "2026-09-16T14:00:00-04:00", "end": "2026-09-16T17:00:00-04:00"}], now=NOW)
        self.assertNotIn("quiz", {s["task_id"] for s in week["scheduled"]})
        self.assertEqual(next(t for t in week["unscheduled"] if t["task_id"] == "quiz")["reason"], "deadline_capacity_insufficient")
        scheduled = [s for s in week["scheduled"] if s["task_id"] == "homework"]
        self.assertTrue(scheduled)
        self.assertEqual(scheduled[0]["start"], "2026-09-16T15:00:00-04:00")

    def test_tuesday_quiz_precedes_friday_high_priority_and_undated(self):
        plan, learner = fixture()
        learner["personal"]["homework"] = {"Priority": "High"}
        learner["personal"]["form"] = {"Priority": "High", "Planned": {"start": "2026-09-14"}}
        updated, session = study_start(plan, learner, 25, now=NOW)
        self.assertEqual(session["task_id"], "quiz")
        self.assertTrue(session["materials"])
        self.assertTrue(session["completion_criteria"])
        self.assertTrue(any("1 天" in reason for reason in session["reason"]))
        _, preferred = study_start(plan, updated, 10, preferred_task="form", now=NOW)
        self.assertEqual(preferred["task_id"], "form")

    def test_learning_session_prioritizes_assessment_and_keeps_admin_reminders(self):
        plan, learner = fixture()
        records = {r["source_key"]: r for r in plan["records"]}
        records["form"]["properties"].update({"Name": "Account status check", "Scope": "Academic Admin", "date:Due:start": "2026-09-01"})
        for key in ("homework", "quiz"):
            records[key]["properties"].update({"Scope": "Academic", "date:Due:start": None, "date:Study Date:start": "2026-09-15"})
        records["homework"]["study_mode"] = "logistics"
        records["quiz"]["study_mode"] = "assessment_preparation"
        week = build_week_plan(plan, learner, availability=100, now=NOW)
        self.assertEqual({task["id"] for task in week["tasks"]}, {"form", "homework", "quiz"})
        admin = next(task for task in week["tasks"] if task["id"] == "form")
        self.assertEqual(admin["estimated_minutes"], 5)
        self.assertEqual(admin["steps"][0]["estimate_basis"], "default_suggestion")
        self.assertIn("不假定 5 分钟能办完", admin["steps"][0]["completion_criteria"])
        self.assertLessEqual(sum(s["minutes"] for s in week["scheduled"] if s["task_id"] == "form"), 5)
        updated, session = study_start(plan, learner, 45, now=NOW)
        self.assertEqual(session["task_id"], "quiz")
        self.assertEqual(session["scope"], "Academic")
        self.assertEqual(session["study_mode"], "assessment_preparation")
        self.assertEqual([item["task_id"] for item in session["verification_reminders"]], ["form"])
        self.assertEqual(session["selection_kind"], "study")
        content = next(r for r in mentor_records(updated) if r["kind"] == "sessions")["generated_content"]
        self.assertIn("行政与状态核实提醒", content)
        self.assertIn("[[record:form]]", content)
        _, chosen_admin = study_start(plan, learner, 45, preferred_task="form", now=NOW)
        self.assertEqual(chosen_admin["task_id"], "form")
        self.assertEqual(chosen_admin["selection_kind"], "verification")
        self.assertEqual(chosen_admin["suggested_minutes"], 5)

    def test_admin_only_fallback_and_explicit_estimates_are_preserved(self):
        plan, learner = fixture()
        plan["records"] = [r for r in plan["records"] if r["kind"] != "tasks" or r["source_key"] == "form"]
        admin = plan["records"][-1]
        admin["properties"]["Scope"] = "Needs confirmation"
        _, session = study_start(plan, learner, 45, now=NOW)
        self.assertEqual(session["task_id"], "form")
        self.assertEqual(session["selection_kind"], "verification")
        self.assertTrue(any("没有未完成的课程学习项" in reason for reason in session["reason"]))
        admin["study_steps"] = [{"id": "application", "title": "Complete the reviewed application", "estimated_minutes": 40}]
        self.assertEqual(task_candidates(plan, learner, NOW)[0]["estimated_minutes"], 40)
        admin.pop("study_steps")
        learner["personal"]["form"] = {"Estimated Minutes": 30}
        self.assertEqual(task_candidates(plan, learner, NOW)[0]["estimated_minutes"], 30)
        learner["personal"]["form"].pop("Estimated Minutes")
        learner["task_progress"]["form"] = {"remaining_minutes": 37}
        self.assertEqual(task_candidates(plan, learner, NOW)[0]["estimated_minutes"], 37)

    def test_study_mode_is_explicit_and_past_preparation_is_not_a_deadline(self):
        plan, learner = fixture()
        records = {r["source_key"]: r for r in plan["records"]}
        for key in ("homework", "quiz"):
            records[key]["properties"].update({"date:Due:start": None, "date:Study Date:start": "2026-09-15"})
        _, session = study_start(plan, learner, 25, now=NOW)
        self.assertEqual(session["task_id"], "homework")  # Quiz in a title does not infer a reviewed mode.
        records["quiz"]["generated_content"] = {"study_mode": "assessment_preparation"}
        _, session = study_start(plan, learner, 25, now=NOW)
        self.assertEqual(session["task_id"], "quiz")
        records["quiz"]["properties"]["date:Study Date:start"] = "2026-09-12"
        records["homework"]["properties"]["date:Due:start"] = "2026-09-15"
        _, session = study_start(plan, learner, 25, now=NOW)
        self.assertEqual(session["task_id"], "homework")

    def test_cross_week_ids_feedback_and_new_conversation(self):
        plan, learner = fixture()
        first = build_week_plan(plan, learner, availability=120, now=NOW)
        learner, session = study_start(plan, learner, 25, now=NOW, week_plan=first)
        learner, done = study_finish(learner, session["id"], {"spent_minutes": 25, "difficulty": "stuck", "remaining_work": "Review the worked example", "remaining_minutes": 90}, now="2026-09-14T19:25:00-04:00")
        self.assertEqual(done["mastery_status"], "not_assessed")
        self.assertNotIn("Done", learner["personal"].get("quiz", {}))
        # Persisted JSON is sufficient; no hidden runtime conversation state.
        restored = json.loads(json.dumps(learner))
        _, next_session = study_start(plan, restored, 15, now="2026-09-14T20:00:00-04:00")
        self.assertIn("worked example", next_session["focus"])
        context = build_context(plan, restored, snapshot={"collected_at": NOW}, now="2026-09-14T20:00:00-04:00")
        self.assertEqual(context["recent_sessions"][0]["feedback"]["difficulty"], "stuck")
        self.assertEqual(context["current_week"]["id"], first["id"])
        self.assertEqual(context["freshness"]["canvas"]["status"], "recent")
        later = build_week_plan(plan, restored, availability=120, now="2026-09-21T10:00:00-04:00")
        self.assertNotEqual(first["id"], later["id"])
        self.assertEqual(first["tasks"][0]["steps"][0]["id"], later["tasks"][0]["steps"][0]["id"])
        self.assertEqual(later["tasks"][0]["estimated_minutes"], 90)
        self.assertIn("current_week_plan_missing", build_context(plan, restored, now="2026-09-21T10:00:00-04:00")["warnings"])
        records = mentor_records(restored)
        self.assertEqual({r["kind"] for r in records}, {"weeks", "sessions"})
        self.assertEqual(len({r["source_key"] for r in records}), len(records))

    def test_ninety_task_week_is_complete_folded_and_uses_page_references(self):
        plan, learner = fixture()
        plan["records"] = [record for record in plan["records"] if record["kind"] == "courses"]
        keys = []
        for index in range(90):
            key = "https://canvas.test|user=7|type=assignment|id=long-stable-" + str(index)
            keys.append(key)
            due = ("2026-09-15", "2026-09-23", None, "2026-11-01")[index % 4]
            props = {"Name": f"Unique task {index:02d}", "Source": key, "date:Due:start": due}
            if 60 <= index < 70:
                learner["personal"][key] = {"Done": True}
            elif 70 <= index < 75:
                props["Evidence Status"] = "confirmed_completed"
            elif 75 <= index < 80:
                props["Scope"] = "Optional"
            elif 80 <= index < 85:
                props["Scope"] = "Reference"
            elif index >= 85:
                props["Scope"] = "Historical"
            if index == 2:
                props["date:Study Date:start"] = "2026-09-24"
            if index == 0:
                props["Evidence Status"] = "partial_completed"
            plan["records"].append({"kind": "tasks", "source_key": key, "properties": props,
                                    "study_steps": [{"id": "read", "title": f"Unique detailed step {index:02d}", "estimated_minutes": 20}]})
        week = build_week_plan(plan, learner, now=NOW)
        content = week["records"][0]["generated_content"]
        self.assertEqual(week["status"], "draft")
        self.assertIn("可用学习时段尚未提供", content)
        self.assertEqual(content.count("<details>"), content.count("</details>"))
        for label in ("｜本周", "｜下一周", "｜再下一周", "无日期 · 待核实", "远期 ·", "未安排完整清单", "可选活动", "参考信息", "历史或范围外记录", "来源确认完成", "来源确认部分完成"):
            self.assertIn(label, content)
        self.assertIn("可用时间尚未提供 45 项", content)
        for index, key in enumerate(keys):
            self.assertIn("[[record:" + key + "]]", content)
        without_placeholders = re.sub(r"\[\[record:[^\]]+\]\]", "", content)
        self.assertNotIn("https://canvas.test|user=7|type=assignment", without_placeholders)
        self.assertIn("Unique detailed step 00", content)
        self.assertIn("Unique detailed step 02", content)  # Explicit preparation date.
        self.assertIn("Unique detailed step 01", content)  # Next week has its own daily plan.
        self.assertIn("Unique detailed step 03", content)  # Full requirements remain in folded groups.
        self.assertIn("Unique detailed step 06", content)
        unscheduled = content.split("<summary>未安排完整清单", 1)[1].split("</details>", 1)[0]
        self.assertNotIn("Unique detailed step 00", unscheduled)
        self.assertNotIn("Unique task 00", content)  # Native mentions supply the title once.

    def test_session_body_uses_task_and_week_page_references(self):
        plan, learner = fixture()
        learner, session = study_start(plan, learner, 25, now=NOW)
        record = next(r for r in mentor_records(learner) if r["kind"] == "sessions")
        content = record["generated_content"]
        self.assertIn("[[record:" + session["task_id"] + "]]", content)
        self.assertIn("[[record:" + session["week_id"] + "]]", content)
        self.assertIn("https://canvas.test/file/1", content)
        self.assertNotIn(session["week_id"], re.sub(r"\[\[record:[^\]]+\]\]", "", content))

    def test_existing_week_title_is_retained_on_replanning(self):
        plan, learner = fixture()
        learner["weeks"]["2026-09-14"] = {"title": "Week 2 · 按日学习计划"}
        week = build_week_plan(plan, learner, now=NOW)
        self.assertEqual(week["title"], "Week 2 · 按日学习计划")
        self.assertEqual(week["records"][0]["properties"]["Name"], "Week 2 · 按日学习计划")

    def test_source_reference_url_is_available_as_start_material(self):
        plan, learner = fixture()
        task = next(r for r in plan["records"] if r["source_key"] == "quiz")
        task["properties"].pop("Source URL")
        task["properties"].pop("Resources")
        task["source_refs"] = ["https://canvas.test/required-reading"]
        _, session = study_start(plan, learner, 25, now=NOW)
        self.assertEqual(session["materials"][0]["url"], "https://canvas.test/required-reading")

    def test_study_start_preserves_saved_week_capacity(self):
        plan, learner = fixture()
        week = build_week_plan(plan, learner, availability=200, now=NOW)
        learner["weeks"][week["week_start"]] = {k: v for k, v in week.items() if k != "records"}
        updated, session = study_start(plan, learner, 25, now=NOW)
        self.assertEqual(updated["weeks"][week["week_start"]]["available_minutes"], 200)
        self.assertEqual(updated["weeks"][week["week_start"]]["scheduled"], week["scheduled"])
        self.assertEqual(session["week_id"], week["id"])

    def test_explicit_task_completion_and_finish_idempotence(self):
        plan, learner = fixture()
        learner, session = study_start(plan, learner, 10, now=NOW)
        feedback = {"task_completed": True, "step_completed": True, "spent_minutes": 10, "remaining_work": ""}
        done, saved = study_finish(learner, session["id"], feedback, now=NOW)
        repeated, _ = study_finish(done, session["id"], feedback, now=NOW)
        self.assertEqual(done, repeated)
        self.assertNotIn("quiz", {t["id"] for t in task_candidates(plan, done, NOW)})
        self.assertEqual(saved["mastery_status"], "not_assessed")

    def test_submitted_and_historical_retained_without_becoming_mastery(self):
        plan, learner = fixture()
        next(r for r in plan["records"] if r["source_key"] == "quiz")["properties"]["Canvas Status"] = "submitted"
        next(r for r in plan["records"] if r["source_key"] == "form")["properties"]["Scope"] = "Historical"
        week = build_week_plan(plan, learner, now=NOW)
        self.assertEqual([t["id"] for t in week["tasks"]], ["homework"])
        self.assertEqual({t["status"] for t in week["completed_tasks"]}, {"submitted_pending_feedback"})
        self.assertEqual([t["id"] for t in week["historical_tasks"]], ["form"])
        self.assertEqual(learner["personal"], {})
        next(r for r in plan["records"] if r["source_key"] == "quiz")["properties"]["Requires Resubmission"] = True
        self.assertIn("quiz", [t["id"] for t in task_candidates(plan, learner, NOW)])

    def test_preparation_date_plans_reading_without_inventing_due(self):
        plan, learner = fixture()
        reading = next(r for r in plan["records"] if r["source_key"] == "form")
        reading["properties"].update({"Type": "Class preparation", "date:Study Date:start": "2026-09-14"})
        reading["study_steps"] = [{"id": "read-chapter", "title": "Read the assigned chapter", "estimated_minutes": 20, "source_refs": ["https://canvas.test/syllabus"]}]
        week = build_week_plan(plan, learner, availability=300, now=NOW)
        candidate = next(t for t in week["tasks"] if t["id"] == "form")
        self.assertIsNone(candidate["due"])
        self.assertEqual(candidate["target_date"], "2026-09-14")
        self.assertEqual(candidate["horizon"], "this_week")
        self.assertEqual(candidate["deadline_kind"], "preparation")
        self.assertIn("form", {s["task_id"] for s in week["scheduled"]})
        _, session = study_start(plan, learner, 25, now=NOW)
        self.assertEqual(session["task_id"], "quiz")
        self.assertIn("非官方截止", week["records"][0]["generated_content"])
        self.assertIsNone(reading["properties"]["date:Due:start"])

    def test_reference_information_is_not_a_delivery_task(self):
        plan, learner = fixture()
        reference = next(r for r in plan["records"] if r["source_key"] == "form")
        reference["properties"].update({"Name": "Pod number", "Scope": "Reference"})
        week = build_week_plan(plan, learner, now=NOW)
        self.assertEqual([t["id"] for t in week["reference_tasks"]], ["form"])
        self.assertNotIn("form", {t["id"] for t in week["tasks"]+week["completed_tasks"]+week["historical_tasks"]})

    def test_context_is_bounded_and_preserves_confirmed_section_facts(self):
        plan, learner = fixture()
        learner["profile"].update({"institution": "Sample University", "term": "synthetic-26fall",
                                  "confirmed_sections": [{"course": "CIV102", "section": "PRA0102", "day": "Tuesday", "start": "13:00", "end": "15:00", "room": "GB217", "first_room": "GB117"}], "unknowns": ["Other sections not confirmed"]})
        base = next(r for r in plan["records"] if r["source_key"] == "form")
        for index in range(105):
            task = deepcopy(base);task["source_key"] = "extra:" + str(index)
            plan["records"].append(task)
        week = build_week_plan(plan, learner, now=NOW)
        learner["weeks"][week["week_start"]] = week
        context = build_context(plan, learner, snapshot={"generated_at": NOW, "coverage": {"status": "stale", "fresh": 3, "stale": 1, "unavailable": 0}}, now=NOW, state_dir="/synthetic/state")
        self.assertEqual(context["unfinished_task_count"], 108)
        self.assertEqual(len(context["unfinished_tasks"]), 40)
        self.assertTrue(context["truncated"]["unfinished_tasks"])
        self.assertEqual(context["current_week"]["unscheduled_count"], 108)
        self.assertEqual(len(context["current_week"]["unscheduled"]), 40)
        self.assertEqual(context["profile"]["confirmed_sections"][0]["room"], "GB217")
        self.assertEqual(context["profile"]["unknowns"], ["Other sections not confirmed"])
        self.assertEqual(context["complete_index"]["plan_file"], "/synthetic/state/notion-plan.json")
        self.assertEqual(len(learner["weeks"][week["week_start"]]["unscheduled"]), 108)
        self.assertIn("canvas_snapshot_contains_stale_or_unavailable_endpoint_data", context["warnings"])

    def test_source_confirms_whole_or_partial_completion(self):
        plan, learner = fixture()
        first = next(r for r in plan["records"] if r["source_key"] == "quiz")
        first["properties"]["Evidence Status"] = "confirmed_completed"
        second = next(r for r in plan["records"] if r["source_key"] == "homework")
        second["properties"]["Evidence Status"] = "partial_completed"
        second["properties"]["Canvas Status"] = "submitted"
        second["observations"] = [{"status": "partial_completed", "summary": "线上部分已通过", "remaining_work": "完成线下实验培训", "source_refs": ["mail:confirmation"]}]
        week = build_week_plan(plan, learner, now=NOW)
        self.assertEqual([t["id"] for t in week["source_completed_tasks"]], ["quiz"])
        self.assertNotIn("quiz", [t["id"] for t in week["completed_tasks"]])
        partial = next(t for t in week["tasks"] if t["id"] == "homework")
        self.assertEqual(partial["remaining_work"], "完成线下实验培训")
        self.assertIn("线上部分已通过", " ".join(partial["reason"]))
        self.assertEqual(learner["personal"], {})
        first["properties"]["Evidence Status"] = "acknowledged"
        self.assertIn("quiz", [t["id"] for t in task_candidates(plan, learner, NOW)])

    def test_optional_deadline_does_not_displace_required_quiz(self):
        plan, learner = fixture()
        optional = next(r for r in plan["records"] if r["source_key"] == "form")
        optional["properties"].update({"Scope": "Optional", "date:Due:start": "2026-09-14"})
        week = build_week_plan(plan, learner, availability=120, now=NOW)
        self.assertEqual([t["id"] for t in week["optional_tasks"]], ["form"])
        self.assertNotIn("form", [t["id"] for t in week["completed_tasks"]])
        _, session = study_start(plan, learner, 25, now=NOW)
        self.assertEqual(session["task_id"], "quiz")
        _, chosen = study_start(plan, learner, 25, preferred_task="form", now=NOW)
        self.assertEqual(chosen["task_id"], "form")
        learner["personal"]["form"] = {"Priority": "High"}
        self.assertIn("form", [t["id"] for t in task_candidates(plan, learner, NOW)])

    def test_source_step_ids_stable_and_future_deadline_still_listed(self):
        plan, learner = fixture()
        task = next(r for r in plan["records"] if r["source_key"] == "homework")
        task["properties"]["date:Due:start"] = "2026-11-01"
        task["study_steps"] = [{"id": "draft", "title": "Write draft", "estimated_minutes": 120, "completion_criteria": "Save the required draft"}]
        week = build_week_plan(plan, learner, availability=300, now=NOW)
        self.assertIn("homework", [t["task_id"] for t in week["unscheduled"]])
        self.assertEqual(next(t for t in week["tasks"] if t["id"] == "homework")["steps"][0]["id"], "homework:step:draft")


if __name__ == "__main__":
    unittest.main()

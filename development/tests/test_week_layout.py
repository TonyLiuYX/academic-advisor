"""Calendar boundaries, faithful class mapping, and offline formatting."""
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill/canvas-notion-study/scripts"))
from study_sync.week_layout import attach_layout, render_week, when
from study_sync.planning import build_week_plan
from test_mentor import fixture, NOW


class WeekLayoutTests(unittest.TestCase):
    def example(self):
        plan, learner = fixture()
        week = build_week_plan(plan, learner, now=NOW)
        task = {"id": "prep", "title": "Read assigned chapter", "trigger_id": "C1:mon", "course": ["C1"],
                "target_date": "2026-09-20", "prepare_before": "2026-09-21T13:00:00-04:00",
                "estimated_minutes": 25, "planning_origin": "Teacher requirement",
                "steps": [{"title": "Read sections 3.1–3.3", "completion_criteria": "Bring a question"}]}
        week["tasks"].append(task)
        classes = [{"occurrence_id": "C1:mon", "series_id": "C1:L1", "label": "C1 L1", "kind": "lecture",
                    "date": "2026-09-21", "start": "2026-09-21T13:00:00-04:00", "end": "2026-09-21T14:00:00-04:00",
                    "room": "BA1130", "cancelled": False}]
        return plan, learner, week, classes

    def test_sunday_prepares_for_monday_in_separate_week(self):
        plan, _, week, classes = self.example()
        original = deepcopy(week)
        content = render_week(attach_layout(week, plan, classes))
        self.assertEqual(week, original)
        headings = re.findall(r"^\s*### (周.) · (\d+月\d+日)", content, re.M)
        self.assertEqual(len(headings), 21)
        self.assertEqual(headings[6:8], [("周日", "9月20日"), ("周一", "9月21日")])
        sunday = content.split("### 周日 · 9月20日", 1)[1].split("---", 1)[0]
        self.assertIn("今天提前准备", sunday)
        self.assertIn("Read sections 3.1–3.3", sunday)
        monday = content.split("### 周一 · 9月21日", 1)[1].split("###", 1)[0]
        self.assertIn("BA1130", monday)
        self.assertIn("13:00–14:00", monday)
        self.assertIn("9月20日（周日）", monday)
        self.assertNotIn("2026-09-21T", content)
        self.assertNotIn("官方截止 9月21日", content)
        self.assertEqual(content.count("\n---"), 3)
        self.assertEqual(content.count("<table "), content.count("</table>"))

    def test_cancelled_classes_do_not_schedule_preparation(self):
        plan, _, week, classes = self.example()
        classes[0]["cancelled"] = True
        content = render_week(attach_layout(week, plan, classes))
        monday = content.split("### 周一 · 9月21日", 1)[1].split("###", 1)[0]
        self.assertIn("停课／取消", monday)
        self.assertNotIn("Read sections", monday)
        self.assertIn("[[record:prep]]", content)  # Source task is retained.
        self.assertNotIn("今天提前准备", content)

    def test_overdue_is_separate_and_personal_date_kept(self):
        plan, _, week, classes = self.example()
        week["tasks"].append({"id": "old", "title": "Old", "due": "2026-09-10", "steps": []})
        week["tasks"].append({"id": "personal", "title": "Mine", "due": "2026-09-15", "planned_date": "2026-09-18", "steps": []})
        content = render_week(attach_layout(week, plan, classes))
        main, rest = content.split("## 其他事项")
        self.assertNotIn("[[record:old]]", main)
        self.assertIn("[[record:old]]", rest)
        friday = main.split("### 周五 · 9月18日", 1)[1].split("###", 1)[0]
        self.assertIn("[[record:personal]]", friday)
        self.assertIn("官方截止 9月15日", friday)

    def test_timezone_date_only_and_exact_seconds(self):
        self.assertEqual(when("2026-09-16T01:30:00Z", "America/Toronto"), "9月15日（周二） 21:30")
        self.assertEqual(when("2026-09-16", "America/Toronto"), "9月16日（周三）")
        self.assertEqual(when("2026-09-16T23:59:59-04:00", "America/Toronto"), "9月16日（周三） 23:59:59")

    def test_offline_cli_only_reformats_weeks_and_is_idempotent(self):
        plan, learner = fixture()
        week = build_week_plan(plan, learner, now=NOW)
        learner["weeks"][week["week_start"]] = {k: v for k, v in week.items() if k != "records"}
        plan["records"] += week["records"]
        before = deepcopy(plan["records"][:-1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"base_url": "https://canvas.test", "state_dir": tmp, "archive_dir": tmp, "term": {"timezone": "America/Toronto"}}
            for name, data in (("config", config), ("notion-plan", plan), ("learner", learner)):
                (root / (name + ".json")).write_text(json.dumps(data))
            cmd = [sys.executable, str(Path(__file__).resolve().parents[2] / "skill/canvas-notion-study/scripts/study.py"), "--config", str(root / "config.json"), "week-render"]
            subprocess.run(cmd, check=True, capture_output=True)  # No credentials or source snapshot exists.
            output = (root / "notion-plan.json").read_bytes()
            self.assertEqual(json.loads(output)["records"][:-1], before)
            actual = json.loads((root / "learner.json").read_text())
            self.assertEqual(actual["personal"], learner["personal"])
            self.assertEqual(actual["weeks"][week["week_start"]]["tasks"], week["tasks"])
            subprocess.run(cmd, check=True, capture_output=True)
            self.assertEqual((root / "notion-plan.json").read_bytes(), output)


if __name__ == "__main__":
    unittest.main()

import unittest

from study_sync.requirements import build_requirements, reconcile_sources


class RequirementTests(unittest.TestCase):
    def test_existing_tasks_and_explicit_exclusions_keep_provenance(self):
        plan = {"records": [{"kind": "tasks", "source_key": "canvas-task", "properties": {"Name": "Reading", "Source": "announcement-1"}}]}
        gmail = {"requirements": [{"id": "email-review", "source_refs": ["gmail:message:1"], "task_keys": [], "status": "non_actionable", "reason": "No action requested."}]}
        result = build_requirements(plan, gmail)
        self.assertEqual(result["canvas_coverage_basis"], "projected_tasks_only")
        self.assertEqual(len(result["requirements"]), 2)
        self.assertEqual(result["requirements"][0]["task_keys"], ["canvas-task"])
        self.assertIn("announcement-1", result["requirements"][0]["source_refs"])

    def test_reconciliation_reports_unreviewed_source_even_when_tasks_exist(self):
        requirements = {"requirements": [{"id": "reading", "source_refs": ["page-1"], "task_keys": ["task-1"], "status": "required", "reason": "Assigned reading."},
                                          {"id": "notice", "source_refs": ["notice-1"], "task_keys": [], "status": "non_actionable", "reason": "Information only."}]}
        result = reconcile_sources(["page-1", "notice-1", "unread-page"], requirements)
        self.assertEqual(result["reviewed_source_count"], 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"][0]["source_key"], "unread-page")

    def test_same_requirement_merges_task_and_source_references(self):
        first = {"requirements": [{"id": "form", "source_refs": ["canvas-1"], "task_keys": ["form-task"], "status": "required", "reason": "Required."}]}
        second = {"requirements": [{"id": "form", "source_refs": ["email-1"], "task_keys": ["form-task"], "status": "required", "reason": "Reminder."}]}
        result = build_requirements(first, second)
        self.assertEqual(len(result["requirements"]), 1)
        self.assertEqual(result["requirements"][0]["source_refs"], ["canvas-1", "email-1"])
        self.assertTrue(reconcile_sources(["canvas-1", "email-1"], result)["ok"])


if __name__ == "__main__":
    unittest.main()

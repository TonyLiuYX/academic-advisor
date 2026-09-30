from copy import deepcopy
from datetime import datetime
import tempfile
from pathlib import Path
import unittest

from study_sync.schedule import compile_preparations, occurrences, task_key
from study_sync.v1 import apply_preparations, build_v1_plan
from study_sync.planning import build_week_plan, task_candidates, study_start
from study_sync.requirements import build_requirements
from study_sync.state import write_json, rendered_content

NOW = "2026-09-14T08:00:00-04:00"
COURSE = "https://canvas.test|user=7|type=course|id=1"


def sample():
    plan = {"term":{"key":"test"},"timezone":"America/Toronto", "records":[{"kind":"courses","source_key":COURSE,"properties":{"Name":"Sample"}}]}
    series = {"id":"lec", "course_key":COURSE,"kind":"lecture","label":"Sample LEC0101", "confirmed":True,
              "start_date":"2026-09-14","end_date":"2026-10-16","weekdays":[0,2],"start_time":"10:00","end_time":"11:00",
              "source_refs":["https://canvas.test/syllabus"]}
    return plan, {"series":[series]}


def project(plan, payload, previous=None, legacy=None, learner=None, now=NOW, week=None):
    legacy=legacy or {"preparations":[]}
    result=deepcopy(plan)
    apply_preparations(result,legacy)
    compiled=compile_preparations(result,payload,legacy,previous,learner,week=week,now=now)
    apply_preparations(result,{"preparations":list(compiled["items"].values())})
    result["class_schedule"]=compiled
    return result, compiled


class ScheduleTests(unittest.TestCase):
    def test_all_class_kinds_coarse_tasks_without_due(self):
        plan,payload=sample()
        for kind in ("lab","pra","tutorial"):
            payload["series"].append({**payload["series"][0],"id":kind,"kind":kind,"weekdays":[1]})
        result,state=project(plan,payload)
        self.assertEqual(len(state["classes"]),15)
        self.assertEqual(len(state["items"]),15)
        self.assertEqual({x["class_kind"] for x in state["items"].values()}, {"lecture","lab","pra","tutorial"})
        self.assertFalse(any("date:Due:start" in r["properties"] for r in result["records"]))
        self.assertEqual(build_requirements(result)["requirements"],[])
        self.assertEqual(len(task_candidates(result,{},now=NOW)),15)

    def test_unconfirmed_section_does_not_generate_class_times(self):
        plan,payload=sample();payload["series"][0]["confirmed"]=False
        result,state=project(plan,payload)
        self.assertEqual(state["items"],{})
        self.assertEqual(state["busy_slots"],[])
        self.assertTrue(state["unknowns"])

    def test_recurrence_holiday_and_move_keep_identity(self):
        plan,payload=sample();s=payload["series"][0]
        s.update({"interval_weeks":2,"exclude_dates":["2026-09-16"],"exceptions":{"2026-09-14":{"date":"2026-09-15","start_time":"12:00","end_time":"13:00"}}})
        result,state=project(plan,payload)
        self.assertIn("class:lec:2026-09-14",state["items"])
        self.assertEqual(state["items"]["class:lec:2026-09-14"]["prepare_before"],"2026-09-15T12:00:00-04:00")
        self.assertNotIn("class:lec:2026-09-16",state["items"])
        self.assertNotIn("class:lec:2026-09-21",state["items"])

    def test_published_move_cancel_repeat_and_personal_completion(self):
        plan,payload=sample();result,first=project(plan,payload)
        key=task_key(first["items"]["class:lec:2026-09-16"])
        learner={"personal":{key:{"Done":True,"Personal Notes":"Keep"}}}
        payload["series"][0]["exceptions"]={"2026-09-16":{"date":"2026-09-17"},"2026-09-14":{"cancelled":True}}
        result,moved=project(plan,payload,first,learner=learner)
        self.assertEqual(task_key(moved["items"]["class:lec:2026-09-16"]),key)
        self.assertEqual(moved["items"]["class:lec:2026-09-14"]["preparation_status"],"Cancelled")
        self.assertNotIn(key,{t["id"] for t in task_candidates(result,learner,now=NOW)})
        _,again=project(plan,payload,moved,learner=learner)
        self.assertEqual(moved,again)
        self.assertEqual(learner["personal"][key]["Personal Notes"],"Keep")

    def test_missed_review_merges_and_does_not_mark_done(self):
        plan,payload=sample();_,first=project(plan,payload)
        result,second=project(plan,payload,first,now="2026-09-15T12:00:00-04:00")
        old=second["items"]["class:lec:2026-09-14"]
        self.assertEqual(old["preparation_status"],"Merged")
        self.assertEqual(old["merged_into"],task_key(second["items"]["class:lec:2026-09-16"]))
        self.assertNotIn(task_key(old),{x["id"] for x in task_candidates(result,{},now="2026-09-15T12:00:00-04:00")})
        self.assertFalse(any(r["properties"].get("Done") for r in result["records"]))

    def test_existing_preparation_and_independent_prelab_reused(self):
        plan,payload=sample()
        plan["records"].append({"kind":"tasks","source_key":"prelab","properties":{"Name":"Submit prelab","date:Due:start":"2026-09-15T18:00:00-04:00"},"generated_content":{}})
        legacy={"preparations":[{"id":"old","course_key":COURSE,"title":"Read lab instructions","source_refs":["https://canvas.test/lab"],"study_date":"2026-09-16"}]}
        payload["bindings"]=[{"occurrence_id":"lec:2026-09-16","preparation_id":"old"}]
        payload["series"][0]["linked_task_keys"]=["prelab"]
        result,state=project(plan,payload,legacy=legacy)
        self.assertEqual(state["items"]["class:lec:2026-09-16"]["canonical_task_key"],COURSE+"|preparation=old")
        self.assertEqual(sum(r["source_key"]==COURSE+"|preparation=old" for r in result["records"]),1)
        self.assertEqual(next(r for r in result["records"] if r["source_key"]=="prelab")["properties"]["date:Due:start"],"2026-09-15T18:00:00-04:00")

    def test_busy_classes_removed_and_prep_must_finish_before_class(self):
        plan,payload=sample();result,state=project(plan,payload)
        slots={"slots":[{"start":"2026-09-14T09:00:00-04:00","end":"2026-09-14T12:00:00-04:00"}]}
        week=build_week_plan(result,{},availability=slots,now=NOW)
        self.assertEqual(week["available_minutes"],120)
        self.assertEqual(week["budget_minutes"],96)
        for block in week["scheduled"]:
            self.assertFalse(block["start"] < "2026-09-14T11:00:00-04:00" and block["end"] > "2026-09-14T10:00:00-04:00")
            if block["task_id"]==task_key(state["items"]["class:lec:2026-09-14"]):
                self.assertLessEqual(block["end"],"2026-09-14T10:00:00-04:00")

    def test_no_preclass_slot_and_future_materials_stay_unplanned(self):
        plan,payload=sample();payload["series"][0]["available_from"]="2026-09-21T09:00:00-04:00"
        result,state=project(plan,payload)
        week=build_week_plan(result,{},availability=120,now=NOW)
        self.assertEqual(week["scheduled"],[])
        self.assertTrue(week["unscheduled"])
        self.assertEqual(build_week_plan(result,{},now=NOW)["status"],"draft")

    def test_recommended_practice_admitted_optional_admin_excluded(self):
        plan,payload=sample();payload["series"]=[]
        payload["recommendations"]=[{"id":"week2","course_key":COURSE,"title":"Teacher problem set","study_date":"2026-09-18","source_refs":["https://canvas.test/practice"]}]
        result,_=project(plan,payload)
        result["records"].append({"kind":"tasks","source_key":"admin","properties":{"Name":"Optional event","Scope":"Optional"}})
        candidate=task_candidates(result,{},now=NOW)
        self.assertEqual(len(candidate),1)
        self.assertEqual(candidate[0]["planning_origin"],"Teacher recommendation")
        self.assertTrue(all(x["status"]=="optional" for x in build_requirements(result)["requirements"] if x["id"]!="admin"))

    def test_exam_lead_times_and_late_announcement(self):
        plan,payload=sample();payload["series"]=[]
        payload["assessments"]=[{"id":"exam","kind":"midterm","course_key":COURSE,"title":"Midterm","date":"2026-10-05T09:00:00-04:00","source_refs":["https://canvas.test/exam"]}]
        _,state=project(plan,payload)
        self.assertEqual(state["items"]["assessment:exam:scope"]["study_date"],"2026-09-28")
        self.assertEqual(state["items"]["assessment:exam:review"]["study_date"],"2026-10-03")
        _,late=project(plan,payload,now="2026-10-04T12:00:00-04:00")
        self.assertNotIn("assessment:exam:scope",late["items"])
        self.assertEqual(late["items"]["assessment:exam:review"]["study_date"],"2026-10-04")

    def test_next_week_regenerates_without_losing_prior_identity(self):
        plan,payload=sample();_,first=project(plan,payload)
        result,second=project(plan,payload,first,week="2026-09-21",now="2026-09-21T08:00:00-04:00")
        self.assertTrue(set(first["items"])<=set(second["items"]))
        self.assertEqual(second["week_start"],"2026-09-21")
        self.assertIn("class:lec:2026-10-05",second["items"])

    def test_complete_rebuild_after_rendered_receipts_is_idempotent(self):
        snapshot={"canvas_origin":"https://canvas.test","user":{"id":7},"term":{"key":"test","timezone":"America/Toronto"},"courses":[{"id":1,"name":"Sample","course_code":"SMP","mode":"course"}]}
        _,payload=sample()
        with tempfile.TemporaryDirectory() as d:
            write_json(Path(d)/"course-schedule.json",payload)
            first=build_v1_plan(snapshot,{}, {"term":snapshot["term"]},{},d,now=NOW)
            write_json(Path(d)/"notion-state.json",{"records":{r["source_key"]:{"kind":r["kind"],"properties":r["properties"],"generated_content":rendered_content(r),"page_id":"page"+str(i)} for i,r in enumerate(first["records"])}})
            again=build_v1_plan(snapshot,{}, {"term":snapshot["term"]},{},d,now=NOW)
            self.assertEqual({r["source_key"]:r["fingerprint"] for r in first["records"]},{r["source_key"]:r["fingerprint"] for r in again["records"]})
            payload["series"][0]["exceptions"]={"2026-09-14":{"cancelled":True}}
            write_json(Path(d)/"course-schedule.json",payload)
            changed=build_v1_plan(snapshot,{}, {"term":snapshot["term"]},{},d,now=NOW)
            self.assertNotIn(COURSE+"|preparation=rule:class:lec:2026-09-14",{x["id"] for x in task_candidates(changed,{},now=NOW)})


if __name__=="__main__": unittest.main()

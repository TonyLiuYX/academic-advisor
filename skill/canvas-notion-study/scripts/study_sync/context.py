"""Bounded handoff context with explicit totals and complete-state retrieval."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from .learner import normalise_learner, timestamp
from .planning import _now, _date, task_candidates


def _compact(value: Any, *, chars: int = 500, items: int = 6) -> Any:
    if isinstance(value, str):
        return value if len(value) <= chars else value[:chars] + "… [text truncated; retrieve full record]"
    if isinstance(value, list):
        selected = [_compact(item, chars=chars, items=items) for item in value[:items]]
        if len(value) > items:
            selected.append({"omitted_items": len(value)-items})
        return selected
    if isinstance(value, Mapping):
        return {key: _compact(item, chars=chars, items=items) for key, item in list(value.items())[:30]}
    return value


def build_context(plan: Mapping, learner: Mapping | None, week_plan: Mapping | None = None,
                  snapshot: Mapping | None = None, now: Any = None, recent_sessions: int = 5,
                  state_dir: str | None = None) -> dict:
    learner = normalise_learner(learner)
    tz = learner["profile"].get("timezone", plan.get("timezone", "UTC"))
    current = _now(now, tz)
    week_start = (current.date() - timedelta(days=current.weekday())).isoformat()
    if week_plan is None:
        week_plan = learner["weeks"].get(week_start)
    tasks = task_candidates(plan, learner, now=current)
    unfinished = [_compact({k: task[k] for k in ("id", "title", "due", "due_conflict", "planned", "priority", "remaining_work", "difficulty", "source_refs", "evidence_status", "target_date", "deadline_kind", "planning_origin", "learning_activity", "prepare_before", "trigger_id")}, chars=350, items=3)
                  for task in tasks[:40]]
    upcoming = []
    for task in tasks:
        due = _date(task["due"], tz)
        if due and due <= current.date() + timedelta(days=21):
            upcoming.append({"id": task["id"], "title": task["title"], "due": task["due"], "type": task["type"],
                             "source_refs": task["source_refs"], "overdue": due < current.date()})
    timetable = []
    for record in plan.get("records", []):
        if record.get("kind") != "timetable":
            continue
        props = record.get("properties", {})
        raw_start = props.get("date:Start:start", props.get("Start"))
        starts = _date(raw_start, tz)
        if starts and current.date() <= starts <= current.date()+timedelta(days=21):
            timetable.append({"id": record["source_key"], "title": props.get("Name"), "start": raw_start,
                              "end": props.get("date:End:start", props.get("End")), "type": props.get("Type"),
                              "source_url": props.get("Source URL"), "course": props.get("Course")})
    timetable.sort(key=lambda item: str(item["start"]))
    all_sessions = sorted(learner["sessions"].values(), key=lambda s: (s.get("started_at", ""), s["id"]), reverse=True)
    limit_sessions = min(5, max(0, recent_sessions))
    sessions = [_compact({k: session.get(k) for k in ("id", "task_id", "task_title", "status", "started_at", "finished_at", "minutes", "spent_minutes", "focus", "feedback", "remaining_work", "mastery_status")})
                for session in all_sessions[:limit_sessions]]
    freshness = {}
    stamps = {"canvas": (snapshot or {}).get("collected_at", (snapshot or {}).get("generated_at", (snapshot or {}).get("fetched_at"))),
              "notion_personal": learner.get("personal_read_at"), "learner": learner.get("updated_at"),
              "week_plan": (week_plan or {}).get("generated_at")}
    for name, value in stamps.items():
        try:
            age = (current - _now(value, tz)).total_seconds() / 3600 if value else None
        except (ValueError, TypeError):
            age = None
        freshness[name] = {"at": value, "age_hours": round(age, 1) if age is not None else None,
                           "status": "unknown" if age is None else "future_timestamp" if age < 0 else "stale" if age > 72 else "recent"}
    coverage = (snapshot or {}).get("coverage", {})
    coverage = coverage if isinstance(coverage, Mapping) else {}
    if coverage:
        freshness["canvas"]["coverage"] = {key: coverage.get(key) for key in ("status", "fresh", "stale", "unavailable", "checked_at")}
    profile_fields = ("name", "timezone", "goals", "preferences", "constraints", "timetable_confirmed", "study_preferences",
                      "confirmed_sections", "unknowns", "term", "institution", "include_optional")
    profile = {key: _compact(learner["profile"][key], chars=1000, items=30) for key in profile_fields if key in learner["profile"]}
    profile["timetable_status"] = "confirmed" if learner["profile"].get("timetable_confirmed") else "partially_confirmed" if learner["profile"].get("confirmed_sections") else "unknown"
    week_summary = None
    section_totals = {}
    if week_plan:
        week_summary = {k: week_plan.get(k) for k in ("id", "week_start", "week_end", "horizon_end", "status", "available_minutes", "budget_minutes", "scheduled_minutes", "warnings")}
        for key in ("scheduled", "unscheduled", "completed_tasks", "source_completed_tasks", "historical_tasks", "optional_tasks", "reference_tasks"):
            items = week_plan.get(key, [])
            section_totals[key] = len(items)
            week_summary[key] = [_compact(item, chars=350, items=3) for item in items[:40]]
            week_summary[key+"_count"] = len(items)
            week_summary[key+"_truncated"] = len(items) > 40
    warnings = []
    if not week_plan:
        warnings.append("current_week_plan_missing")
    if not learner.get("personal_read_at"):
        warnings.append("notion_personal_not_read_back")
    if any(value["status"] in ("unknown", "stale") for value in freshness.values()):
        warnings.append("check_source_freshness_before_time_sensitive_advice")
    if coverage.get("status") in ("stale", "unavailable"):
        warnings.append("canvas_snapshot_contains_stale_or_unavailable_endpoint_data")
    truncated = {"unfinished_tasks": len(tasks)>40, "upcoming_assessments_and_deadlines": len(upcoming)>25,
                 "upcoming_timetable": len(timetable)>25, "recent_sessions": len(all_sessions)>limit_sessions,
                 **{"week_"+key: total>40 for key, total in section_totals.items()}}
    if any(truncated.values()):
        warnings.append("context_is_a_priority_excerpt_not_the_complete_task_list")
    def location(filename):
        return str(Path(state_dir)/filename) if state_dir else filename
    index = {"records_total": len(plan.get("records", [])), "unfinished_total": len(tasks),
             "upcoming_deadlines_total": len(upcoming), "upcoming_timetable_total": len(timetable), "sessions_total": len(all_sessions),
             "plan_file": location("notion-plan.json"), "learner_file": location("learner.json"),
             "course_schedule_file":location("course-schedule.json"), "preparation_ledger_file":location("schedule-state.json"),
             "week_file": location("weeks/"+week_start+".json"), "requirements_file": location("requirements.json"),
             "retrieval": "Read the full local index for all records. Use study.py --config <instance-config> task --key <source-key> for one task, personal fields, progress and sources. Omitted records remain in local state and the full Notion week page."}
    notes = plan.get("note_entries", [])
    truncated["recent_notes"] = len(notes) > 10
    index["notes_total"] = len(notes)
    index["notes_state_file"] = location("notion-state.json")
    index["notes_retrieval"] = "study.py --config CONFIG notes-find --course COURSE --topic TOPIC --read; resolve the current local path before advice"
    return {"schema_version": 2, "generated_at": timestamp(current), "profile": profile,
            "recent_notes": [_compact(note) for note in notes[:10]],
            "class_schedule":{"classes":plan.get("class_schedule",{}).get("classes",[])[:30],"count":len(plan.get("class_schedule",{}).get("classes",[])),"truncated":len(plan.get("class_schedule",{}).get("classes",[]))>30,"unknowns":_compact(plan.get("class_schedule",{}).get("unknowns",[]))},
            "current_week": week_summary, "unfinished_tasks": unfinished, "unfinished_task_count": len(tasks),
            "upcoming_assessments_and_deadlines": [_compact(item, chars=350, items=3) for item in upcoming[:25]],
            "upcoming_timetable": [_compact(item, chars=350, items=3) for item in timetable[:25]],
            "recent_sessions": sessions, "freshness": freshness, "warnings": warnings, "truncated": truncated,
            "complete_index": index,
            "learning_claim": "Study activity and task completion are recorded; knowledge mastery has not been assessed."}

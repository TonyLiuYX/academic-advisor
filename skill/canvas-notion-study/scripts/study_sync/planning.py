"""Source-backed weekly planning and small, persistent study sessions.

Times and estimates are planning suggestions, never instructor requirements or
proof of mastery. Pure functions return JSON for the caller to persist.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import math
from typing import Any, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from .learner import normalise_learner, timestamp
from .week_layout import attach_layout, render_week

MENTOR_SCHEMAS = {
    "weeks": {"name": "Weeks", "description": "Weekly study plans with explicit capacity and unscheduled work.", "properties": {
        "Name": {"type": "title"}, "Source Key": {"type": "text"}, "Term": {"type": "text"},
        "Start": {"type": "date"}, "End": {"type": "date"}, "Status": {"type": "select"},
        "Available Minutes": {"type": "number"}, "Budget Minutes": {"type": "number"},
        "Scheduled Minutes": {"type": "number"}, "Tasks": {"type": "relation", "target": "tasks", "many": True}}},
    "sessions": {"name": "Study Sessions", "description": "Study activity, user feedback and remaining work; completion is not mastery.", "properties": {
        "Name": {"type": "title"}, "Source Key": {"type": "text"}, "Term": {"type": "text"},
        "Task": {"type": "relation", "target": "tasks", "many": True},
        "Week": {"type": "relation", "target": "weeks", "many": True},
        "Started": {"type": "date"}, "Finished": {"type": "date"},
        "Minutes": {"type": "number"}, "Status": {"type": "select"}}},
}


def _dt(value: Any, tz: str = "UTC") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=ZoneInfo(tz)) if parsed.tzinfo is None else parsed.astimezone(ZoneInfo(tz))


def _now(now: Any, tz: str) -> datetime:
    return _dt(now, tz) if now else datetime.now(ZoneInfo(tz))


def _date(value: Any, tz: str) -> date | None:
    if isinstance(value, Mapping):
        value = value.get("start")
    if not value:
        return None
    try:
        return _dt(value, tz).date()
    except (ValueError, TypeError):
        return None


def _list(value: Any) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _term(plan: Mapping) -> str:
    term = plan.get("term", {})
    return str(term.get("key", "term")) if isinstance(term, Mapping) else str(term)


def _namespace(plan: Mapping) -> str:
    keys = [str(r["source_key"]) for r in plan.get("records", []) if r.get("kind") == "courses"]
    # Namespace depends on instance/source ownership, never on current tasks.
    source = plan.get("source", {})
    source = source if isinstance(source, Mapping) else {}
    origin = plan.get("canvas_origin") or source.get("canvas_origin")
    owner = plan.get("user_id") or source.get("user_id")
    prefix = sorted(keys)[0].split("|type=", 1)[0] if keys else str(origin or "local") + "|user=" + str(owner or "self")
    return "mentor:" + hashlib.sha256((prefix + "|" + _term(plan)).encode()).hexdigest()[:20]


def week_identity(plan: Mapping, start: date) -> str:
    return _namespace(plan) + ":week:" + start.isoformat()


def _sources(record: Mapping) -> list:
    props = record.get("properties", {})
    raw = props.get("Source", "")
    sources = raw.splitlines() if isinstance(raw, str) else _list(raw)
    extra = [s.get("source_key", s.get("url", s.get("id", ""))) if isinstance(s, Mapping) else s for s in _list(record.get("source_refs"))]
    return list(dict.fromkeys([record["source_key"], *[s for s in sources if s], *[s for s in extra if s]]))


def _verification_scope(scope: Any) -> bool:
    return str(scope or "").strip().lower().replace("_", " ").replace("-", " ") in (
        "academic admin", "needs confirmation")


def _steps(record: Mapping, personal: Mapping, progress: Mapping, learner: Mapping) -> list:
    generated = record.get("generated_content", {})
    generated = generated if isinstance(generated, Mapping) else {}
    explicit = record.get("study_steps") or generated.get("study_steps")
    parent = record["source_key"]
    sources = _sources(record)
    finished = set(progress.get("completed_steps", []))
    scale = progress.get("estimate_scale", 1.0)
    try:
        scale = min(3.0, max(0.5, float(scale)))
    except (ValueError, TypeError):
        scale = 1.0
    result = []
    if explicit:
        for step in explicit:
            if not isinstance(step, Mapping) or not step.get("id"):
                raise ValueError(f"Reviewed study steps require a stable id: {parent}")
            step_id = parent + ":step:" + str(step["id"])
            estimate = step.get("estimated_minutes", 25)
            result.append({"id": step_id, "title": step.get("title", step.get("text", str(step["id"]))),
                           "estimated_minutes": max(1, math.ceil(float(estimate) * scale)),
                           "estimate_basis": "reviewed_estimate" if "estimated_minutes" in step else "default_suggestion",
                           "source_refs": step.get("source_refs") or sources,
                           "completion_criteria": step.get("completion_criteria", "完成本步骤并记录产出、疑问与下一步。"),
                           "done": step_id in finished})
    elif (_verification_scope(record.get("properties", {}).get("Scope"))
          and not any(value is not None for value in (personal.get("Estimated Minutes"),
                      record.get("properties", {}).get("Estimated Minutes"), generated.get("estimated_minutes"),
                      progress.get("remaining_minutes")))
          and record.get("properties", {}).get("Evidence Status") != "partial_completed"):
        step_id = parent + ":step:verify-status"
        result.append({"id": step_id, "title": "核实当前状态并记录下一步", "estimated_minutes": 5,
                       "estimate_basis": "default_suggestion", "source_refs": sources,
                       "completion_criteria": "核实当前状态并记录下一步；若需办理，记录实际步骤和所需时间，不假定 5 分钟能办完。",
                       "done": step_id in finished})
    else:
        raw_estimate = personal.get("Estimated Minutes", record.get("properties", {}).get("Estimated Minutes", generated.get("estimated_minutes", 60)))
        try:
            estimate = max(15, math.ceil(float(raw_estimate) * scale))
        except (ValueError, TypeError):
            estimate = 60
        weights = [("prepare", "读要求并准备材料", 0.2, "确认来源要求、交付物和需要使用的材料。"),
                   ("work", "完成主要学习或交付工作", 0.6, "产出可检查的解答、草稿或学习记录；记录未完成部分。"),
                   ("check", "核对要求并收尾", 0.2, "逐项核对来源要求；如需提交，确认提交结果。")]
        for label, title, weight, criteria in weights:
            step_id = parent + ":step:" + label
            result.append({"id": step_id, "title": title, "estimated_minutes": max(1, math.ceil(estimate * weight)),
                           "estimate_basis": "user_or_reviewed_estimate" if raw_estimate != 60 else "default_suggestion",
                           "source_refs": sources, "completion_criteria": criteria, "done": step_id in finished})
    if "remaining_minutes" in progress:
        unfinished = [s for s in result if not s["done"]]
        total = sum(s["estimated_minutes"] for s in unfinished)
        remaining = max(0, math.ceil(progress["remaining_minutes"]))
        for index, step in enumerate(unfinished):
            allocation = remaining if index == len(unfinished) - 1 else min(remaining, round(progress["remaining_minutes"] * step["estimated_minutes"] / max(total, 1)))
            step["estimated_minutes"] = allocation
            step["estimate_basis"] = "user_remaining_minutes"
            remaining -= allocation
    if len({step["id"] for step in result}) != len(result):
        raise ValueError(f"Duplicate study step id: {parent}")
    if progress.get("remaining_work"):
        for step in result:
            if not step["done"]:
                step["remaining_work"] = progress["remaining_work"]
                break
    return result


def task_status(record: Mapping, learner: Mapping, include_optional: bool = False) -> str | None:
    props = record.get("properties", {})
    if props.get("Preparation Status") in ("Cancelled", "Merged", "Past", "Deferred") and props.get("Planning Origin") != "Teacher requirement":
        return "advice_" + props["Preparation Status"].lower()
    scope = str(props.get("Scope", "")).lower()
    if scope in ("historical", "out_of_scope", "历史"):
        return "historical_out_of_scope"
    if scope in ("reference", "informational", "参考", "信息"):
        return "reference_not_actionable"
    personal = learner.get("personal", {}).get(record["source_key"], {})
    if personal.get("Done") in (True, "__YES__"):
        return "user_completed"
    opted_in = props.get("Planning Origin") == "Teacher recommendation" or include_optional or learner.get("profile", {}).get("include_optional") is True or bool(personal.get("Priority")) or bool(personal.get("Planned"))
    if scope in ("optional", "可选") and not opted_in:
        return "optional_not_selected"
    generated = record.get("generated_content", {})
    generated = generated if isinstance(generated, Mapping) else {}
    if str(props.get("Evidence Status", "")).lower() == "confirmed_completed":
        return "source_confirmed_completed"
    if str(props.get("Evidence Status", "")).lower() == "partial_completed":
        return None
    resubmit = record.get("requires_resubmission", generated.get("requires_resubmission", props.get("Requires Resubmission")))
    if resubmit not in (True, "__YES__"):
        canvas = str(props.get("Canvas Status", "")).lower()
        if canvas in ("submitted", "pending_review"):
            return "submitted_pending_feedback"
        if canvas == "graded":
            return "graded"
    return None


def task_candidates(plan: Mapping, learner: Mapping | None, now: Any = None, include_optional: bool = False) -> list:
    learner = normalise_learner(learner)
    tz = learner["profile"].get("timezone", plan.get("timezone", "UTC"))
    today = _now(now, tz).date()
    resources = {r["source_key"]: r for r in plan.get("records", []) if r.get("kind") == "resources"}
    tasks = []
    for record in plan.get("records", []):
        if record.get("kind") != "tasks":
            continue
        key = record["source_key"]
        personal = learner["personal"].get(key, {})
        if task_status(record, learner, include_optional=include_optional):
            continue
        props = record.get("properties", {})
        progress = learner["task_progress"].get(key, {})
        preparation = record.get("preparation", {})
        generated = record.get("generated_content", {})
        generated = generated if isinstance(generated, Mapping) else {}
        observations = _list(record.get("observations", generated.get("observations", generated.get("evidence_observations", []))))
        observations = [item for item in observations if isinstance(item, Mapping)]
        evidence_status = props.get("Evidence Status")
        partial_summaries = [str(item.get("summary")) for item in observations if item.get("summary")]
        source_remaining = [str(item.get("remaining_work")) for item in observations if item.get("remaining_work")]
        due = _date(props.get("date:Due:start", props.get("Due")), tz)
        target = _date(props.get("date:Study Date:start", props.get("Study Date")), tz)
        planned = _date(personal.get("Planned"), tz)
        conflict = props.get("Due Conflict") in (True, "__YES__")
        materials = []
        materials.extend(deepcopy(record.get("note_materials", [])))
        for resource_key in _list(props.get("Resources")):
            resource = resources.get(resource_key)
            if resource:
                rp = resource.get("properties", {})
                materials.append({"source_key": resource_key, "title": rp.get("Name"), "url": rp.get("Source URL"),
                                  "local_path": rp.get("Local Path"), "availability": rp.get("Download Status")})
            else:
                materials.append({"source_key": resource_key, "availability": "not_indexed"})
        if props.get("Source URL"):
            materials.append({"source_key": key, "title": "来源要求", "url": props["Source URL"]})
        material_urls = {m.get("url") for m in materials if m.get("url")}
        for ref in _sources(record):
            if isinstance(ref, str) and ref.startswith(("https://", "http://")) and ref not in material_urls:
                materials.append({"source_key": ref, "title": "来源材料", "url": ref})
                material_urls.add(ref)
        steps = _steps(record, personal, progress, learner)
        reasons = []
        if planned:
            reasons.append("优先保留你的计划日期 " + planned.isoformat())
        if due:
            delta = (due - today).days
            reasons.append("已过截止日期，需核查并处理" if delta < 0 else f"距截止日期 {delta} 天")
        elif conflict:
            reasons.append("来源截止日期冲突，需核实；任务仍保留")
        elif target:
            reasons.append("课前准备日期已过，安排复盘或补读；这不是官方截止日期" if target < today else "来源课程准备日期 " + target.isoformat() + "（非官方截止日期）")
            reasons.append("只按已知日期安排；具体课前时段需依据已确认的上课时刻")
        else:
            reasons.append("来源未公布截止日期，任务仍保留")
        if evidence_status == "partial_completed":
            reasons.append("来源只确认部分完成：" + ("；".join(partial_summaries) if partial_summaries else "已有部分通过；剩余要求仍需核对"))
        if due and planned and planned > due:
            reasons.append("你的计划日期晚于来源截止日期，需处理冲突；保留原计划日期")
        if progress.get("difficulty") in ("hard", "stuck", "困难", "卡住"):
            reasons.append("上次反馈有困难，先处理记录的卡点")
        priority = str(personal.get("Priority", "Normal") or "Normal")
        before = preparation.get("prepare_before") or props.get("date:Prepare Before:start")
        if before:
            reasons.append("课节／考核之前准备：" + str(before) + "；建议日期不是官方截止")
        if props.get("Planning Origin"):
            reasons.append({"Teacher requirement":"教师要求", "Teacher recommendation":"教师推荐", "Assistant suggestion":"助手建议"}.get(props["Planning Origin"], props["Planning Origin"]))
        tasks.append({"id": key, "source_key": key, "title": props.get("Name", key),
                      "planning_origin":props.get("Planning Origin"), "learning_activity":props.get("Learning Activity"),
                      "prepare_before":before, "not_before":preparation.get("not_before"),
                      "trigger_id":preparation.get("trigger_id"), "class_kind":preparation.get("class_kind"),
                      "preparation_status":props.get("Preparation Status"),
                      "course": _list(props.get("Course")), "type": props.get("Type", "task"),
                      "scope": props.get("Scope"),
                      "study_mode": record.get("study_mode", generated.get("study_mode", props.get("Study Mode"))),
                      "due": due.isoformat() if due else None, "due_at": props.get("date:Due:start", props.get("Due")), "due_conflict": conflict,
                      "target_date": target.isoformat() if target else None, "effective_date": (due or target).isoformat() if (due or target) else None,
                      "deadline_kind": "official" if due else "preparation" if target else "unknown",
                      "due_choices": props.get("Due Choices", []), "planned": deepcopy(personal.get("Planned")),
                      "planned_date": planned.isoformat() if planned else None, "priority": priority,
                      "personal_notes": personal.get("Personal Notes"), "source_refs": _sources(record),
                      "materials": materials, "steps": steps,
                      "estimated_minutes": sum(s["estimated_minutes"] for s in steps if not s["done"]),
                      "remaining_work": progress.get("remaining_work") or ("；".join(source_remaining) if source_remaining else "核对未完成的来源要求" if evidence_status == "partial_completed" else None),
                      "difficulty": progress.get("difficulty"), "evidence_status": evidence_status, "source_observations": deepcopy(observations),
                      "reason": reasons, "completion_is_mastery": False})
    def sort_key(task):
        planned = task["planned_date"]
        due = _date(task["due"], tz)
        before = _date(task.get("prepare_before"), tz)
        obligation = min([x for x in (due, before if task.get("planning_origin") == "Teacher requirement" else None) if x], default=None)
        urgent = 0 if obligation and obligation <= today + timedelta(days=1) else 1 if obligation and obligation <= today + timedelta(days=3) else 2
        activity_rank = 0 if task.get("learning_activity") == "Exam preparation" and before and before <= today+timedelta(days=7) else 1 if before and before <= today+timedelta(days=3) else 2 if task.get("planning_origin") == "Teacher recommendation" else 3
        return (urgent, task["due"] or "9999-12-31" if urgent < 2 else "",
                0 if planned and planned <= today.isoformat() else 1 if planned else 2,
                planned or "9999-12-31", {"High": 0, "高": 0, "Normal": 1, "Low": 2, "低": 2}.get(task["priority"], 1),
                activity_rank, task["effective_date"] or "9999-12-31", task["id"])
    return sorted(tasks, key=sort_key)


def _capacity(availability: Any, start: date, now: datetime, tz: str, busy: list | None = None) -> tuple[int | None, list, list]:
    """Only explicit available minutes/slots become capacity; unknown stays null."""
    warnings, slots = [], []
    if availability is None:
        return None, slots, warnings
    if isinstance(availability, (int, float)) and not isinstance(availability, bool):
        if availability < 0:
            raise ValueError("Available minutes must be nonnegative")
        return int(availability), slots, warnings
    if isinstance(availability, list):
        availability = {"slots": availability}
    if not isinstance(availability, Mapping):
        raise ValueError("Availability must contain explicit weekly_minutes or dated slots")
    if "slots" not in availability:
        minutes = availability.get("weekly_minutes", availability.get("minutes"))
        if minutes is None:
            return None, slots, ["available_time_unknown"]
        if float(minutes) < 0:
            raise ValueError("Available minutes must be nonnegative")
        return int(minutes), slots, warnings
    lower = _dt(start, tz)
    upper = lower + timedelta(days=7)
    intervals = []
    for slot in availability["slots"]:
        a, b = _dt(slot["start"], tz), _dt(slot["end"], tz)
        if b <= a:
            raise ValueError("Availability slot end must be after start")
        a, b = max(a, lower, now), min(b, upper)
        if b > a:
            intervals.append((a, b))
    # Merge overlaps to prevent double-counting a student's available time.
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    # Remove confirmed classes before deriving the 80% study budget.
    for event in busy or []:
        a, b = _dt(event["start"], tz), _dt(event["end"], tz)
        fragments = []
        for left, right in merged:
            if b <= left or a >= right:
                fragments.append((left, right))
            else:
                if a > left: fragments.append((left, a))
                if b < right: fragments.append((b, right))
        merged = fragments
    for a, b in merged:
        slots.append({"start": a, "end": b, "cursor": a, "minutes": int((b-a).total_seconds()//60)})
    return sum(s["minutes"] for s in slots), slots, warnings


def build_week_plan(plan: Mapping, learner: Mapping | None, week: Any = None,
                    availability: Any = None, now: Any = None) -> dict:
    learner = normalise_learner(learner)
    tz = learner["profile"].get("timezone", plan.get("timezone", "UTC"))
    current = _now(now, tz)
    requested = _date(week, tz) if week else current.date()
    if requested is None:
        raise ValueError("Week must be an ISO date")
    start = requested - timedelta(days=requested.weekday())
    end, horizon = start + timedelta(days=6), start + timedelta(days=20)
    if availability is None:
        availability = learner["profile"].get("availability_by_week", {}).get(start.isoformat())
    available, slots, warnings = _capacity(availability, start, current, tz, plan.get("class_schedule",{}).get("busy_slots",[]))
    budget = math.floor(available * 0.8) if available is not None else None
    remaining_budget = budget or 0
    tasks = task_candidates(plan, learner, now=current)
    inactive = [{"id": r["source_key"], "title": r.get("properties", {}).get("Name", r["source_key"]),
                 "status": task_status(r, learner), "source_refs": _sources(r), "mastery_status": "not_assessed"}
                for r in plan.get("records", []) if r.get("kind") == "tasks" and task_status(r, learner)]
    retired = [task for task in inactive if task["status"].startswith("advice_")]
    completed = [task for task in inactive if not task["status"].startswith("advice_") and task["status"] not in ("historical_out_of_scope", "optional_not_selected", "source_confirmed_completed", "reference_not_actionable")]
    source_completed = [task for task in inactive if task["status"] == "source_confirmed_completed"]
    historical = [task for task in inactive if task["status"] == "historical_out_of_scope"]
    optional = [task for task in inactive if task["status"] == "optional_not_selected"]
    reference = [task for task in inactive if task["status"] == "reference_not_actionable"]
    scheduled, unscheduled = [], []
    for task in tasks:
        due, planned = _date(task["due"], tz), _date(task["planned_date"], tz)
        effective = _date(task["effective_date"], tz)
        task["horizon"] = "this_week" if effective and effective <= end else "next_two_weeks" if effective and effective <= horizon else "undated" if not effective else "later"
        task_remaining = 0
        reason = None
        deadline = None
        allocation_target = task.get("prepare_before") or task.get("due_at") or task.get("target_date")
        if allocation_target and not task["due_conflict"]:
            raw_due = allocation_target.get("start") if isinstance(allocation_target, Mapping) else str(allocation_target)
            deadline = _dt(raw_due, tz)
            if len(raw_due) == 10 and not task.get("prepare_before"):
                deadline += timedelta(days=1)
            if task.get("prepare_before") and task.get("due_at") and not task["due_conflict"]:
                official_raw=task["due_at"].get("start") if isinstance(task["due_at"],Mapping) else str(task["due_at"])
                official_deadline=_dt(official_raw,tz)
                if len(official_raw)==10: official_deadline+=timedelta(days=1)
                deadline=min(deadline,official_deadline)
            # Overdue tasks stay actionable with a visible overdue reason.
            if deadline <= current and not task.get("prepare_before"):
                deadline = None
        personal_start = None
        planned_raw = task["planned"].get("start") if isinstance(task["planned"], Mapping) else task["planned"]
        if planned_raw and len(str(planned_raw)) > 10:
            personal_start = _dt(planned_raw, tz)
        if task.get("not_before"):
            earliest = _dt(task["not_before"], tz)
            personal_start = max(personal_start, earliest) if personal_start else earliest
        if planned and not start <= planned <= end:
            reason = "user_plan_outside_week"
        elif deadline and deadline <= current and task.get("prepare_before"):
            reason = "preparation_window_passed"
        elif effective and effective > horizon and not planned:
            reason = "outside_three_week_horizon"
        elif available is None:
            reason = "available_time_unknown"
        elif personal_start and personal_start >= _dt(end+timedelta(days=1), tz):
            reason = "materials_not_available_this_week"
        elif budget == 0:
            reason = "no_available_capacity"
        for step in task["steps"]:
            if step["done"]:
                continue
            left = step["estimated_minutes"]
            if not reason and remaining_budget:
                if slots:
                    for slot in slots:
                        if planned and slot["start"].date() != planned:
                            continue
                        session_start = max(slot["cursor"], personal_start) if personal_start else slot["cursor"]
                        last_end = min(slot["end"], deadline) if deadline else slot["end"]
                        usable = max(0, int((last_end-session_start).total_seconds()//60))
                        take = min(left, remaining_budget, slot["minutes"], usable)
                        if take <= 0:
                            continue
                        session_end = session_start + timedelta(minutes=take)
                        scheduled.append({"task_id": task["id"], "step_id": step["id"], "title": step["title"],
                                          "minutes": take, "start": session_start.isoformat(), "end": session_end.isoformat(),
                                          "source_refs": step["source_refs"], "user_planned": bool(planned)})
                        slot["cursor"], slot["minutes"] = session_end, max(0, int((slot["end"]-session_end).total_seconds()//60))
                        remaining_budget, left = remaining_budget-take, left-take
                        if not left or not remaining_budget:
                            break
                else:
                    take = min(left, remaining_budget)
                    scheduled.append({"task_id": task["id"], "step_id": step["id"], "title": step["title"], "minutes": take,
                                      "date": planned.isoformat() if planned else None, "start": personal_start.isoformat() if personal_start else None, "end": None,
                                      "source_refs": step["source_refs"], "user_planned": bool(planned)})
                    remaining_budget, left = remaining_budget-take, left-take
            task_remaining += left
        if task_remaining or not task["steps"]:
            unscheduled.append({"task_id": task["id"], "title": task["title"], "minutes": task_remaining,
                                "reason": reason or ("preparation_date_capacity_insufficient" if deadline and task["deadline_kind"] == "preparation" and slots and remaining_budget else "deadline_capacity_insufficient" if deadline and slots and remaining_budget else "user_plan_has_no_matching_slot" if planned and slots and remaining_budget else "capacity_exceeded"),
                                "source_refs": task["source_refs"], "planned": task["planned"]})
    if not learner["profile"].get("timetable_confirmed"):
        warnings.append("personal_timetable_unknown_no_class_times_inferred")
    warnings.extend(plan.get("class_schedule",{}).get("unknowns",[]))
    if available is None:
        warnings.append("complete_draft_without_time_slots")
    result = {"schema_version": 2, "id": week_identity(plan, start), "term": _term(plan),
              "title": learner["weeks"].get(start.isoformat(), {}).get("title") or "Week of " + start.isoformat(),
              "week_start": start.isoformat(), "week_end": end.isoformat(), "horizon_end": horizon.isoformat(),
              "timezone": tz, "generated_at": timestamp(current), "status": "draft" if available is None else "planned",
              "availability": deepcopy(availability), "available_minutes": available, "capacity_ratio": 0.8,
              "budget_minutes": budget, "scheduled_minutes": sum(s["minutes"] for s in scheduled),
              "tasks": tasks, "completed_tasks": completed, "source_completed_tasks": source_completed, "historical_tasks": historical, "optional_tasks": optional, "reference_tasks": reference, "retired_advice":retired, "scheduled": scheduled, "unscheduled": unscheduled, "warnings": warnings}
    result = attach_layout(result, plan)
    result["records"] = [_week_record(result)]
    return result


def _record_reference(key: str) -> str:
    """A symbolic page mention resolved by the outbox after database binding."""
    return "[[record:" + key + "]]"


def _fold(title: str, rows: list[str]) -> list[str]:
    return ["<details>", "<summary>" + title + "</summary>",
            *["\t" + row for row in (rows or ["暂无事项。"])] , "</details>"]


def _week_record(week: Mapping) -> dict:
    props = {"Name": week.get("title") or "Week of " + week["week_start"], "Source Key": week["id"], "Term": week["term"],
             "date:Start:start": week["week_start"], "date:Start:is_datetime": 0,
             "date:End:start": week["week_end"], "date:End:is_datetime": 0, "Status": week["status"],
             "Scheduled Minutes": week["scheduled_minutes"], "Tasks": [t["id"] for t in week["tasks"]]}
    for key, name in (("available_minutes", "Available Minutes"), ("budget_minutes", "Budget Minutes")):
        if week[key] is not None:
            props[name] = week[key]
    return {"kind": "weeks", "source_key": week["id"], "properties": props, "generated_content": render_week(week)}


def study_start(plan: Mapping, learner: Mapping | None, minutes: int, preferred_task: str | None = None,
                now: Any = None, week_plan: Mapping | None = None) -> tuple[dict, dict]:
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
        raise ValueError("Study minutes must be a positive integer")
    result = normalise_learner(learner)
    candidates = task_candidates(plan, result, now, include_optional=bool(preferred_task))
    verification = [task for task in candidates if _verification_scope(task.get("scope"))]
    selection_reasons = []
    if preferred_task:
        matches = [t for t in candidates if preferred_task in (t["id"], t["title"])]
        if len(matches) != 1:
            raise ValueError("Preferred task must identify one unfinished task by source key or exact title")
        chosen = matches[0]
        selection_reasons.append("按你指定的首选任务开始")
    elif candidates:
        learning = [task for task in candidates if not _verification_scope(task.get("scope"))]
        pool = learning or candidates
        chosen = pool[0]
        if learning and verification:
            selection_reasons.append("本次时段优先用于课程学习；行政与状态核实另列提醒")
        elif not learning:
            selection_reasons.append("当前没有未完成的课程学习项，先核实行政事项的状态与下一步")
        tz = result["profile"].get("timezone", plan.get("timezone", "UTC"))
        today = _now(now, tz).date()
        target = _date(chosen["effective_date"], tz)
        # Keep deadline/Planned ordering, only resolving ties on the selected
        # imminent day from an explicit reviewed mode, never a title guess.
        if learning and target and target <= today + timedelta(days=3) and (chosen["due"] or target >= today):
            same_day = [task for task in pool if task["effective_date"] == target.isoformat()]
            mode_rank = {"assessment_preparation": 0, "logistics": 2}
            chosen = min(same_day, key=lambda task: mode_rank.get(task.get("study_mode"), 1))
            if len(same_day) > 1 and chosen.get("study_mode") == "assessment_preparation":
                selection_reasons.append("同一临近日期有明确的考核准备，优先处理考核学习")
        # Resolve equivalent suggestions using the available block, without
        # letting a short low-priority task displace an urgent obligation.
        equivalent=[t for t in pool if t.get("effective_date")==chosen.get("effective_date")
                    and t.get("planning_origin")==chosen.get("planning_origin")
                    and t.get("learning_activity")==chosen.get("learning_activity")
                    and t.get("priority")==chosen.get("priority") and t.get("planned_date")==chosen.get("planned_date")]
        fitting=[t for t in equivalent if t["estimated_minutes"]<=minutes]
        if fitting and chosen["estimated_minutes"]>minutes:
            chosen=min(fitting,key=lambda t:(t["estimated_minutes"],t["id"]))
            selection_reasons.append("同等紧迫的安排中，优先选择本次时间能够完成的一项")
    else:
        return result, {"status": "no_unfinished_tasks", "minutes": minutes, "reason": "当前来源任务均已明确完成或尚无任务；不推断知识掌握情况。"}
    next_step = next((s for s in chosen["steps"] if not s["done"]), None)
    # Steps can be done while the official task still needs user confirmation.
    if next_step is None:
        next_step = {"id": chosen["id"] + ":step:confirm", "title": "核实任务完成状态", "estimated_minutes": 5,
                     "completion_criteria": "检查交付物或提交结果，明确报告任务是否完成。", "source_refs": chosen["source_refs"]}
    focus = next_step["title"]
    if chosen["difficulty"] in ("hard", "stuck", "困难", "卡住"):
        focus = "先处理上次卡点：" + str(chosen["remaining_work"] or next_step["title"])
    if week_plan is None:
        tz = result["profile"].get("timezone", plan.get("timezone", "UTC"))
        today = _now(now, tz).date()
        week_start = (today-timedelta(days=today.weekday())).isoformat()
        week_plan = result["weeks"].get(week_start)
        if week_plan is None:
            week_plan = build_week_plan(plan, result, now=now)
    week_copy = {k: deepcopy(v) for k, v in week_plan.items() if k != "records"}
    result["weeks"][week_copy["week_start"]] = week_copy
    identifier = _namespace(plan) + ":session:" + uuid4().hex
    session = {"id": identifier, "term": _term(plan), "task_id": chosen["id"], "task_title": chosen["title"],
               "scope": chosen.get("scope"), "study_mode": chosen.get("study_mode"),
               "selection_kind": "verification" if _verification_scope(chosen.get("scope")) else "study",
               "verification_reminders": [{"task_id": task["id"], "title": task["title"], "scope": task["scope"],
                                           "due": task["due"], "planned": task["planned"], "reason": task["reason"]}
                                          for task in verification if task["id"] != chosen["id"]],
               "step_id": next_step["id"], "week_id": week_plan["id"], "status": "started", "started_at": timestamp(now),
               "minutes": minutes, "suggested_minutes": max(1, min(minutes, next_step["estimated_minutes"])),
               "focus": focus, "reason": selection_reasons + chosen["reason"],
               "materials": chosen["materials"], "source_refs": chosen["source_refs"],
               "completion_criteria": next_step["completion_criteria"], "remaining_work": chosen["remaining_work"],
               "mastery_status": "not_assessed"}
    result["sessions"][identifier] = deepcopy(session)
    result["updated_at"] = timestamp(now)
    return result, session


def study_finish(learner: Mapping | None, session_id: str, feedback: Mapping,
                 now: Any = None) -> tuple[dict, dict]:
    result = normalise_learner(learner)
    if session_id not in result["sessions"]:
        raise ValueError("Unknown study session")
    if not isinstance(feedback, Mapping):
        raise ValueError("Feedback must be a JSON object")
    session = result["sessions"][session_id]
    if session.get("status") == "finished":
        if session.get("feedback") == feedback:
            return result, deepcopy(session)
        raise ValueError("Session already finished with different feedback; preserve original feedback")
    spent = feedback.get("spent_minutes", feedback.get("minutes", session["minutes"]))
    if not isinstance(spent, (int, float)) or isinstance(spent, bool) or spent < 0:
        raise ValueError("Spent minutes must be nonnegative")
    session.update({"status": "finished", "finished_at": timestamp(now), "spent_minutes": spent,
                    "feedback": deepcopy(dict(feedback)), "mastery_status": "not_assessed"})
    key = session["task_id"]
    progress = result["task_progress"].setdefault(key, {})
    progress["minutes_spent"] = progress.get("minutes_spent", 0) + spent
    progress["last_session_id"] = session_id
    progress["updated_at"] = timestamp(now)
    if "remaining_work" in feedback:
        progress["remaining_work"] = deepcopy(feedback["remaining_work"])
        session["remaining_work"] = deepcopy(feedback["remaining_work"])
    if "difficulty" in feedback:
        progress["difficulty"] = feedback["difficulty"]
    if feedback.get("step_completed") is True:
        progress["completed_steps"] = list(dict.fromkeys(progress.get("completed_steps", []) + [session["step_id"]]))
        expected = max(1, session.get("suggested_minutes", session["minutes"]))
        observed_scale = max(0.5, min(3.0, spent / expected))
        progress["estimate_scale"] = round(0.5 * progress.get("estimate_scale", 1) + 0.5 * observed_scale, 3)
    elif feedback.get("difficulty") in ("hard", "stuck", "困难", "卡住"):
        progress["estimate_scale"] = min(3.0, round(progress.get("estimate_scale", 1) * 1.25, 3))
    if "remaining_minutes" in feedback:
        if not isinstance(feedback["remaining_minutes"], (int, float)) or feedback["remaining_minutes"] < 0:
            raise ValueError("Remaining minutes must be nonnegative")
        progress["remaining_minutes"] = feedback["remaining_minutes"]
    # Only the separate explicit user assertion completes an official task.
    if "task_completed" in feedback:
        if not isinstance(feedback["task_completed"], bool):
            raise ValueError("task_completed must be a boolean")
        personal = result["personal"].setdefault(key, {})
        personal.update({"Done": feedback["task_completed"], "updated_at": timestamp(now), "source": "explicit_session_feedback"})
    result["updated_at"] = timestamp(now)
    return result, deepcopy(session)


def mentor_records(learner: Mapping | None, week_plans: Any = None) -> list:
    learner = normalise_learner(learner)
    weeks = dict(learner["weeks"])
    if week_plans:
        items = week_plans.values() if isinstance(week_plans, Mapping) and "week_start" not in week_plans else _list(week_plans)
        for week in items:
            weeks[week["week_start"]] = week
    records = [_week_record(week) for _, week in sorted(weeks.items())]
    for key, session in sorted(learner["sessions"].items()):
        props = {"Name": session["task_title"] + " · " + session["started_at"][:10], "Source Key": key,
                 "Term": session["term"], "Task": [session["task_id"]], "Week": [session["week_id"]],
                 "date:Started:start": session["started_at"], "date:Started:is_datetime": 1,
                 "Minutes": session.get("spent_minutes", session["minutes"]), "Status": session["status"]}
        if session.get("finished_at"):
            props.update({"date:Finished:start": session["finished_at"], "date:Finished:is_datetime": 1})
        lines = ["任务：" + session["task_title"] + " " + _record_reference(session["task_id"]),
                 "所属周：" + _record_reference(session["week_id"]),
                 "本次重点：" + session["focus"], "理由：" + "；".join(session["reason"]),
                 "完成标准：" + str(session["completion_criteria"]), "完整要求与来源见关联任务。"]
        for material in session["materials"]:
            destination = str(material.get("url") or material.get("local_path") or "")
            if "|user=" in destination or "|type=" in destination:
                continue
            title = str(material.get("title") or "材料待核对")
            lines.append("材料：" + title + (" " + destination if destination else ""))
        if session.get("feedback") is not None:
            lines.append("学习反馈：" + str(session["feedback"]))
        if session.get("remaining_work"):
            lines.append("剩余工作：" + str(session["remaining_work"]))
        if session.get("verification_reminders"):
            reminders = ["- " + reminder["title"] + " " + _record_reference(reminder["task_id"]) +
                         (" · 来源日期 " + reminder["due"] if reminder.get("due") else " · 日期待核实")
                         for reminder in session["verification_reminders"]]
            lines.append("\n".join(_fold("行政与状态核实提醒", reminders)))
        lines.append("知识掌握：未评估；活动完成不代表掌握。")
        records.append({"kind": "sessions", "source_key": key, "properties": props, "generated_content": "\n\n".join(lines)})
    return records

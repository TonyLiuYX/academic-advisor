"""Coarse, source-backed class preparation. No teaching or mastery inference."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

KINDS = {"lecture", "lab", "pra", "tutorial"}
ORIGINS = {"Teacher requirement", "Teacher recommendation", "Assistant suggestion"}


def task_key(item):
    return item.get("canonical_task_key") or item["course_key"] + "|preparation=" + item["id"]


def _day(value):
    return date.fromisoformat(str(value)[:10])


def _moment(day, clock, tz):
    return datetime.fromisoformat(str(day) + "T" + clock).replace(tzinfo=ZoneInfo(tz)).isoformat() if clock else None


def occurrences(payload, tz):
    """Keep original occurrence identity when its actual date/time changes."""
    result, gaps = [], list(payload.get("unknowns", []))
    ids = set()
    for series in payload.get("series", []):
        sid = str(series["id"])
        if sid in ids or series.get("kind") not in KINDS or not series.get("source_refs"):
            raise ValueError("Class series needs unique id, supported kind and sources: " + sid)
        ids.add(sid)
        if not series.get("confirmed"):
            gaps.append({"series_id": sid, "course_key": series["course_key"], "reason": "Personal section/date unconfirmed"})
            continue
        explicit = series.get("occurrences")
        if explicit is None:
            first, last = _day(series["start_date"]), _day(series["end_date"])
            interval = int(series.get("interval_weeks", 1))
            if interval < 1 or last < first:
                raise ValueError("Invalid class recurrence: " + sid)
            anchor = first - timedelta(days=first.weekday())
            explicit = []
            cursor = first
            while cursor <= last:
                if cursor.weekday() in series.get("weekdays", []) and ((cursor-anchor).days//7) % interval == 0:
                    explicit.append({"id": cursor.isoformat(), "date": cursor.isoformat()})
                cursor += timedelta(days=1)
            explicit.extend(series.get("additional_occurrences", []))
        seen = set()
        for raw in explicit:
            oid = str(raw.get("id") or raw["date"])
            if oid in seen:
                raise ValueError("Duplicate class occurrence: " + sid + ":" + oid)
            seen.add(oid)
            clocks = series.get("times_by_weekday", {}).get(str(_day(raw["date"]).weekday()), {})
            entry = {**deepcopy(series), **deepcopy(clocks), **deepcopy(raw)}
            exception = series.get("exceptions", {}).get(oid, {})
            entry.update(exception)
            day = _day(entry["date"])
            entry.update({"series_id": sid, "occurrence_id": sid + ":" + oid,
                          "date": day.isoformat(), "cancelled": bool(entry.get("cancelled") or day.isoformat() in series.get("exclude_dates", [])),
                          "start": _moment(day, entry.get("start_time"), tz),
                          "end": _moment(day, entry.get("end_time"), tz)})
            if entry["start"] and entry["end"] and entry["end"] <= entry["start"]:
                raise ValueError("Class end must follow start: " + entry["occurrence_id"])
            result.append(entry)
    return sorted(result, key=lambda x: (x["date"], x.get("start") or "", x["occurrence_id"])), gaps


def compile_preparations(plan, payload, legacy, previous=None, learner=None, week=None, now=None):
    """Return deterministic inputs plus a durable ledger of generated identities."""
    tz = plan.get("timezone", "UTC")
    current = datetime.fromisoformat(str(now).replace("Z", "+00:00")) if now else datetime.now(ZoneInfo(tz))
    current = current.replace(tzinfo=ZoneInfo(tz)) if current.tzinfo is None else current.astimezone(ZoneInfo(tz))
    target = _day(week) if week else current.date()
    start = target - timedelta(days=target.weekday())
    end = start + timedelta(days=20)
    classes, gaps = occurrences(payload, tz)
    ledger = deepcopy((previous or {}).get("items", {}))
    records = {r["source_key"]: r for r in plan["records"]}
    old_items = {x["id"]: x for x in legacy.get("preparations", [])}
    bindings = {x["occurrence_id"]: x for x in payload.get("bindings", [])}
    valid = set()
    class_ids = {c["occurrence_id"] for c in classes}
    busy = [{"start": c["start"], "end": c["end"], "id": c["occurrence_id"]}
            for c in classes if not c["cancelled"] and c["start"] and c["end"] and start <= _day(c["date"]) <= end]

    def add(item, event_date, cancelled=False):
        rid = item["rule_id"]
        valid.add(rid)
        if rid not in ledger and (cancelled or not start <= event_date <= end):
            return
        if rid in ledger and item.get("canonical_task_key") is None and ledger[rid].get("canonical_task_key"):
            item["canonical_task_key"] = ledger[rid]["canonical_task_key"]
        if item.get("origin", "Assistant suggestion") not in ORIGINS:
            raise ValueError("Unknown preparation origin")
        item["rule_managed"] = True
        item["preparation_status"] = "Cancelled" if cancelled else "Deferred" if event_date > end else "Active"
        before = item.get("prepare_before")
        expired = event_date < current.date() or bool(before and "T" in before and datetime.fromisoformat(before) <= current)
        if expired and item.get("origin") == "Assistant suggestion":
            item["preparation_status"] = "Past"
            if rid not in ledger and not item.get("canonical_task_key"):
                return
        item["study_date"] = max(_day(item["study_date"]), current.date()).isoformat() if event_date >= current.date() else item["study_date"]
        # Once created, an unchanged recommendation does not drift every day.
        old = ledger.get(rid)
        if old and old.get("prepare_before") == item.get("prepare_before") and old.get("event_date") == item.get("event_date"):
            item["study_date"] = old.get("study_date", item["study_date"])
        ledger[rid] = item

    preceding = {}
    for c in classes:
        rid = "class:" + c["occurrence_id"]
        binding = bindings.get(c["occurrence_id"], {})
        base = deepcopy(old_items.get(binding.get("preparation_id"), {}))
        if binding.get("preparation_id") and not base:
            raise ValueError("Unknown existing preparation in class binding")
        canonical = binding.get("canonical_task_key") or (task_key(base) if base else None)
        label = c.get("label", c["kind"])
        steps = deepcopy(base.get("study_steps") or c.get("study_steps") or [])
        if c["kind"] == "lecture":
            generic = [{"id": "review-notes", "title": "回顾上次 lecture 的笔记", "estimated_minutes": 10},
                       {"id": "preview-materials", "title": "浏览本次 lecture 已发布的材料与要求", "estimated_minutes": 15}]
        else:
            generic = [{"id": "check-class-materials", "title": "查看本次 " + c["kind"] + " 材料与要求，回顾相关笔记", "estimated_minutes": 20}]
        if not steps:
            steps = generic
        elif c["kind"] == "lecture" and not any(s.get("id") == "review-notes" for s in steps):
            steps.insert(0, generic[0])
        for s in steps:
            s.setdefault("completion_criteria", "完成所列准备；实际完成与剩余事项由学生反馈。")
        item = {"id": "rule:" + rid, "rule_id": rid, "series_id": c["series_id"], "trigger_id": c["occurrence_id"],
                "course_key": c["course_key"], "title": c.get("preparation_title") or label + " · " + c["date"] + " 课前准备",
                "origin": base.get("origin", "Teacher requirement" if base else c.get("origin", "Assistant suggestion")),
                "activity_type": "Class preparation", "class_kind": c["kind"], "event_date": c["date"],
                "study_date": (_day(c["date"])-timedelta(days=1)).isoformat(), "prepare_before": c["start"] or c["date"],
                "not_before": c.get("available_from"), "source_refs": list(dict.fromkeys(base.get("source_refs", []) + c["source_refs"])),
                "source_url": c.get("source_url") or base.get("source_url"),
                "resource_keys": list(dict.fromkeys(base.get("resource_keys", []) + c.get("resource_keys", []))),
                "study_steps": steps, "linked_task_keys": c.get("linked_task_keys", []),
                "study_mode": base.get("study_mode", "class_preparation")}
        earlier = preceding.get(c["series_id"])
        lower_bounds = [v for v in (earlier, item.get("not_before")) if v]
        if lower_bounds:
            item["not_before"] = max(lower_bounds)
        if not c["cancelled"]:
            preceding[c["series_id"]] = c["end"] or (str(_day(c["date"])+timedelta(days=1)))
        if canonical:
            item["canonical_task_key"] = canonical
            # A canonical official task keeps its obligation; rule metadata only annotates preparation.
            if records.get(canonical, {}).get("properties", {}).get("date:Due:start"):
                item["origin"] = "Teacher requirement"
        add(item, _day(c["date"]), c["cancelled"])

    for assessment in payload.get("assessments", []):
        if assessment.get("kind") not in ("quiz", "midterm", "final") or not assessment.get("source_refs"):
            raise ValueError("Assessment requires reviewed kind and sources")
        when = assessment.get("date")
        canonical = assessment.get("canonical_task_key")
        official = records.get(canonical, {}).get("properties", {})
        when = official.get("date:Due:start") or when
        if not when or official.get("Due Conflict") in (True, "__YES__"):
            gaps.append({"assessment_id": assessment["id"], "reason": "Assessment date missing/conflicting"})
            continue
        day = _day(when)
        phases = [("review", int(payload.get("quiz_lead_days", 2)))] if assessment["kind"] == "quiz" else [("scope", int(payload.get("exam_lead_days", 7))), ("review", int(payload.get("exam_final_lead_days", 2)))]
        for phase, lead in phases:
            suggested = day-timedelta(days=lead)
            if phase == "scope" and suggested < current.date() and "assessment:" + str(assessment["id"]) + ":" + phase not in ledger:
                continue
            rid = "assessment:" + str(assessment["id"]) + ":" + phase
            item = {"id": "rule:" + rid, "rule_id": rid, "trigger_id": str(assessment["id"]),
                    "course_key": assessment["course_key"], "title": assessment["title"] + (" · 整理范围与复习材料" if phase == "scope" else " · 考前回顾与已有练习"),
                    "origin": "Assistant suggestion", "activity_type": "Exam preparation", "study_mode": "assessment_preparation",
                    "event_date": day.isoformat(), "study_date": suggested.isoformat(), "prepare_before": when,
                    "source_refs": assessment["source_refs"], "source_url": assessment.get("source_url"),
                    "resource_keys": assessment.get("resource_keys", []), "linked_task_keys": [canonical] if canonical else [],
                    "study_steps": [{"id": phase, "title": assessment.get("review_title", "按已公布范围回顾笔记和课程练习材料"), "estimated_minutes": 45 if phase == "scope" else 60,
                                     "completion_criteria": "完成本次安排，记录尚未做完的事项；不包含出题或掌握度诊断。"}]}
            if assessment.get("preparation_id") and phase == "review":
                prep = old_items[assessment["preparation_id"]]
                item.update({"canonical_task_key": task_key(prep), "origin": prep.get("origin", "Teacher requirement"), "study_steps": deepcopy(prep.get("study_steps", item["study_steps"]))})
            # A study block in this horizon can prepare for an exam just beyond it.
            add(item, max(suggested, current.date()) if day >= current.date() else day, assessment.get("cancelled", False))

    for rec in payload.get("recommendations", []):
        if not rec.get("source_refs"):
            raise ValueError("Recommended practice needs a reviewed source")
        item = deepcopy(rec)
        rid = "recommendation:" + str(item["id"])
        item.update({"id": "rule:" + rid, "rule_id": rid, "trigger_id": rec["id"],
                     "origin": "Teacher recommendation", "activity_type": "Recommended practice", "event_date": item["study_date"]})
        if item.get("preparation_id"):
            old = old_items[item["preparation_id"]]
            item["canonical_task_key"] = task_key(old)
            item.setdefault("study_steps", deepcopy(old.get("study_steps", [])))
        add(item, _day(item["study_date"]))

    personal = (learner or {}).get("personal", {})
    for rid, item in ledger.items():
        if rid not in valid:
            item["preparation_status"] = "Needs confirmation"
            gaps.append({"rule_id": rid, "reason": "Previously generated rule no longer verified; retained"})
        if rid.startswith("assessment:") and _day(item["event_date"]) < current.date() and item.get("origin") == "Assistant suggestion":
            item["preparation_status"] = "Past"
        if item.get("origin") != "Assistant suggestion" or item.get("preparation_status") != "Past":
            continue
        if personal.get(task_key(item), {}).get("Done") in (True, "__YES__"):
            continue
        successors = [x for x in ledger.values() if x.get("series_id") and x.get("series_id") == item.get("series_id") and x.get("preparation_status") == "Active" and x["event_date"] > item["event_date"]]
        if successors:
            successor = min(successors, key=lambda x: x["event_date"])
            item["preparation_status"], item["merged_into"] = "Merged", task_key(successor)
            successor["carried_from"] = sorted(set(successor.get("carried_from", []) + [task_key(item)]))
    # Protect canonical obligations from retirement; only synthetic advice is retired.
    for item in ledger.values():
        key = item.get("canonical_task_key")
        if key and records.get(key, {}).get("properties", {}).get("date:Due:start") and item["preparation_status"] != "Active":
            item["preparation_status"] = "Active"
    return {"schema_version": 1, "week_start": start.isoformat(), "horizon_end": end.isoformat(),
            "items": ledger, "classes": [{k: c.get(k) for k in ("occurrence_id", "series_id", "course_key", "label", "kind", "date", "start", "end", "room", "cancelled", "study_steps", "origin")} for c in classes if start <= _day(c["date"]) <= end],
            "busy_slots": busy, "unknowns": gaps}

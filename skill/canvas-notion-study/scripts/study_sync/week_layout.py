"""Calendar presentation of saved plans; no task scheduling or source fetching."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
ORIGINS = {"Teacher requirement": "教师要求", "Teacher recommendation": "教师推荐", "Assistant suggestion": "助手建议"}
KINDS = {"lecture": "Lecture", "tutorial": "Tutorial", "lab": "Lab", "pra": "Practical"}
INACTIVE = (("completed_tasks", "个人完成或 Canvas 已提交"), ("source_completed_tasks", "来源确认完成"),
            ("optional_tasks", "可选活动 · 尚未选择"), ("reference_tasks", "参考信息 · 无需交付"),
            ("historical_tasks", "历史或范围外记录"), ("retired_advice", "已合并、取消或后续周的学习建议"))
REASONS = {"preparation_window_passed": "课前准备窗口已过，需核实或并入后续回顾",
           "materials_not_available_this_week": "材料尚未开放", "available_time_unknown": "可用时间尚未提供",
           "no_available_capacity": "本周没有可安排的时间", "capacity_exceeded": "超出本周可安排容量",
           "deadline_capacity_insufficient": "截止前的可用时段不足", "preparation_date_capacity_insufficient": "课前可用时段不足",
           "user_plan_outside_week": "个人计划日期在本周以外", "user_plan_has_no_matching_slot": "个人计划日期没有对应可用时段",
           "outside_three_week_horizon": "在本周及后两周的范围之外"}


def text(value):
    return re.sub(r"([\\*~`$\[\]<>{}|^])", r"\\\1", str(value or "")).replace("\n", "<br>")


def ref(key):
    return "[[record:" + key + "]]"


def local_date(value, tz):
    if isinstance(value, dict):
        value = value.get("start")
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed.astimezone(ZoneInfo(tz)) if parsed.tzinfo else parsed).date()


def when(value, tz, weekday=True):
    if isinstance(value, dict):
        value = value.get("start")
    if not value:
        return "日期待确认"
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    parsed = parsed.astimezone(ZoneInfo(tz)) if parsed.tzinfo else parsed
    label = f"{parsed.month}月{parsed.day}日" + (f"（{WEEKDAYS[parsed.weekday()]}）" if weekday else "")
    if "T" in str(value) or " " in str(value):
        label += " " + parsed.strftime("%H:%M:%S" if parsed.second else "%H:%M")
    return label


def fold(title, rows):
    return ["<details>", "<summary>" + title + "</summary>", *["\t" + r for r in rows or ["暂无事项。"]], "</details>"]


def table(headers, rows):
    lines = ['<table fit-page-width="true" header-row="true">']
    for cells in [headers, *rows]:
        lines += ["\t<tr>", *["\t\t<td>" + cell + "</td>" for cell in cells], "\t</tr>"]
    return lines + ["</table>"]


def attach_layout(week, plan, classes=None):
    """Add presentation inputs without changing the saved plan or personal state."""
    result = deepcopy(week)
    source = classes if classes is not None else plan.get("class_schedule", {}).get("classes", [])
    result["class_sessions"] = [{k: deepcopy(c.get(k)) for k in
        ("occurrence_id", "series_id", "course_key", "label", "kind", "date", "start", "end", "room", "cancelled", "study_steps", "origin")}
        for c in source if week["week_start"] <= c["date"] <= week["horizon_end"]]
    # Retired/completed tasks still describe what an already-passed class required.
    # They do not become active again just because the class is displayed.
    known = {t["id"] for name, _ in INACTIVE for t in week.get(name, [])}
    active = {t["id"] for t in week["tasks"]}
    details = []
    for record in plan.get("records", []):
        key, prep = record.get("source_key"), record.get("preparation", {})
        if record.get("kind") != "tasks" or key not in known or key in active or not prep.get("trigger_id"):
            continue
        props = record.get("properties", {})
        details.append({"id": key, "title": props.get("Name"), "trigger_id": prep["trigger_id"],
                        "prepare_before": prep.get("prepare_before"), "target_date": prep.get("study_date"),
                        "planning_origin": prep.get("origin"), "steps": deepcopy(prep.get("study_steps", [])),
                        "preparation_status": props.get("Preparation Status"), "course": props.get("Course", [])})
    result["class_task_details"] = details
    return result


def timing(task, tz):
    parts = []
    if task.get("planned") or task.get("planned_date"):
        parts.append("个人计划 " + when(task.get("planned") or task["planned_date"], tz))
    if task.get("target_date"):
        parts.append("建议准备 " + when(task["target_date"], tz))
    if task.get("due_conflict"):
        parts.append("官方截止存在冲突 · 待核实")
    elif task.get("due_at") or task.get("due"):
        parts.append("官方截止 " + when(task.get("due_at") or task["due"], tz))
    if task.get("prepare_before"):
        parts.append("课前／考前 " + when(task["prepare_before"], tz))
    return "<br>".join(parts) or "日期待确认"


def summary(task):
    steps = [str(s["title"]) for s in task.get("steps", []) if s.get("title") and not s.get("done")]
    return text("；".join(dict.fromkeys(steps)) or task.get("title") or "查看任务中的具体要求")


def task_rows(task, tz, detailed=True):
    # A native mention already displays the full title. Never repeat it in prose.
    rows = ["- " + ref(task["id"]), "\t" + timing(task, tz)]
    if task.get("planning_origin"):
        rows.append("\t" + ORIGINS.get(task["planning_origin"], text(task["planning_origin"])))
    if detailed:
        for step in task.get("steps", []):
            if not step.get("done"):
                row = "\t- " + text(step.get("title"))
                if step.get("completion_criteria"):
                    row += "；完成标准：" + text(step["completion_criteria"])
                rows.append(row)
        if task.get("remaining_work"):
            rows.append("\t剩余工作：" + text(task["remaining_work"]))
    if task.get("evidence_status") == "partial_completed":
        rows.append("\t来源确认部分完成；" + text("；".join(o["summary"] for o in task.get("source_observations", []) if o.get("summary"))))
    return rows


def render_week(week):
    tz = week.get("timezone", "UTC")
    first, last = date.fromisoformat(week["week_start"]), date.fromisoformat(week["horizon_end"])
    tasks = week.get("tasks", [])
    classes = sorted(week.get("class_sessions", []), key=lambda c: (c["date"], c.get("start") or "99", c["occurrence_id"]))
    class_ids = {c["occurrence_id"] for c in classes if not c.get("cancelled")}
    class_by_id = {c["occurrence_id"]: c for c in classes if not c.get("cancelled")}
    attached, ordinary = {}, []
    for task in [*tasks, *week.get("class_task_details", [])]:
        if task.get("trigger_id") in class_ids:
            attached.setdefault(task["trigger_id"], []).append(task)
        elif task in tasks:
            ordinary.append(task)
    buckets = {}
    overdue, undated, later = [], [], []
    for task in ordinary:
        day = local_date(task.get("planned") or task.get("planned_date") or task.get("target_date") or task.get("effective_date") or task.get("due"), tz)
        if day is None:
            undated.append(task)
        elif day < first:
            overdue.append(task)
        elif day > last:
            later.append(task)
        else:
            buckets.setdefault(day.isoformat(), []).append(task)
    lines = ['<callout icon="🗓️" color="blue_bg">',
             f"\t**每周一至周日 · {text(tz)}**",
             "\t日期草案：可用学习时段尚未提供。表内课节时间来自已确认课表。" if week.get("available_minutes") is None else
             f"\t可用 {week['available_minutes']} 分钟 · 安排上限 {week['budget_minutes']} 分钟 · 已安排 {week['scheduled_minutes']} 分钟。",
             "\t建议准备日为非官方截止；完成勾选保留在原任务中。", '</callout>']
    for offset in range(0, (last-first).days+1, 7):
        monday = first + timedelta(days=offset)
        sunday = monday + timedelta(days=6)
        label = "本周" if offset == 0 else "下一周" if offset == 7 else "再下一周"
        heading = f"{when(monday.isoformat(), tz, False)} — {when(sunday.isoformat(), tz, False)}｜{label}"
        week_lines = ["## " + heading]
        for n in range(7):
            day = monday + timedelta(days=n)
            day_key = day.isoformat()
            week_lines.append(f"### {WEEKDAYS[n]} · {day.month}月{day.day}日" + ' {color="blue_bg"}')
            today_classes = [c for c in classes if c["date"] == day_key]
            rows, detail_rows = [], []
            for c in today_classes:
                label = c.get("label") or c.get("series_id", "课节").replace(":", " ")
                clock = "时间待确认"
                if c.get("start"):
                    clock = when(c["start"], tz).split(" ")[-1]
                    if c.get("end"):
                        clock += "–" + when(c["end"], tz).split(" ")[-1]
                place = " · " + text(c["room"]) if c.get("room") else ""
                cell = f"**{text(label)}**<br>{text(KINDS.get(c['kind'], c['kind']))} · {clock}{place}"
                preparations = attached.get(c["occurrence_id"], [])
                if c.get("cancelled"):
                    rows.append([cell, "停课／取消", "无需为该课节安排准备"])
                elif preparations:
                    notes, dates = [], []
                    for task in preparations:
                        origin = ORIGINS.get(task.get("planning_origin"), "课程准备")
                        notes.append("**" + origin + "**<br>" + summary(task))
                        dates.append(when(task.get("planned") or task.get("planned_date") or task.get("target_date"), tz))
                        detail_rows.extend(task_rows(task, tz, detailed=False))
                    rows.append([cell, "<br>".join(notes), "<br>".join(dict.fromkeys(dates))])
                else:
                    steps = c.get("study_steps") or []
                    preparation = text("；".join(s["title"] for s in steps if s.get("title"))) if steps else "回顾相关笔记，浏览已发布材料；尚无本课节专属准备记录。"
                    rows.append([cell, "**" + ORIGINS.get(c.get("origin"), "助手建议") + "**<br>" + preparation, "课前完成 · 未指定准备日"])
            if rows:
                week_lines.extend(table(["当天课节", "课前准备", "准备日期"], rows))
            else:
                week_lines.append("暂无已确认课节。")
            daily = buckets.get(day_key, [])
            if daily:
                week_lines.append("**当天学习与提交**")
                week_lines.extend(table(["任务", "准备／行动", "日期与截止"],
                                        [[ref(t["id"]), summary(t), timing(t, tz)] for t in daily]))
                for task in daily:
                    detail_rows.extend(task_rows(task, tz, detailed=False))
            advance = [t for t in tasks if t.get("trigger_id") in class_by_id
                       and class_by_id[t["trigger_id"]]["date"] > day_key
                       and local_date(t.get("planned") or t.get("planned_date") or t.get("target_date"), tz) == day]
            if advance:
                week_lines.append("**今天提前准备**")
                advance_rows = []
                for task in sorted(advance, key=lambda t: (class_by_id[t["trigger_id"]].get("start") or class_by_id[t["trigger_id"]]["date"], t["id"])):
                    c = class_by_id[task["trigger_id"]]
                    label = c.get("label") or c.get("series_id", "课节").replace(":", " ")
                    advance_rows.append([text(label) + "<br>上课 " + when(c.get("start") or c["date"], tz),
                                         summary(task), f"约 {task['estimated_minutes']} 分钟" if task.get("estimated_minutes") is not None else "未估时"])
                    detail_rows.extend(task_rows(task, tz, detailed=False))
                week_lines.extend(table(["对应课节", "今天准备什么", "建议用时"], advance_rows))
            # Learning blocks use only times previously scheduled by the planner.
            sessions = [s for s in week.get("scheduled", []) if local_date(s.get("start") or s.get("date"), tz) == day]
            for session in sessions:
                week_lines.append("- 已安排学习：" + when(session.get("start") or session.get("date"), tz) + " · " + text(session["title"]) + f" · {session['minutes']} 分钟")
            if detail_rows:
                week_lines.extend(fold("任务入口 · 打开查看材料与完成标准", detail_rows))
            if n == 6:
                week_lines.append(f"本周截至 {sunday.month}月{sunday.day}日（周日）。" + ' {color="gray"}')
        lines.extend(week_lines if offset == 0 else fold(heading, week_lines[1:]))
        lines.append("---")
    lines.append("## 其他事项")
    for title, group in (("往周未完成 · 待核实或补做", overdue), ("无日期 · 待核实", undated), ("远期", later)):
        rows = [row for task in group for row in task_rows(task, tz)]
        lines.extend(fold(title + f" · {len(group)} 项", rows))
    if week.get("unscheduled"):
        counts = {}
        for item in week["unscheduled"]:
            reason = REASONS.get(item["reason"], "尚需核对安排条件")
            counts[reason] = counts.get(reason, 0) + 1
        lines.append("尚未安排或尚未排完：" + "；".join(f"{reason} {count} 项" for reason, count in counts.items()))
        rows = ["- " + ref(t["task_id"]) + f" · 剩余约 {t['minutes']} 分钟 · " + REASONS.get(t["reason"], "尚需核对安排条件") for t in week["unscheduled"]]
        lines.extend(fold(f"未安排完整清单 · {len(rows)} 项", rows))
    floating = [s for s in week.get("scheduled", []) if not (s.get("start") or s.get("date"))]
    if floating:
        lines.extend(fold("已分配分钟 · 具体时段待定", ["- " + ref(s["task_id"]) + f" · {s['minutes']} 分钟 · " + text(s["title"]) for s in floating]))
    for name, title in INACTIVE:
        group = week.get(name, [])
        if group:
            statuses = {"user_completed": "个人明确完成", "submitted_pending_feedback": "Canvas 已提交，等待反馈", "graded": "Canvas 已评分"}
            lines.extend(fold(title + f" · {len(group)} 项", ["- " + ref(t["id"]) + (" · " + statuses[t["status"]] if t.get("status") in statuses else "") for t in group]))
    return "\n".join(lines)

"""Compose source collection, reviewed requirements and persistent mentoring."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .projection import build_plan
from .state import read_json, write_json, record_fingerprint
from .planning import MENTOR_SCHEMAS, mentor_records


def refresh_mentor(plan, learner):
    result=deepcopy(plan)
    result["databases"].update(deepcopy(MENTOR_SCHEMAS))
    result["records"]=[r for r in result["records"] if r["kind"] not in MENTOR_SCHEMAS]
    result["records"].extend(mentor_records(learner))
    for record in result["records"]:
        record["fingerprint"]=record_fingerprint(record)
    result["stats"]["record_counts"]={kind:sum(r["kind"]==kind for r in result["records"]) for kind in result["databases"]}
    result["stats"]["record_count"]=len(result["records"])
    return result


def build_v1_plan(snapshot, enrichments, config, learner, state_dir, week=None, now=None):
    directory=Path(state_dir)
    plan=build_plan(snapshot,enrichments=enrichments,bindings=config.get("notion",{}).get("databases",{}))
    plan["timezone"]=config["term"]["timezone"]
    plan["databases"]["tasks"]["properties"].update({
        "Scope":{"type":"select"}, "Requirement IDs":{"type":"text"},"Evidence Status":{"type":"select"},
        "Study Date":{"type":"date"}, "Planning Origin":{"type":"select","options":["Teacher requirement","Teacher recommendation","Assistant suggestion"]},
        "Learning Activity":{"type":"select","options":["Class preparation","Exam preparation","Recommended practice"]},
        "Preparation Status":{"type":"select","options":["Active","Cancelled","Merged","Past","Deferred","Needs confirmation"]},
        "Class / Assessment":{"type":"text"}, "Prepare Before":{"type":"date"}})
    plan["databases"]["announcements"]["properties"].update({
        "Scope":{"type":"select"}, "Source":{"type":"text"}})
    plan["databases"]["resources"]["properties"].update({
        "Scope":{"type":"select"}, "Source":{"type":"text"},"Current":{"type":"checkbox"}})
    supplemental=None
    gmail_snapshot=read_json(directory/"gmail-snapshot.json",{})
    if gmail_snapshot:
        from .gmail import validate_reviews,build_supplement
        reviews,warnings=validate_reviews(gmail_snapshot,read_json(directory/"gmail-reviews.json",{}))
        legacy=read_json(directory/"gmail-supplement-20260913.json",{})
        supplemental=build_supplement(gmail_snapshot,reviews,plan,legacy=legacy)
        plan["gmail_review_warnings"]=warnings+supplemental.get("warnings",[])
        overrides=supplemental.get("overrides",{})
        plan["records"]=[overrides.get(r["source_key"],r) for r in plan["records"]]
        existing={r["source_key"] for r in plan["records"]}
        plan["records"].extend(r for r in supplemental.get("records",[]) if r["source_key"] not in existing)
    else:
        # Keep previously published supplemental records until reviewed mail is
        # available. Rebuilding a Canvas projection must not erase email work.
        legacy=read_json(directory/"gmail-supplement-20260913.json",{})
        existing={r["source_key"] for r in plan["records"]}
        plan["records"].extend(deepcopy(r) for r in legacy.get("records",[]) if r["source_key"] not in existing)
    preparations=read_json(directory/"study-preparations.json",{})
    schedule=read_json(directory/"course-schedule.json",{})
    schedule_state=read_json(directory/"schedule-state.json",{})
    preparation_keys={item["course_key"]+"|preparation="+str(item["id"])
                      for item in preparations.get("preparations",[])
                      if not item.get("canonical_task_key") and item.get("course_key") and item.get("id")}
    from .schedule import task_key
    preparation_keys.update(task_key(item) for item in schedule_state.get("items",{}).values() if not item.get("canonical_task_key"))
    dispositions=read_json(directory/"record-dispositions.json",{})
    from .notes import project_notes, link_preparation_notes
    project_notes(plan, read_json(directory/"notion-state.json", {}))
    preserve_published_history(plan,read_json(directory/"notion-state.json",{}),dispositions,
                               projected_keys=preparation_keys)
    from .requirements import build_requirements
    source_requirements=read_json(directory/"canvas-requirements.json",{})
    preparation_requirements=apply_preparations(plan,preparations)
    if schedule:
        from .schedule import compile_preparations
        compiled=compile_preparations(plan,schedule,preparations,schedule_state,learner,week=week,now=now)
        preparation_requirements.extend(apply_preparations(plan,{"preparations":list(compiled["items"].values())}))
        plan["class_schedule"]=compiled
        write_json(directory/"schedule-state.json",compiled)
    link_preparation_notes(plan)
    source_requirements=deepcopy(source_requirements)
    source_requirements.setdefault("requirements",[]).extend(preparation_requirements)
    requirements=build_requirements(plan,supplemental,reviews=source_requirements)
    linked={}
    for requirement in requirements.get("requirements",[]):
        for key in requirement.get("task_keys",[]):
            linked.setdefault(key,[]).append(requirement)
    for record in plan["records"]:
        if record["kind"]!="tasks":
            continue
        entries=linked.get(record["source_key"],[])
        record["requirement_ids"]=list(dict.fromkeys(e["id"] for e in entries))
        record["properties"]["Requirement IDs"]="\n".join(record["requirement_ids"])
        # Explicit source review governs optional scope; a generated-task
        # baseline alone is not a semantic classification.
        reviewed=[e for e in entries if e.get("id")!=record["source_key"]]
        direct=[e for e in source_requirements.get("requirements",[])
                if record["source_key"] in e.get("source_refs",[])]
        if direct and all(e.get("status")=="non_actionable" for e in direct):
            record["properties"]["Scope"]="Reference"
        elif direct and all(e.get("status")=="optional" for e in direct):
            record["properties"]["Scope"]="Optional"
        elif reviewed and all(e.get("status")=="optional" for e in reviewed):
            record["properties"]["Scope"]="Optional"
        else:
            record["properties"].setdefault("Scope","Academic")
    # Explicit dispositions also apply to records created later by a source
    # projection, including current class preparations.
    for record in plan["records"]:
        rule=dispositions.get("records",{}).get(record["source_key"],{})
        if rule.get("scope"):
            record["properties"]["Scope"]=rule["scope"]
        refs=rule.get("source_refs",[])
        if refs:
            record["source_refs"]=list(dict.fromkeys(record.get("source_refs",[])+refs))
            record["properties"]["Source"]="\n".join(dict.fromkeys(str(record["properties"].get("Source","")).splitlines()+refs))
    plan["requirements"]=requirements["requirements"]
    plan["source_review_coverage"]={key:source_requirements.get(key) for key in ("coverage_basis","reconciliation","unresolved","summary")}
    write_json(directory/"requirements.json",requirements)
    return refresh_mentor(plan,learner)


def preserve_published_history(plan, state, dispositions=None, projected_keys=None):
    """Keep published IDs and version history visible without recreating rows."""
    current={r["source_key"] for r in plan["records"]} | set(projected_keys or ())
    previous=state.get("records",{})
    inverse={v["page_id"]:k for k,v in previous.items() if v.get("page_id")}
    rules=(dispositions or {}).get("records",{})
    for record in plan["records"]:
        rule=rules.get(record["source_key"],{})
        if rule.get("scope"):
            record["properties"]["Scope"]=rule["scope"]
        if record["kind"]=="resources":
            record["properties"]["Current"]="__YES__"
            record["properties"].setdefault("Scope","Academic")
    for key, old in previous.items():
        if key in current or old.get("kind") in MENTOR_SCHEMAS:
            continue
        kind=old.get("kind")
        if kind not in plan["databases"]:
            continue
        props=deepcopy(old.get("properties",{}))
        for name,spec in plan["databases"][kind]["properties"].items():
            if spec.get("type")=="relation" and isinstance(props.get(name),list):
                props[name]=[inverse.get(value,value) for value in props[name]]
        rule=rules.get(key,{})
        if kind=="tasks":
            props["Scope"]=rule.get("scope","Historical" if props.get("Preparation Status") in ("Cancelled","Merged","Past","Deferred") else "Needs confirmation")
        elif kind in ("resources","announcements"):
            props["Scope"]=rule.get("scope","Historical")
        if kind=="resources":
            props["Current"]="__NO__"
        props["Source Key"]=key
        record={"kind":kind,"source_key":key,"properties":props,"generated_content":old.get("generated_content","")}
        if old.get("content_mode") == "remote_append":
            record["content_mode"] = "remote_append"
            record["generated_content"] = ""
        refs=rule.get("source_refs",[])
        if refs:
            record["source_refs"]=refs
            props["Source"]="\n".join(dict.fromkeys(str(props.get("Source","")).splitlines()+refs))
        plan["records"].append(record)


def apply_preparations(plan, payload):
    """Publish reviewed class preparation separately from official due dates."""
    indexed={r["source_key"]:r for r in plan["records"]}
    requirements=[]
    for item in payload.get("preparations",[]):
        if not item.get("id") or not item.get("source_refs") or not item.get("course_key"):
            raise ValueError("Class preparation requires stable id, course_key and source_refs")
        course=item["course_key"]
        if course not in indexed or indexed[course]["kind"]!="courses":
            raise ValueError("Preparation course is not in this instance: "+course)
        key=item.get("canonical_task_key") or course+"|preparation="+str(item["id"])
        record=indexed.get(key)
        if item.get("canonical_task_key") and (not record or record["kind"]!="tasks"):
            raise ValueError("Preparation canonical task not found: "+key)
        if not record:
            source_url=item.get("source_url")
            for ref in item["source_refs"]:
                candidate=indexed.get(ref,{}).get("properties",{}).get("Source URL",ref)
                if not source_url and str(candidate).startswith("https://") and "|" not in candidate:
                    source_url=candidate
            if not source_url:
                raise ValueError("Preparation needs a resolvable source_url: "+key)
            record={"kind":"tasks","source_key":key,"course_key":course,
                    "properties":{"Name":item["title"],"Source Key":key,"Course":[course],
                                  "Term":plan["term"]["key"],"Type":"Class preparation","Scope":"Academic",
                                  "Source":"\n".join(item["source_refs"]),"Source URL":source_url,"Sync Status":"reviewed"},
                    "generated_content":{"reviewed_action":item["title"],"source_ref":"; ".join(item["source_refs"])}}
            plan["records"].append(record);indexed[key]=record
        if item.get("origin"):
            record["properties"]["Planning Origin"]=item["origin"]
        if item.get("activity_type"):
            record["properties"]["Learning Activity"]=item["activity_type"]
        if item.get("rule_managed"):
            record["preparation"]=deepcopy(item)
            record["properties"]["Preparation Status"]=item.get("preparation_status","Active")
            record["properties"]["Class / Assessment"]=item.get("trigger_id","")
            before=item.get("prepare_before")
            record["properties"].update({"date:Prepare Before:start":before,"date:Prepare Before:is_datetime":int(bool(before and "T" in before))})
            if not item.get("canonical_task_key"):
                record["properties"]["Name"]=item["title"]
        record["source_refs"]=list(dict.fromkeys(record.get("source_refs",[])+item["source_refs"]))
        record["properties"]["Source"]="\n".join(dict.fromkeys(str(record["properties"].get("Source","")).splitlines()+item["source_refs"]))
        if item.get("study_date"):
            from datetime import date
            date.fromisoformat(item["study_date"])
            record["properties"].update({"date:Study Date:start":item["study_date"],"date:Study Date:is_datetime":0})
        if item.get("resource_keys"):
            record["properties"]["Resources"]=list(dict.fromkeys(record["properties"].get("Resources",[])+item["resource_keys"]))
        if item.get("study_steps"):
            record["study_steps"]=deepcopy(item["study_steps"])
            generated=record.get("generated_content")
            if isinstance(generated,dict):
                generated["study_steps"]=deepcopy(item["study_steps"])
        if item.get("study_mode"):
            record["study_mode"]=item["study_mode"]
        if item.get("rule_managed"):
            if not isinstance(record.get("generated_content"),dict):
                record["generated_content"]={"preserved_source_content":record.get("generated_content","")}
            record["generated_content"].update({"planning_origin":item.get("origin"), "learning_activity":item.get("activity_type"),
                "class_or_assessment":item.get("trigger_id"),"prepare_before":item.get("prepare_before"),
                "preparation_status":item.get("preparation_status"),"merged_into":item.get("merged_into"),
                "carried_from":item.get("carried_from",[]),"linked_task_keys":item.get("linked_task_keys",[])})
        if item.get("origin")=="Assistant suggestion":
            continue
        requirements.append({"id":"preparation:"+course+":"+item["id"],"source_refs":item["source_refs"],
                             "task_keys":[key],"status":"optional" if item.get("origin")=="Teacher recommendation" else "required","reason":"Source-backed class preparation; Study Date is separate from official Due."})
    return requirements

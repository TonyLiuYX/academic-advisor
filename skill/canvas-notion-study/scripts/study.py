#!/usr/bin/env python3
"""Local entry point. Notion writes run through the host's connected tools."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

from study_sync.credentials import CredentialError, load_token, prompt_store, service_name
from study_sync.state import read_json, write_json, empty_state, prepare_operations, begin_operations, commit_receipts, content_edit


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def load_config(path):
    config = read_json(path)
    service_name(config["base_url"])
    for key in ("state_dir", "archive_dir"):
        if not Path(config[key]).is_absolute():
            config[key] = str((Path(path).resolve().parent / config[key]).resolve())
    return config


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config",required=True,help="Non-secret instance JSON configuration")
    sub=p.add_subparsers(dest="command",required=True)
    init=sub.add_parser("init",help="Create a non-secret instance configuration")
    init.add_argument("--base-url",required=True)
    init.add_argument("--term-label",required=True)
    init.add_argument("--term-key",required=True)
    init.add_argument("--start",required=True)
    init.add_argument("--end",required=True)
    init.add_argument("--timezone",default="America/Toronto")
    init.add_argument("--archive-dir",required=True)
    init.add_argument("--state-dir",required=True)
    sub.add_parser("auth-store",help="Save an existing token in macOS Keychain using a hidden prompt")
    sub.add_parser("probe",help="Verify identity and readable course list using GET requests")
    sub.add_parser("sync",help="Read Canvas and archive accessible source materials")
    sub.add_parser("status",help="Show saved coverage and Notion receipt counts")
    sub.add_parser("export",help="Build course folders, local file indexes, and an explicit-deadline ICS calendar")
    sub.add_parser("views",help="Print linked-view definitions and any saved view IDs")
    register=sub.add_parser("register",help="Register this non-secret instance for future skill invocations")
    register.add_argument("--registry")
    review=sub.add_parser("review-packet",help="Export evidence for host AI summaries")
    review.add_argument("--course-id")
    plan=sub.add_parser("plan",help="Build deterministic Notion records from the current snapshot")
    plan.add_argument("--enrichments")
    plan.add_argument("--out")
    plan.add_argument("--week",help="Target week for class preparation; defaults to the current week")
    plan.add_argument("--now",help="ISO time for reproducible preparation planning")
    schema=sub.add_parser("schema",help="Print a database definition using the current plan and bindings")
    schema.add_argument("--kind",required=True)
    schema.add_argument("--fetched-schema",help="JSON data-source state from Notion fetch; merge new enum options")
    nxt=sub.add_parser("notion-next",help="Prepare tool operations; --begin persists the outbox before applying")
    nxt.add_argument("--kind")
    nxt.add_argument("--limit",type=int,default=20)
    nxt.add_argument("--begin",action="store_true")
    nxt.add_argument("--out")
    receipt=sub.add_parser("notion-receipts",help="Commit explicit verified remote outcomes")
    receipt.add_argument("file")
    bind=sub.add_parser("bind",help="Save Notion root/database IDs discovered through fetch")
    bind.add_argument("--root-page-id")
    bind.add_argument("--kind")
    bind.add_argument("--database-id")
    bind.add_argument("--data-source-id")
    bind.add_argument("--view-key")
    bind.add_argument("--view-id")
    edit=sub.add_parser("content-edit",help="Prepare a targeted managed-content update from a fetched page")
    edit.add_argument("--source-key",required=True)
    edit.add_argument("--fetched-content",required=True)
    migrate=sub.add_parser("migrate-state",help="Preview or apply state migration with backups")
    migrate.add_argument("--apply",action="store_true")
    personal=sub.add_parser("import-personal",help="Import faithful Notion personal fields without changing source data")
    personal.add_argument("file")
    week=sub.add_parser("week-plan",help="Save a complete week draft or plan using explicit available time")
    week.add_argument("--week",help="ISO date in the target week")
    week.add_argument("--availability",help="JSON with explicit minutes or available slots")
    week.add_argument("--now",help="ISO time for reproducible planning")
    render=sub.add_parser("week-render",help="Reformat saved weeks from local data without rescheduling or fetching sources")
    render.add_argument("--week",help="ISO date in one saved week; omit to reformat all saved weeks")
    start=sub.add_parser("study-start",help="Recommend the next source-backed study task and save a session")
    start.add_argument("--minutes",type=int,required=True)
    start.add_argument("--task",help="Optional preferred task Source Key")
    start.add_argument("--now")
    finish=sub.add_parser("study-finish",help="Save explicit session feedback and remaining work")
    finish.add_argument("--session",required=True)
    finish.add_argument("--feedback",required=True,help="JSON feedback file")
    finish.add_argument("--now")
    context=sub.add_parser("context",help="Build a bounded, durable context packet for a new conversation")
    context.add_argument("--week")
    audit=sub.add_parser("audit",help="Reconcile required work, Notion records and local files")
    audit.add_argument("--notion-rows")
    audit.add_argument("--baseline")
    audit.add_argument("--out")
    gmail=sub.add_parser("gmail-queries",help="Print targeted, overlapping Gmail connector queries")
    gmail.add_argument("--now")
    ingest=sub.add_parser("gmail-import",help="Import connector full/metadata messages and search coverage")
    ingest.add_argument("path",help="JSON file or directory of connector message JSON files")
    ingest.add_argument("--manifest",help="JSON search pages, query IDs and completeness")
    sub.add_parser("gmail-review-packet",help="Print source-hashed Gmail review evidence")
    task=sub.add_parser("task",help="Retrieve one task with evidence, materials and personal progress")
    task.add_argument("--key",required=True)
    notes=sub.add_parser("notes-import",help="Import a faithfully transcribed photo/PDF batch; no remote writes")
    notes.add_argument("file",help="JSON source manifest and source-attributed Notion Markdown")
    notes.add_argument("--now",help="Timezone-aware ISO timestamp for resolving today")
    nn=sub.add_parser("notes-next",help="Prepare the next notebook/evidence/attachment/entry operation")
    nn.add_argument("--batch",help="Batch ID from notes-import")
    nn.add_argument("--readbacks",help="JSON main/evidence page readbacks from actual Notion fetch")
    nn.add_argument("--begin",action="store_true",help="Persist the operation before calling Notion")
    nn.add_argument("--out")
    nr=sub.add_parser("notes-receipts",help="Commit verified notebook and attachment outcomes")
    nr.add_argument("file")
    nf=sub.add_parser("notes-find",help="Locate a local Obsidian course notebook and read relevant units")
    nf.add_argument("--course")
    nf.add_argument("--note-id")
    nf.add_argument("--term")
    nf.add_argument("--topic")
    nf.add_argument("--read",action="store_true")
    ni=sub.add_parser("notes-index",help="Queue or verify Notion index metadata after a local notebook commit")
    ni.add_argument("--course-key",required=True)
    ni.add_argument("--note-id",required=True)
    ni.add_argument("--readback",help="Actual Notion properties and page_id JSON")
    return p


def run(args):
    if args.command=="init":
        from datetime import date
        from zoneinfo import ZoneInfo
        service_name(args.base_url)
        ZoneInfo(args.timezone)
        if date.fromisoformat(args.start)>=date.fromisoformat(args.end):
            raise ValueError("The collection window must end after it starts.")
        if Path(args.config).exists():
            raise ValueError("Configuration already exists; preserve existing bindings and settings.")
        config={"schema_version":1,"base_url":args.base_url.rstrip('/'),"keychain_account":"canvas-student",
                "term":{"key":args.term_key,"label":args.term_label,"start":args.start,"end":args.end,"timezone":args.timezone},
                "archive_dir":str(Path(args.archive_dir).resolve()),"state_dir":str(Path(args.state_dir).resolve()),"notion":{"databases":{}}}
        write_json(args.config,config)
        emit({"config":str(Path(args.config).resolve()),"next":"auth-store"})
        return 0
    config=load_config(args.config)
    directory=Path(config["state_dir"])
    snapshot_path=directory/"snapshot.json"
    plan_path=directory/"notion-plan.json"
    state_path=directory/"notion-state.json"
    learner_path=directory/"learner.json"
    if args.command=="auth-store":
        prompt_store(config["base_url"],config.get("keychain_account","canvas-student"))
    elif args.command in ("probe","sync"):
        from study_sync.canvas import CanvasClient, collect_snapshot
        client=CanvasClient(config["base_url"],load_token(config["base_url"],config.get("keychain_account","canvas-student")))
        if args.command=="probe":
            profile=client.get("/api/v1/users/self/profile")
            if config.get("owner_id") and profile["id"]!=config["owner_id"]:
                raise ValueError("Canvas identity differs from this instance; use a separate configuration.")
            courses=client.list("/api/v1/courses",{"include[]":["term"],"enrollment_state":"active"})
            config["owner_id"]=profile["id"]
            write_json(args.config,config)
            emit({"status":"ok","user_id":profile["id"],"name":profile.get("name"),"courses":[{"id":c["id"],"name":c.get("name"),"term":c.get("term",{}).get("name")} for c in courses]})
        else:
            previous=read_json(snapshot_path,{})
            snapshot=collect_snapshot(client,config,Path(config["archive_dir"]),previous=previous or None,
                                      progress=lambda event: print(json.dumps(event,ensure_ascii=False),file=sys.stderr,flush=True))
            if config.get("owner_id") and snapshot["user"]["id"]!=config["owner_id"]:
                raise ValueError("Canvas identity differs from this instance; use a separate configuration.")
            write_json(snapshot_path,snapshot)
            stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            write_json(directory/"snapshots"/(stamp+".json"),snapshot)
            emit({"snapshot":str(snapshot_path),"stats":snapshot.get("stats"),"warning_details":"See snapshot warnings.","courses":[{"id":c["id"],"name":c["name"],"mode":c.get("mode"),"assignments":len(c.get("assignments",[])),"announcements":len(c.get("announcements",[])),"files":len(c.get("files",[])),"warning_count":len(c.get("warnings",[]))} for c in snapshot["courses"]]})
    elif args.command=="review-packet":
        from study_sync.review import evidence_packet
        emit(evidence_packet(read_json(snapshot_path),args.course_id))
    elif args.command=="plan":
        from study_sync.v1 import build_v1_plan
        from study_sync.review import validate_enrichments
        snapshot=read_json(snapshot_path)
        enrich=read_json(args.enrichments) if args.enrichments else read_json(directory/"enrichments.json",{})
        enrich,review_warnings=validate_enrichments(snapshot,enrich)
        plan=build_v1_plan(snapshot,enrich,config,read_json(learner_path,{}),directory,week=args.week,now=args.now)
        plan["review_warnings"]=review_warnings
        write_json(args.out or plan_path,plan)
        emit({"plan":str(args.out or plan_path),"records":len(plan.get("records",[])),"stats":plan.get("stats"),"warnings":plan.get("warnings",[]),"review_warnings":review_warnings})
    elif args.command=="schema":
        from study_sync.notion_schema import database_definition,schema_updates
        if args.fetched_schema:
            emit(schema_updates(read_json(plan_path),args.kind,read_json(args.fetched_schema),config.get("notion",{}).get("databases",{})))
        else:
            emit(database_definition(read_json(plan_path),args.kind,config.get("notion",{}).get("databases",{})))
    elif args.command=="export":
        from study_sync.exporting import export_workspace
        emit(export_workspace(read_json(snapshot_path),read_json(plan_path),config["archive_dir"]))
    elif args.command=="views":
        from study_sync.views import view_definitions
        emit({'views':view_definitions(read_json(plan_path),read_json(state_path,empty_state()),config)})
    elif args.command=="register":
        from study_sync.registry import register_instance
        emit(register_instance(args.config,config,args.registry))
    elif args.command=="notes-find":
        from study_sync.obsidian_notes import find
        emit(find(config,read_json(state_path,empty_state()),course=args.course,note_id=args.note_id,
                  term=args.term,topic=args.topic,read=args.read))
    elif args.command=="notes-index":
        from study_sync.obsidian_notes import index_notebook
        from study_sync.notes import project_notes,link_preparation_notes
        state=read_json(state_path,empty_state())
        plan=read_json(plan_path)
        result=index_notebook(config,state,plan,args.course_key,args.note_id,
                              read_json(args.readback) if args.readback else None)
        write_json(state_path,state)
        write_json(plan_path,link_preparation_notes(project_notes(plan,state)))
        emit(result)
    elif args.command in ("notes-import", "notes-next", "notes-receipts"):
        from study_sync.notes import import_batch, next_operation, commit_note_receipts, cleanup_cache, project_notes, link_preparation_notes
        state=read_json(state_path,empty_state())
        cleanup=[]
        if args.command=="notes-import":
            result=import_batch(state,read_json(plan_path),read_json(args.file),directory,config,
                                schedule=read_json(directory/"course-schedule.json",{}),now=args.now)
        elif args.command=="notes-next":
            result=next_operation(state,config,directory,batch_id=args.batch,
                                  readbacks=read_json(args.readbacks) if args.readbacks else None,begin=args.begin)
        else:
            raw=read_json(args.file)
            cleanup=commit_note_receipts(state,raw.get("receipts",raw) if isinstance(raw,dict) else raw)
            result={"verified_batches":cleanup,"inflight":len(state.get("inflight",{}))}
        if args.command!="notes-next" or args.begin:
            write_json(state_path,state)
            # State commits before cleanup. Rebuilding this derived plan is safe
            # after interruption; no Canvas fetch or learner mutation is needed.
            from study_sync.v1 import refresh_mentor
            plan=link_preparation_notes(project_notes(read_json(plan_path),state))
            write_json(plan_path,refresh_mentor(plan,read_json(learner_path,{})))
        # Also recover cleanup interrupted after a successful state commit.
        cleanup_cache(directory,[b["id"] for b in state.get("class_notes",{}).get("batches",{}).values()
                                 if b.get("status")=="verified"])
        if getattr(args,"out",None):
            write_json(args.out,result)
            emit({"out":args.out,"operations":len(result.get("operations",[])),"blocked":len(result.get("blocked",[]))})
        else:
            emit(result)
    elif args.command=="notion-next":
        state=read_json(state_path,empty_state())
        plan=read_json(plan_path)
        if plan.get("review_warnings") or plan.get("gmail_review_warnings"):
            raise ValueError("Source reviews are stale or incomplete. Re-read the evidence packet and rebuild the plan before applying Notion changes.")
        batch=prepare_operations(plan,state,config.get("notion",{}).get("databases",{}),args.kind,args.limit)
        if args.begin:
            write_json(state_path,begin_operations(state,batch["operations"]))
        if args.out:
            write_json(args.out,batch)
            emit({"out":args.out,"operations":len(batch["operations"]),"blocked":len(batch["blocked"]),"unchanged":batch["unchanged"]})
        else:
            emit(batch)
    elif args.command=="notion-receipts":
        receipts=read_json(args.file)
        state=commit_receipts(read_json(state_path,empty_state()),receipts.get("receipts",receipts) if isinstance(receipts,dict) else receipts)
        write_json(state_path,state)
        emit({"records":len(state["records"]),"inflight":len(state["inflight"])})
    elif args.command=="bind":
        notion=config.setdefault("notion",{})
        if args.root_page_id:
            notion["root_page_id"]=args.root_page_id
        if args.kind:
            if not args.database_id or not args.data_source_id:
                raise ValueError("Binding a database requires both IDs from Notion fetch.")
            notion.setdefault("databases",{})[args.kind]={"database_id":args.database_id,"data_source_id":args.data_source_id}
        if args.view_key:
            if not args.view_id:
                raise ValueError("A view binding requires the verified view ID.")
            notion.setdefault("views",{})[args.view_key]=args.view_id
        write_json(args.config,config)
        emit({"bound":args.kind or "root"})
    elif args.command=="content-edit":
        state=read_json(state_path)
        op=state["inflight"][args.source_key]
        if op.get("baseline_region") is None:
            raise ValueError("No verified content baseline exists. Fetch and reconcile this page before editing.")
        fetched=Path(args.fetched_content).read_text()
        edit=content_edit(fetched,op["generated_content"],op.get("baseline_region"))
        emit({"page_id":op["page_id"],"command":"update_content","content_updates":[edit]})
    elif args.command=="migrate-state":
        from study_sync.migrations import migrate_state_dir
        emit(migrate_state_dir(directory,dry_run=not args.apply))
    elif args.command=="gmail-queries":
        from study_sync.gmail import build_queries
        emit(build_queries(config,read_json(directory/"gmail-snapshot.json",{}),now=args.now))
    elif args.command=="gmail-import":
        from study_sync.gmail import ingest_messages
        location=Path(args.path)
        payloads=[read_json(p) for p in sorted(location.glob("*.json"))] if location.is_dir() else read_json(location)
        if not isinstance(payloads,list):
            payloads=payloads.get("messages",[payloads])
        result=ingest_messages(read_json(directory/"gmail-snapshot.json",{}),payloads,
                               search_manifest=read_json(args.manifest) if args.manifest else None)
        write_json(directory/"gmail-snapshot.json",result)
        emit({"messages":len(result.get("messages",[])),"stats":result.get("stats"),"pending_full":result.get("fetch_queue",[])})
    elif args.command=="gmail-review-packet":
        from study_sync.gmail import review_packet
        emit(review_packet(read_json(directory/"gmail-snapshot.json")))
    elif args.command=="task":
        plan=read_json(plan_path)
        records={r["source_key"]:r for r in plan["records"]}
        record=records.get(args.key)
        if not record or record["kind"]!="tasks":
            raise ValueError("Task Source Key not found in current plan")
        learner=read_json(learner_path,{})
        materials=[records[key] for key in record.get("properties",{}).get("Resources",[]) if key in records]
        materials.extend(record.get("note_materials",[]))
        emit({"task":record,"materials":materials,"personal":learner.get("personal",{}).get(args.key,{}),
              "progress":learner.get("task_progress",{}).get(args.key,{})})
    elif args.command=="import-personal":
        from study_sync.learner import import_personal
        learner,report=import_personal(read_json(learner_path,{}),read_json(args.file),read_json(state_path,empty_state()))
        learner.setdefault("profile",{}).setdefault("timezone",config["term"]["timezone"])
        write_json(learner_path,learner)
        write_json(directory/"personal-import-report.json",report)
        emit(report)
    elif args.command=="week-render":
        from copy import deepcopy
        from datetime import date, timedelta
        from study_sync.week_layout import attach_layout
        from study_sync.planning import _week_record
        from study_sync.state import record_fingerprint
        learner=read_json(learner_path,{})
        plan=read_json(plan_path)
        classes=None
        if (directory/"course-schedule.json").exists():
            from study_sync.schedule import occurrences
            classes,_=occurrences(read_json(directory/"course-schedule.json"),config["term"]["timezone"])
        selected=None
        if args.week:
            day=date.fromisoformat(args.week)
            selected=(day-timedelta(days=day.weekday())).isoformat()
            if selected not in learner.get("weeks",{}):
                raise ValueError("No saved plan for this week; week-render does not create plans")
        replacements={}
        for key,week in learner.get("weeks",{}).items():
            if selected and key!=selected:
                continue
            updated=attach_layout(week,plan,classes)
            record=_week_record(updated)
            record["fingerprint"]=record_fingerprint(record)
            replacements[record["source_key"]]=record
            learner["weeks"][key]=updated
            write_json(directory/"weeks"/(key+".json"),dict(deepcopy(updated),records=[record]))
        plan["records"]=[replacements.get(r["source_key"],r) for r in plan["records"]]
        write_json(learner_path,learner)
        write_json(plan_path,plan)
        emit({"reformatted_weeks":list(replacements),"sources_fetched":False,"tasks_rescheduled":False})
    elif args.command in ("week-plan","study-start","study-finish"):
        from study_sync.planning import build_week_plan,study_start,study_finish
        from study_sync.learner import normalise_learner
        from study_sync.v1 import refresh_mentor
        learner=normalise_learner(read_json(learner_path,{}))
        learner["profile"].setdefault("timezone",config["term"]["timezone"])
        plan=read_json(plan_path)
        if args.command=="study-start" and (directory/"course-schedule.json").exists():
            from study_sync.v1 import build_v1_plan
            from study_sync.review import validate_enrichments
            snapshot=read_json(snapshot_path)
            enrich,warnings=validate_enrichments(snapshot,read_json(directory/"enrichments.json",{}))
            plan=build_v1_plan(snapshot,enrich,config,learner,directory,now=args.now)
            plan["review_warnings"]=warnings
        if args.command=="week-plan":
            if (directory/"course-schedule.json").exists():
                from study_sync.v1 import build_v1_plan
                from study_sync.review import validate_enrichments
                snapshot=read_json(snapshot_path)
                enrich,warnings=validate_enrichments(snapshot,read_json(directory/"enrichments.json",{}))
                plan=build_v1_plan(snapshot,enrich,config,learner,directory,week=args.week,now=args.now)
                plan["review_warnings"]=warnings
            availability=read_json(args.availability) if args.availability else None
            result=build_week_plan(plan,learner,week=args.week,availability=availability,now=args.now)
            learner["weeks"][result["week_start"]]={k:v for k,v in result.items() if k!="records"}
            write_json(directory/"weeks"/(result["week_start"]+".json"),result)
        elif args.command=="study-start":
            learner,result=study_start(plan,learner,args.minutes,preferred_task=args.task,now=args.now)
        else:
            learner,result=study_finish(learner,args.session,read_json(args.feedback),now=args.now)
        write_json(learner_path,learner)
        write_json(plan_path,refresh_mentor(plan,learner))
        emit(result)
    elif args.command=="context":
        from study_sync.context import build_context
        from study_sync.notes import project_notes, link_preparation_notes
        learner=read_json(learner_path,{})
        week=learner.get("weeks",{}).get(args.week) if args.week else None
        plan=link_preparation_notes(project_notes(read_json(plan_path),read_json(state_path,empty_state())))
        packet=build_context(plan,learner,week_plan=week,snapshot=read_json(snapshot_path,{}),state_dir=str(directory))
        write_json(directory/"context.json",packet)
        emit(packet)
    elif args.command=="audit":
        from study_sync.audit import audit_plan,audit_files
        result={"plan":audit_plan(read_json(plan_path),requirements=read_json(directory/"requirements.json",{}),
                                  notion_rows=read_json(args.notion_rows) if args.notion_rows else None,
                                  notion_state=read_json(state_path,empty_state())),
                "files":audit_files(read_json(snapshot_path),baseline=read_json(args.baseline) if args.baseline else None)}
        write_json(args.out or directory/"audit.json",result)
        emit(result)
    elif args.command=="status":
        snapshot=read_json(snapshot_path,{})
        state=read_json(state_path,empty_state())
        emit({"last_canvas_snapshot":snapshot.get("generated_at"),"stats":snapshot.get("stats"),"notion_records":len(state["records"]),"inflight":list(state["inflight"]),"root_page_id":config.get("notion",{}).get("root_page_id")})
    return 0


def main(argv=None):
    args=parser().parse_args(argv)
    if args.command in ("notes-import", "notes-next", "notes-receipts", "notion-next", "notion-receipts"):
        from study_sync.notes import state_lock
        with state_lock(load_config(args.config)["state_dir"]):
            return run(args)
    return run(args)


if __name__=="__main__":
    try:
        raise SystemExit(main())
    except (CredentialError,ValueError,FileNotFoundError,KeyError) as error:
        print(json.dumps({"error":str(error),"type":type(error).__name__},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(2)

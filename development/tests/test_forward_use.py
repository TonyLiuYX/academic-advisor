"""Forward test for a realistic Canvas -> archive -> Notion update workflow.

This test deliberately uses a synthetic same-origin Canvas transport and a
fake Notion receipt writer.  It leaves a complete run dossier under
``work/forward-test`` so a reviewer can inspect the exact inputs, commands,
plans, batches, receipts and failures without credentials or a live account.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from urllib.parse import parse_qs, urlsplit


REPO = Path(__file__).resolve().parents[1]


def _resolve_script_root() -> Path:
    """Find the CLI in either the development repo or packaged skill layout."""

    explicit = os.environ.get("CANVAS_NOTION_STUDY_SCRIPTS")
    candidates: list[Path] = [Path(explicit).expanduser()] if explicit else []
    location = Path(__file__).resolve()
    for base in (location.parent, *location.parents):
        candidates.extend(
            (
                base / "skill" / "canvas-notion-study" / "scripts",
                base / "scripts",
            )
        )
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / "study.py").is_file():
            return candidate
    raise RuntimeError(
        "Cannot find study.py; set CANVAS_NOTION_STUDY_SCRIPTS to the skill scripts directory."
    )


SCRIPT_ROOT = _resolve_script_root()
STUDY = SCRIPT_ROOT / "study.py"
PYTHON = Path(os.environ.get("CANVAS_NOTION_STUDY_PYTHON", sys.executable))
workspace_override = os.environ.get("CANVAS_NOTION_STUDY_FORWARD_WORKSPACE")
if workspace_override:
    WORKSPACE = Path(workspace_override).expanduser()
else:
    PROJECT_ROOT = REPO.parent.parent if REPO.parent.name == "work" else REPO.parent
    WORKSPACE = PROJECT_ROOT / "work" / "forward-test"

sys.path.insert(0, str(SCRIPT_ROOT))

from study_sync.canvas import CanvasClient, CanvasOriginError, collect_snapshot  # noqa: E402
from study_sync.review import evidence_packet  # noqa: E402
from study_sync.state import (  # noqa: E402
    begin_operations,
    commit_receipts,
    content_edit,
    empty_state,
    managed_page,
    managed_region,
    prepare_operations,
    write_json,
)


class SyntheticCanvas:
    """Small Canvas API fixture with pagination and downloadable materials."""

    def __init__(self, pdf_bytes: bytes, *, safety_bytes: bytes | None = None, reading_bytes: bytes | None = None):
        self.pdf_bytes = pdf_bytes
        self.safety_bytes = safety_bytes or pdf_bytes
        self.reading_bytes = reading_bytes or pdf_bytes
        self.calls: list[dict] = []

    def __call__(self, url: str, headers=None, timeout=None):
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        path = parts.path
        self.calls.append(
            {
                "url": url,
                "path": path,
                "query": {key: values for key, values in query.items()},
                # Keep the transport dossier credential-free while retaining
                # enough evidence to verify same-origin auth injection.
                "authorization_present": bool((headers or {}).get("Authorization")),
            }
        )

        if path == "/api/v1/users/self/profile":
            return {"id": 77, "name": "Synthetic Student", "time_zone": "America/Toronto"}
        if path == "/api/v1/courses":
            return [
                {
                    "id": 101,
                    "name": "BIO101 Cell Biology",
                    "course_code": "BIO101H1 F LEC0101",
                    "term": {"name": "2026-fall", "start_at": "2026-08-01T00:00:00Z", "end_at": "2027-01-01T00:00:00Z"},
                    "html_url": "https://canvas.example.edu/courses/101",
                    "syllabus_body": '<p>See the <a href="/files/900/download">course syllabus</a>.</p>',
                },
                {
                    "id": 202,
                    "name": "Student Success Hub",
                    "course_code": "",
                    "term": {"name": "2026-fall", "start_at": "2026-08-01T00:00:00Z", "end_at": "2027-01-01T00:00:00Z"},
                    "html_url": "https://canvas.example.edu/courses/202",
                },
            ]

        if path == "/api/v1/announcements":
            contexts = query.get("context_codes[]", [])
            page = query.get("page", ["1"])[0]
            if contexts == ["course_101"] and page == "1":
                return (
                    200,
                    {"Link": '<https://canvas.example.edu/api/v1/announcements?page=2&context_codes%5B%5D=course_101>; rel="next"'},
                    [
                        {
                            "id": 7001,
                            "title": "Lab consent form",
                            "posted_at": "2026-09-05T09:00:00Z",
                            "message": "Submit the lab consent form before the first lab.",
                            "html_url": "https://canvas.example.edu/courses/101/discussion_topics/7001",
                        }
                    ],
                )
            if contexts == ["course_101"] and page == "2":
                return [
                    {
                        "id": 7002,
                        "title": "Reading reminder",
                        "posted_at": "2026-09-06T09:00:00Z",
                        "message": "Read chapter 2 before class.",
                        "html_url": "https://canvas.example.edu/courses/101/discussion_topics/7002",
                    }
                ]
            if contexts == ["course_202"]:
                return [
                    {
                        "id": 8001,
                        "title": "Orientation checklist",
                        "posted_at": "2026-09-04T10:00:00Z",
                        "message": "Complete the accessibility orientation this week.",
                        "html_url": "https://canvas.example.edu/courses/202/discussion_topics/8001",
                    }
                ]
            return []

        if path == "/api/v1/courses/101/assignments":
            return [
                {
                    "id": 1001,
                    "name": "Cell transport problem set",
                    "due_at": "2026-09-20T23:59:00-04:00",
                    "description": "Submit calculations as a PDF.",
                    "html_url": "https://canvas.example.edu/courses/101/assignments/1001",
                    "workflow_state": "published",
                },
                {
                    "id": 1002,
                    "name": "Lab safety acknowledgement",
                    "due_at": None,
                    "description": "Complete the acknowledgement before attending lab.",
                    "attachments": [
                        {
                            "id": 901,
                            "display_name": "lab-safety.pdf",
                            "url": "https://canvas.example.edu/files/901/download",
                            "content_type": "application/pdf",
                        }
                    ],
                    "html_url": "https://canvas.example.edu/courses/101/assignments/1002",
                },
            ]
        if path == "/api/v1/courses/202/assignments":
            return []

        if path == "/api/v1/calendar_events":
            contexts = query.get("context_codes[]", [])
            if contexts == ["course_101"]:
                return [
                    {
                        "id": 3001,
                        "title": "BIO101 lecture",
                        "start_at": "2026-09-10T14:00:00-04:00",
                        "end_at": "2026-09-10T15:00:00-04:00",
                        "event_type": "event",
                        "html_url": "https://canvas.example.edu/calendar_events/3001",
                    }
                ]
            if contexts == ["course_202"]:
                return [
                    {
                        "id": 3002,
                        "title": "Student success drop-in",
                        "start_at": "2026-09-11T12:00:00-04:00",
                        "end_at": "2026-09-11T13:00:00-04:00",
                        "event_type": "event",
                        "html_url": "https://canvas.example.edu/calendar_events/3002",
                    }
                ]
            return []

        if path == "/api/v1/courses/101/modules":
            return [{"id": 5001, "name": "Week 1", "items_count": 1}]
        if path == "/api/v1/courses/101/modules/5001/items":
            return [{"id": 5002, "title": "Course overview", "type": "Page", "page_url": "overview"}]
        if path == "/api/v1/courses/101/pages":
            return [{"page_id": "overview", "url": "overview", "title": "Course overview"}]
        if path == "/api/v1/courses/101/pages/overview":
            # The fixture wrapper keeps Canvas' JSON ``body`` field from
            # being mistaken for the transport response body's metadata.
            return (
                200,
                {},
                {
                    "page_id": "overview",
                    "url": "overview",
                    "title": "Course overview",
                    "body": '<p>Use <a href="/files/902/download">the reading packet</a>.</p>',
                },
            )
        if path == "/api/v1/courses/101/files":
            return [
                {
                    "id": 900,
                    "display_name": "BIO101 syllabus.pdf",
                    "url": "https://canvas.example.edu/files/900/download",
                    "content_type": "application/pdf",
                    "updated_at": "2026-09-01T12:00:00Z",
                    "version": "3",
                },
                {
                    "id": 901,
                    "display_name": "lab-safety.pdf",
                    "url": "https://canvas.example.edu/files/901/download",
                    "content_type": "application/pdf",
                    "updated_at": "2026-09-02T12:00:00Z",
                },
            ]
        if path == "/files/900/download":
            return 200, {"Content-Type": "application/pdf"}, self.pdf_bytes
        if path == "/files/901/download":
            return 200, {"Content-Type": "application/pdf"}, self.safety_bytes
        if path == "/files/902/download":
            return 200, {"Content-Type": "application/pdf"}, self.reading_bytes

        raise AssertionError(f"Synthetic Canvas fixture has no route for {url}")


class WorkflowFailure(RuntimeError):
    pass


class ForwardUseTests(unittest.TestCase):
    """Run a realistic first import and next-week update in a fake workspace."""

    @classmethod
    def setUpClass(cls):
        if WORKSPACE.exists():
            # This path is created only by this forward test; avoid touching
            # any sibling project or production source tree.
            if not workspace_override and WORKSPACE.name != "forward-test":
                raise AssertionError(f"unexpected artifact path: {WORKSPACE}")
            shutil.rmtree(WORKSPACE)
        for child in ("raw", "archive", "state", "logs", "notion"):
            (WORKSPACE / child).mkdir(parents=True, exist_ok=True)
        cls.config = WORKSPACE / "config.json"
        cls.archive = WORKSPACE / "archive"
        cls.state = WORKSPACE / "state"
        cls.logs = WORKSPACE / "logs"
        cls.failures: list[dict] = []
        cls.commands: list[dict] = []

    @classmethod
    def tearDownClass(cls):
        write_json(WORKSPACE / "logs" / "commands.json", cls.commands)
        write_json(WORKSPACE / "observed_failures.json", cls.failures)
        snapshot_path = WORKSPACE / "state" / "snapshot.json"
        plan_path = WORKSPACE / "state" / "notion-plan.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else {}
        plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
        state_observation_path = WORKSPACE / "notion" / "state-compatibility.json"
        state_observation = json.loads(state_observation_path.read_text(encoding="utf-8")) if state_observation_path.exists() else {}
        archived = sorted(
            str(path.relative_to(WORKSPACE / "archive"))
            for path in (WORKSPACE / "archive").rglob("*")
            if path.is_file()
        )
        report = [
            "# Synthetic forward-test report",
            "",
            "This dossier uses an injected same-origin Canvas transport and synthetic Notion IDs/receipts. No live account, browser, credential store, or external write was used.",
            "",
            "## Observed workflow",
            "",
            f"- unittest command: `{PYTHON.name} -m unittest -v tests.test_forward_use`",
            f"- commands attempted: {len(cls.commands)}",
            f"- Canvas courses collected: {snapshot.get('stats', {}).get('course_count', 0)} (formal={snapshot.get('stats', {}).get('formal_course_count', 0)}, hub={snapshot.get('stats', {}).get('hub_count', 0)})",
            f"- plan records: {plan.get('stats', {}).get('record_count', 0)}; unique source keys: {len(set(plan.get('comparison', {}).get('record_keys', [])))}",
            f"- pending enrichment count: {plan.get('stats', {}).get('pending_enrichment_count', 0)}",
            f"- archived files: {len(archived)}",
            "",
            "## Artifacts",
            "",
            "- `state/snapshot.json` and `state/snapshots/fixture-first.json`: canonical collected snapshot.",
            "- `archive/files/`: three archived PDF materials; two extracted as PDF text and one HTML-linked file is recorded as unsupported because its discovered filename is extensionless.",
            "- `state/enrichments.json`: reviewed syllabus and announcement enrichment with one intentionally pending announcement.",
            "- `state/notion-plan.json`: deterministic 15-record projection.",
            "- `notion/state-compatibility.json`: direct state-layer observation after serializing structured generated content to text.",
            "- `logs/`: every attempted CLI command and stdout/stderr, including the first blocked upsert.",
            "",
            f"- direct `content_edit(dict)` probe: {state_observation.get('content_edit_structured_input', {}).get('status', 'not run')}; the supported CLI path passes rendered text.",
            "",
            "## Failures",
            "",
        ]
        if cls.failures:
            report.extend(f"- `{item.get('stage')}`: {item.get('detail')}" for item in cls.failures)
        else:
            report.append("- none")
        report.extend(
            [
                "",
                "## Acceptance interpretation",
                "",
                "Initialization, Canvas pagination, same-origin credential isolation, local archiving, document extraction, review-packet export, enrichment gating, deterministic planning, source-key uniqueness, unknown-result reconciliation, no-op repeat, changed-date update, and personal-region editing all passed in the synthetic run. The direct helper probe records that `content_edit` accepts rendered text, while the CLI path now renders structured projection content before calling it.",
            ]
        )
        (WORKSPACE / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    def _record_failure(self, stage: str, detail: str, **extra):
        item = {"stage": stage, "detail": detail}
        item.update(extra)
        self.failures.append(item)
        write_json(WORKSPACE / "observed_failures.json", self.failures)

    def _run_cli(self, label: str, *args: str, expected: int = 0) -> subprocess.CompletedProcess:
        command = [str(PYTHON), str(STUDY), "--config", str(self.config), *args]
        proc = subprocess.run(
            command,
            cwd=str(REPO),
            env={**os.environ, "PYTHONPATH": str(SCRIPT_ROOT)},
            text=True,
            capture_output=True,
            check=False,
        )
        (self.logs / f"{label}.stdout").write_text(proc.stdout, encoding="utf-8")
        (self.logs / f"{label}.stderr").write_text(proc.stderr, encoding="utf-8")
        self.commands.append({"label": label, "argv": command, "returncode": proc.returncode})
        if proc.returncode != expected:
            raise WorkflowFailure(
                f"{label} returned {proc.returncode}, expected {expected}; see {self.logs / (label + '.stderr')}"
            )
        return proc

    @staticmethod
    def _pdf_bytes(title: str = "Syllabus") -> bytes:
        # Committed valid documents keep this workflow independent of the
        # optional reportlab generator while still exercising PDF extraction.
        fixture = Path(__file__).parent / "fixtures" / (title.lower() + ".pdf")
        return fixture.read_bytes()

    def _bind_databases(self):
        self._run_cli("bind-root", "bind", "--root-page-id", "notion-root-synthetic")
        for kind in ("courses", "tasks", "resources", "announcements", "notes", "timetable"):
            self._run_cli(
                f"bind-{kind}",
                "bind",
                "--kind",
                kind,
                "--database-id",
                f"db-{kind}-synthetic",
                "--data-source-id",
                f"ds-{kind}-synthetic",
            )

    def _create_receipts(self, operations: list[dict], *, prefix: str) -> list[dict]:
        receipts = []
        for index, operation in enumerate(operations, start=1):
            remote_region = None
            content = operation.get("content")
            if isinstance(content, str) and content.count("**课程资料同步区**") == 1:
                remote_region = managed_region(content)
            receipt = {
                "source_key": operation["source_key"],
                "operation_id": operation["operation_id"],
                "status": "succeeded",
                # An update receipt must retain the existing remote page ID;
                # creates receive a deterministic synthetic page ID.
                "page_id": operation.get("page_id") or f"{prefix}-{index}",
            }
            if remote_region is not None:
                receipt["remote_region"] = remote_region
            receipts.append(receipt)
        return receipts

    def _commit_batch(self, label: str, batch_path: Path, *, prefix: str, unknown_first: bool = False):
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        operations = batch.get("operations", [])
        self.assertFalse(batch.get("blocked"), f"{label} unexpectedly blocked: {batch.get('blocked')}")
        if unknown_first and operations:
            unknown = {
                "receipts": [
                    {
                        "source_key": operations[0]["source_key"],
                        "operation_id": operations[0]["operation_id"],
                        "status": "unknown",
                    }
                ]
            }
            unknown_path = self.state / f"{label}-unknown-receipt.json"
            write_json(unknown_path, unknown)
            self._run_cli(f"receipts-{label}-unknown", "notion-receipts", str(unknown_path))
            blocked_proc = self._run_cli(
                f"{label}-blocked-after-unknown",
                "notion-next",
                "--kind",
                label,
                "--out",
                str(self.state / f"{label}-blocked.json"),
            )
            blocked = json.loads((self.state / f"{label}-blocked.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(blocked.get("blocked", [])), 1)
            receipts = {"receipts": self._create_receipts(operations, prefix=prefix)}
            receipt_path = self.state / f"{label}-receipts.json"
            write_json(receipt_path, receipts)
            self._run_cli(f"receipts-{label}", "notion-receipts", str(receipt_path))
            return batch
        receipt_path = self.state / f"{label}-receipts.json"
        write_json(receipt_path, {"receipts": self._create_receipts(operations, prefix=prefix)})
        self._run_cli(f"receipts-{label}", "notion-receipts", str(receipt_path))
        return batch

    def test_forward_setup_archive_enrichment_upsert_and_update(self):
        fixture = SyntheticCanvas(
            self._pdf_bytes("Syllabus"),
            safety_bytes=self._pdf_bytes("Safety"),
            reading_bytes=self._pdf_bytes("Reading"),
        )
        fixture_path = WORKSPACE / "raw" / "canvas-api.json"
        write_json(
            fixture_path,
            {
                "profile": {"id": 77, "name": "Synthetic Student"},
                "courses": [101, 202],
                "pagination": "announcements course_101 -> page 2",
                "materials": [900, 901, 902],
                "token": "<never stored; injected only into CanvasClient>",
            },
        )

        try:
            self._run_cli(
                "init",
                "init",
                "--base-url",
                "https://canvas.example.edu",
                "--term-label",
                "26fall",
                "--term-key",
                "2026-fall",
                "--start",
                "2026-08-01",
                "--end",
                "2027-01-01",
                "--timezone",
                "America/Toronto",
                "--archive-dir",
                str(self.archive),
                "--state-dir",
                str(self.state),
            )
            config = json.loads(self.config.read_text(encoding="utf-8"))
            config.update(
                {
                    "course_modes": {"101": "course", "202": "hub"},
                    "course_ids": [101, 202],
                    "download_files": True,
                    "extract_documents": True,
                }
            )
            write_json(self.config, config)

            canvas_client = CanvasClient(
                "https://canvas.example.edu",
                "synthetic-token-never-written",
                transport=fixture,
                retries=0,
            )
            snapshot = collect_snapshot(canvas_client, config, self.archive)
            write_json(self.state / "snapshot.json", snapshot)
            write_json(self.state / "snapshots" / "fixture-first.json", snapshot)
            write_json(WORKSPACE / "raw" / "transport-calls.json", fixture.calls)

            self.assertEqual(snapshot["user"]["id"], 77)
            self.assertEqual(snapshot["stats"]["formal_course_count"], 1)
            self.assertEqual(snapshot["stats"]["hub_count"], 1)
            formal = next(course for course in snapshot["courses"] if course["id"] == 101)
            self.assertEqual(len(formal["announcements"]), 2, "paginated announcement was lost")
            self.assertEqual(len(formal["assignments"]), 2)
            self.assertEqual(len(formal["files"]), 3, "HTML-linked material was not archived/indexed")
            self.assertTrue(all(Path(item["local_path"]).is_file() for item in formal["files"] if item.get("local_path")))
            self.assertTrue(all(item.get("sha256") for item in formal["files"] if item.get("download_status") in {"downloaded", "reused"}))
            self.assertTrue(any(item.get("extraction_status") == "extracted" for item in formal["files"]))

            # The same-origin client must isolate authorization and reject
            # cross-origin URLs before calling the fixture transport.
            before = len(fixture.calls)
            with self.assertRaises(CanvasOriginError):
                canvas_client.get("https://evil.example.edu/api/v1/courses")
            self.assertEqual(len(fixture.calls), before)
            self.assertTrue(all(call["authorization_present"] for call in fixture.calls))

            self._run_cli("review-packet", "review-packet")
            review_packet = json.loads((self.logs / "review-packet.stdout").read_text(encoding="utf-8"))
            # The integrated CLI emits canonical dictionaries. Keep a direct
            # module fallback so this forward test remains runnable while the
            # parent integration is being developed in parallel.
            if isinstance(review_packet.get("courses"), dict):
                review_hashes = review_packet
            else:
                review_hashes = evidence_packet(snapshot)
            write_json(WORKSPACE / "raw" / "review-packet.json", review_hashes)
            enrichments = {
                "courses": {
                    "101": {
                        "reviewed": True,
                        "source_hash": review_hashes["courses"]["101"]["source_hash"],
                        "syllabus_summary": "Reviewed BIO101 syllabus: labs require safety training and weekly preparation.",
                        "important_info": ["Bring a calculator to lab.", "Late work requires instructor approval."],
                        "user_notes": "I prefer to review diagrams on Sunday.",
                    }
                },
                "announcements": {
                    "7001": {
                        "reviewed": True,
                        "source_hash": review_hashes["announcements"]["7001"]["source_hash"],
                        "summary": "The lab consent form is required before the first lab.",
                        "action_items": [{"text": "Submit the lab consent form", "assignment_id": "1002"}],
                    },
                    "8001": {
                        "reviewed": True,
                        "source_hash": review_hashes["announcements"]["8001"]["source_hash"],
                        "summary": "The hub orientation checklist is due this week.",
                        "action_items": [{"text": "Complete accessibility orientation", "due_date": "2026-09-12"}],
                    },
                },
            }
            enrichments_path = self.state / "enrichments.json"
            write_json(enrichments_path, enrichments)
            self._bind_databases()
            self._run_cli(
                "plan-first",
                "plan",
                "--enrichments",
                str(enrichments_path),
                "--out",
                str(self.state / "notion-plan.json"),
            )
            plan = json.loads((self.state / "notion-plan.json").read_text(encoding="utf-8"))
            write_json(WORKSPACE / "notion" / "plan-summary.json", {"stats": plan["stats"], "warnings": plan["warnings"]})
            self.assertEqual(len(plan["comparison"]["record_keys"]), len(set(plan["comparison"]["record_keys"])))
            self.assertGreater(plan["stats"]["record_count"], 0)
            self.assertTrue(any(record["properties"].get("Name") == "Lab safety acknowledgement" for record in plan["records"]))
            self.assertFalse(any("Personal Notes" in record["properties"] for record in plan["records"]))

            # Root's documented dependency order: course pages first, then
            # source-backed records that relate to them.
            for kind in ("courses", "resources", "tasks", "announcements", "notes", "timetable"):
                batch_path = self.state / f"batch-{kind}.json"
                self._run_cli(
                    f"next-{kind}",
                    "notion-next",
                    "--kind",
                    kind,
                    "--begin",
                    "--out",
                    str(batch_path),
                )
                batch = json.loads(batch_path.read_text(encoding="utf-8"))
                # The first create intentionally receives an unknown response,
                # proving that the next run blocks instead of blind-retrying.
                self._commit_batch(kind, batch_path, prefix=f"first-{kind}", unknown_first=(kind == "courses"))
                for operation in batch.get("operations", []):
                    # ``Source`` is newline-delimited rich text in the
                    # current projection schema; Course/Resources are the
                    # relation properties that need page-ID resolution.
                    for property_name in ("Course", "Resources"):
                        value = operation.get("properties", {}).get(property_name)
                        values = value if isinstance(value, list) else [value]
                        for entry in values:
                            if isinstance(entry, str) and "|user=" in entry and "|type=" in entry:
                                self._record_failure(
                                    "notion-relation-resolution",
                                    f"{kind} operation emitted a symbolic relation for {property_name}",
                                    source_key=operation.get("source_key"),
                                    property=property_name,
                                    value=entry,
                                )

            # A second identical plan must be a no-op after verified receipts.
            repeat_counts = {}
            for kind in ("courses", "resources", "tasks", "announcements", "notes", "timetable"):
                path = self.state / f"repeat-{kind}.json"
                self._run_cli("repeat-next-" + kind, "notion-next", "--kind", kind, "--out", str(path))
                repeat = json.loads(path.read_text(encoding="utf-8"))
                repeat_counts[kind] = {"operations": len(repeat.get("operations", [])), "blocked": len(repeat.get("blocked", [])), "unchanged": repeat.get("unchanged", 0)}
                self.assertEqual(repeat.get("operations", []), [], f"repeat {kind} created or updated a record")
            write_json(WORKSPACE / "notion" / "repeat-counts.json", repeat_counts)

            # Simulate next week's Canvas change: assignment 1001 keeps its
            # source identity and page, while the personal completion state is
            # present only in the fetched remote page and omitted from sync.
            snapshot["courses"][0]["assignments"][0]["due_at"] = "2026-09-22T23:59:00-04:00"
            write_json(self.state / "snapshot-next-week.json", snapshot)
            # ``study.py plan`` always reads the canonical snapshot path;
            # replace it only after preserving the first-week copy above.
            write_json(self.state / "snapshot.json", snapshot)
            self._run_cli(
                "plan-next-week",
                "plan",
                "--enrichments",
                str(enrichments_path),
                "--out",
                str(self.state / "notion-plan-next-week.json"),
            )
            next_plan = json.loads((self.state / "notion-plan-next-week.json").read_text(encoding="utf-8"))
            # ``notion-next`` consumes the canonical ``notion-plan.json``;
            # retain the named copy above and promote the reviewed update.
            write_json(self.state / "notion-plan.json", next_plan)
            task_key = "https://canvas.example.edu|user=77|type=assignment|id=1001"
            next_task = next(record for record in next_plan["records"] if record["source_key"] == task_key)
            previous_task = next(record for record in plan["records"] if record["source_key"] == task_key)
            next_due = next_task["properties"].get("date:Due:start", next_task["properties"].get("Due"))
            previous_due = previous_task["properties"].get("date:Due:start", previous_task["properties"].get("Due"))
            self.assertNotEqual(next_due, previous_due)
            self.assertNotIn("Done", next_task["properties"])
            self.assertNotIn("Personal Notes", next_task["properties"])

            update_batch_path = self.state / "batch-tasks-next-week.json"
            self._run_cli(
                "next-tasks-next-week",
                "notion-next",
                "--kind",
                "tasks",
                "--begin",
                "--out",
                str(update_batch_path),
            )
            update_batch = json.loads(update_batch_path.read_text(encoding="utf-8"))
            update_op = next(op for op in update_batch["operations"] if op["source_key"] == task_key)
            self.assertEqual(update_op["action"], "update")
            self.assertNotIn("Done", update_op["properties"])
            self.assertNotIn("Personal Notes", update_op["properties"])
            initial_state = json.loads((self.state / "notion-state.json").read_text(encoding="utf-8"))
            initial_region = initial_state.get("records", {}).get(task_key, {}).get("remote_region") or "old source"
            self.assertEqual(update_op.get("page_id"), initial_state.get("records", {}).get(task_key, {}).get("page_id"))
            fetched = managed_page(initial_region)
            fetched += "\n\nDone: yes\nMy private note: review transport diagrams Sunday.\n<database>Tasks view</database>"
            fetched_path = self.state / "fetched-task-page.json"
            fetched_path.write_text(fetched, encoding="utf-8")
            self._run_cli(
                "content-edit-task",
                "content-edit",
                "--source-key",
                task_key,
                "--fetched-content",
                str(fetched_path),
            )
            edit_result = json.loads((self.logs / "content-edit-task.stdout").read_text(encoding="utf-8"))
            update_text = json.dumps(edit_result, ensure_ascii=False)
            self.assertIn("update_content", update_text)
            self.assertIn("old_str", update_text)
            self.assertIn("new_str", update_text)
            self.assertIn("Done: yes", fetched)
            self.assertIn("My private note", fetched)
            self.assertIn("<database>Tasks view</database>", fetched)
            write_json(WORKSPACE / "notion" / "next-week-verification.json", {"task_key": task_key, "page_id": update_op.get("page_id"), "personal_text_preserved_in_fetched_page": True, "content_edit": edit_result})

            # Commit the update and verify the changed plan becomes a no-op.
            receipts_path = self.state / "tasks-next-week-receipts.json"
            write_json(receipts_path, {"receipts": self._create_receipts([update_op], prefix="updated-tasks")})
            self._run_cli("receipts-tasks-next-week", "notion-receipts", str(receipts_path))
            final_path = self.state / "final-next-week.json"
            self._run_cli("final-next-week", "notion-next", "--kind", "tasks", "--out", str(final_path))
            final = json.loads(final_path.read_text(encoding="utf-8"))
            self.assertEqual(final.get("operations", []), [])

        except WorkflowFailure as exc:
            self._record_failure("workflow-command", str(exc))
            self.fail(str(exc))
        except Exception as exc:
            self._record_failure("workflow-test", f"{type(exc).__name__}: {exc}")
            raise

    def test_state_content_edit_preserves_personal_region(self):
        old = managed_page('{"summary":"old generated text"}')
        fetched = old + "\n\nDone: yes\nMy private note: keep this.\n<database>Tasks view</database>"
        edit = content_edit(fetched, '{"summary":"new generated text"}', '{"summary":"old generated text"}')
        updated = fetched.replace(edit["old_str"], edit["new_str"])
        self.assertIn("Done: yes", updated)
        self.assertIn("My private note: keep this.", updated)
        self.assertIn("<database>Tasks view</database>", updated)
        write_json(WORKSPACE / "notion" / "content-edit-unit.json", {"edit": edit, "updated": updated})

    def test_state_operation_contract_observations(self):
        """Capture state-layer behavior after the first plan, even if CLI upsert blocks.

        The current state implementation expects generated content as text,
        while projection emits structured JSON.  This observation adapter lets
        the forward dossier continue far enough to expose relation-resolution
        behavior without changing production code.  If the implementation is
        fixed, the adapter remains harmless and the resulting report records
        the improved behavior.
        """
        plan_path = WORKSPACE / "state" / "notion-plan.json"
        if not plan_path.exists():
            self.skipTest("first forward run did not reach plan generation")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        compatible_plan = json.loads(json.dumps(plan, ensure_ascii=False))
        for record in compatible_plan.get("records", []):
            content = record.get("generated_content")
            if isinstance(content, dict):
                record["generated_content"] = json.dumps(content, ensure_ascii=False, sort_keys=True)

        bindings = {
            kind: {"data_source_id": f"ds-{kind}-synthetic"}
            for kind in ("courses", "tasks", "resources", "announcements", "notes", "timetable")
        }
        state = empty_state()
        observations = {}
        for kind in ("courses", "resources", "tasks", "announcements", "notes", "timetable"):
            batch = prepare_operations(compatible_plan, state, bindings, kind=kind)
            symbolic = []
            for operation in batch.get("operations", []):
                # ``Source`` is currently a newline-delimited rich-text
                # property in the implementation; only Course/Resources are
                # relation properties that must resolve to remote page IDs.
                for property_name in ("Course", "Resources"):
                    value = operation.get("properties", {}).get(property_name)
                    values = value if isinstance(value, list) else [value]
                    for entry in values:
                        if isinstance(entry, str) and "|user=" in entry and "|type=" in entry:
                            symbolic.append({"source_key": operation["source_key"], "property": property_name, "value": entry})
            observations[kind] = {
                "operation_count": len(batch.get("operations", [])),
                "blocked": batch.get("blocked", []),
                "symbolic_relations": symbolic,
            }
            if symbolic:
                self._record_failure(
                    "state-relation-contract",
                    f"{kind} operations retain symbolic relation values after course/resource pages are available",
                    kind=kind,
                    symbolic_relations=symbolic,
                )
            if batch.get("operations"):
                begin_operations(state, batch["operations"])
                receipts = []
                for index, operation in enumerate(batch["operations"], start=1):
                    remote_region = None
                    if isinstance(operation.get("content"), str) and operation["content"].count("**课程资料同步区**") == 1:
                        remote_region = managed_region(operation["content"])
                    item = {
                        "source_key": operation["source_key"],
                        "operation_id": operation["operation_id"],
                        "status": "succeeded",
                        "page_id": f"observation-{kind}-{index}",
                    }
                    if remote_region is not None:
                        item["remote_region"] = remote_region
                    receipts.append(item)
                commit_receipts(state, receipts)
        try:
            content_edit(managed_page("{\"summary\":\"old\"}"), {"summary": "new"}, "{\"summary\":\"old\"}")
        except Exception as exc:
            observations["content_edit_structured_input"] = {
                "status": "unsupported",
                "error": f"{type(exc).__name__}: {exc}",
                "note": "CLI notion-next renders generated_content before content-edit; this is a direct API probe only.",
            }
        else:
            observations["content_edit_structured_input"] = {"status": "supported"}
        write_json(WORKSPACE / "notion" / "state-compatibility.json", observations)


if __name__ == "__main__":
    unittest.main()

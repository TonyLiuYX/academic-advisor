# Canvas → Notion projection schema

This document is the contract for `study_sync.projection.build_plan`. The
projection is pure: it consumes one canonical Canvas snapshot and optional
reviewed enrichment, then returns a JSON-serialisable plan. It does not call
Canvas, Notion, a model, or a browser. The host's apply phase may replace symbolic
relations with Notion page IDs after it has resolved the `Source Key` values.

## Plan envelope

```json
{
  "schema_version": 1,
  "term": {"key": "2026-fall", "label": "26fall", "start": "2026-08-01", "end": "2027-01-01", "timezone": "America/Toronto"},
  "databases": {"courses": {}, "tasks": {}, "resources": {}, "announcements": {}, "notes": {}, "timetable": {}},
  "records": [],
  "views": {"cross_course_source_index": {}, "term_filtered": {}},
  "warnings": [],
  "stats": {},
  "comparison": {}
}
```

The `databases` value is a machine-readable description of all six database
schemas. `records` are sorted by database kind and `source_key`; no generated
timestamp or random identifier is added by projection. `comparison` contains a
canonical JSON SHA-256 over the plan before `comparison` is added, plus a
sorted list and digest of record keys. This makes rerun comparisons
reproducible even when the same snapshot is projected more than once.

Each record has this shape:

```json
{
  "kind": "tasks",
  "source_key": "https://canvas.example.edu|user=1|type=assignment|id=42",
  "course_key": "https://canvas.example.edu|user=1|type=course|id=123",
  "display_timezone": "America/Toronto",
  "properties": {
    "Name": "Problem set 1",
    "Source Key": "https://canvas.example.edu|user=1|type=assignment|id=42",
    "Course": ["https://canvas.example.edu|user=1|type=course|id=123"],
    "Term": "2026-fall",
    "Source": "https://canvas.example.edu|user=1|type=assignment|id=42"
  },
  "generated_content": {},
  "user_content": {}
}
```

`course_key` is retained for hub records so they remain traceable even though
hub courses do not receive a row in the formal `courses` database. A root
writer may bind a hub key to a managed hub page or leave that relation
unresolved while retaining the source record. Hub records omit the `Course`
relation and carry a human `Source Space` text property; formal-course records
carry `Course` as a one-element relation array.

Property values are flattened for the Notion MCP boundary:

- date properties use flattened MCP keys such as `date:Due:start` and
  `date:Due:is_datetime` (the latter is numeric `1` for a date-time and `0`
  for a date-only value);
- checkbox values are exactly `__YES__` or `__NO__`;
- `Course` is an array of symbolic source keys because it is a Notion
  relation; `Resources` is an array targeting the Resources database;
- `Source` is newline-delimited rich text because a task can cite both an
  assignment and an announcement, which are different Notion databases;
- absent source values are omitted rather than replaced with guessed values.

The property names `Name`, `Source Key`, `Course`, `Term`, and `Source URL`
are shared across applicable databases. `Source Key` is the exact state key
the host should use for upsert and comparison.

## Stable source keys

The canonical format is:

```
<origin>|user=<user-id>|type=<source-type>|id=<object-id>[|version=<version>]
```

Values are URL-escaped while the Canvas origin's normal URL punctuation is
preserved. The key therefore contains the Canvas origin, the Canvas user ID,
the source type, and the source object ID. Resource records append a version
when Canvas provides one. A duplicate resource ID with two versions becomes
two records; a duplicate ID/version is merged deterministically.

Source types used by projection are `course`, `assignment`, `announcement`,
`announcement_action`, `course_action`, `calendar_event`, `reviewed_event`,
`file`, `page`, `resource`, `attachment`, and `syllabus`. A generated
`announcement_action` still has the actual announcement key in its `Source`
provenance text. A reviewed course action/event ID is prefixed by its course
ID in the source-key object ID. No source key is generated from a task counter.

## Six databases

The plan's `databases` field is authoritative. The following is the compact
human-readable view of the same schema.

| Database | Required / identifying fields | Other source fields |
| --- | --- | --- |
| `courses` | `Name`, `Source Key`, `Canvas ID` | `Course Code`, `Course Mode`, `Term`, `Source URL`, `Canvas Status`, `Syllabus Available`, `Enrichment Status`, counts |
| `tasks` | `Name`, `Source Key` | `Course`, `Term`, `Source URL`, `Source`, `Type`, `Due`, `Due Choices`, `Due Conflict`, `Has Attachments`, `Attachment Count`, `Resources`, `Assignment ID`, `Announcement ID`, `Canvas Status`, `Sync Status` |
| `resources` | `Name`, `Source Key`, `Canvas ID` | `Course`, `Term`, `Source URL`, `Type`, `Syllabus`, `Version`, `Local Path`, `Download Status`, `Extraction Status`, `SHA256`, `Sync Status` |
| `announcements` | `Name`, `Source Key`, `Canvas ID` | `Course`, `Term`, `Source URL`, `Posted`, `Type`, `Has Action Items`, `Sync Status` |
| `notes` | `Name`, `Source Key` | `Course`, `Term`, `Source URL`, `Type`, `Sync Status` |
| `timetable` | `Name`, `Source Key` | `Course`, `Term`, `Source URL`, `Start`, `End`, `All Day`, `Type`, `Sync Status` |

Personal workflow state is deliberately absent from synced properties. In
particular, projection never emits `Done`, `Completed`, `Planned`, `Plan`,
`Priority`, or personal notes as Notion properties. A caller may maintain
those fields in Notion without projection overwriting them.

For assignments, `Canvas Status` prefers the current student's
`submission.workflow_state` (`unsubmitted`, `submitted`, `graded`, and so on)
and falls back to the assignment's publication state only when submission data
is unavailable. It is an official Canvas status, not a completion checkbox;
personal completion remains the separate Notion `Done` property.

Assignments become `tasks` regardless of whether they have a due date or
attachments. An assignment with no date simply omits `date:Due:start` and
`date:Due:is_datetime`, and has `Due Conflict=__NO__`; an assignment with
multiple distinct effective dates omits the flattened Due keys, includes all
values in `Due Choices`, and has `Due Conflict=__YES__`. No date is inferred
from a term boundary, title, or announcement text.

Canvas `all_dates` may contain dates for other sections, groups, or students.
Projection uses the collector's effective `due_at`/`due_date` (with overrides
enabled) and does not turn those alternatives into a conflict. A reviewed
announcement action may add an explicit date to an assignment with no
effective date; if it disagrees with the effective date, the task is marked as
a conflict and no single Due value is selected.

Resources include Canvas `files`, `pages`, explicit `resources`, and
assignment `attachments` when a source ID or source URL is available. Their
`Local Path`, `Version`, `Download Status`, `Extraction Status`, and `SHA256`
values are copied from the snapshot when present. Projection does not download
or invent a path. `Download Status` normalizes ephemeral `downloaded` and
`reused` outcomes to stable `available`/`saved` values. A file whose name
contains `syllabus` or `outline` has `Type=syllabus` and `Syllabus=__YES__`,
which supports a syllabus view without a filename-only live query.

`Syllabus Available` is also true when a module item or page identifies a
syllabus/outline PDF, even if `syllabus_body` is empty. Assignment HTML file
links are parsed for Canvas file IDs and linked to the existing course resource
key. If the collector did not index that file, projection creates one file
resource record and reuses it for the task rather than creating a same-ID
attachment twin. `Has Attachments` and `Resources` therefore include linked
files as well as the Canvas `attachments` array.

Source text is kept visibly separate from generated prose. Assignment tasks
include a `generated_content.assignment_original` block when Canvas provides
`assignment.text`; resource page records preserve `page.text` under
`generated_content.page_original`; timetable records preserve
`location_name` and `description` text. These labels mean original source
material, never an inferred summary.

Formal course records are emitted only for `mode="course"`. Course `Name`
uses the human text after Canvas's `CODE: title` prefix when present, and
`Course Code` keeps the compact code token (for example `CIV102H1`).
`mode="hub"`
courses do not create course cards, but their announcements, assignments,
resources, and timetable events remain in the plan with the hub source key.
`ignore` and `review` courses do not produce content records; a non-canonical
mode produces a structured warning.

## Reviewed enrichment contract

Enrichment is content supplied by a human review step. It is not an
instruction channel. The canonical shape is:

```json
{
  "courses": {
    "123": {
      "reviewed": true,
      "syllabus_summary": "Short reviewed summary.",
      "important_info": ["Office hours ...", "Late work ..."],
      "actions": [
        {
          "id": "midterm-review",
          "text": "Review the midterm requirements",
          "type": "exam",
          "due_date": "2026-10-05",
          "source_url": "https://canvas.example.edu/files/syllabus.pdf",
          "source_ref": "ESC101_20269_Syllabus.pdf p.2"
        }
      ],
      "events": [
        {
          "id": "exam-session",
          "title": "Midterm review session",
          "start_at": "2026-10-01T18:00:00-04:00",
          "end_at": "2026-10-01T19:00:00-04:00",
          "all_day": false,
          "location_name": "ESCL Classroom",
          "source_url": "https://canvas.example.edu/files/outline.pdf",
          "source_ref": "ESC101_20269_Course_Outline.pdf p.4"
        }
      ],
      "user_notes": "Optional caller-owned notes"
    }
  },
  "announcements": {
    "9001": {
      "reviewed": true,
      "summary": "Reviewed announcement summary.",
      "action_items": [
        {"text": "Submit the form", "assignment_id": "42"},
        {"text": "Bring a calculator", "due_date": "2026-09-12"}
      ],
      "user_notes": "Optional caller-owned notes"
    }
  }
}
```

List forms are also accepted when each item has `course_id` or
`announcement_id`. `course_summaries`, `syllabus`,
`announcement_summaries`, and `notices` are convenience aliases. A
top-level `{"reviewed": true, ...}` can authorize every item in a trusted
review packet; otherwise each item must have `reviewed: true` (or
`status: "reviewed"`).

The reviewed `courses` item also accepts syllabus/outline facts that are not
Canvas Assignment or Calendar Event objects:

- `actions` is a list of explicit review items with `id`, `text`, optional
  `type`, `due_date` (or `due_at`), `source_url`, `source_ref`, and optional
  `assignment_id`. Each standalone item becomes a `tasks` record with a
  source key whose type is `course_action` and whose ID is
  `<course-id>:<action-id>`. The action's source reference and URL are
  rendered in the task body. If `assignment_id` matches an assignment in the
  same course, the action is merged into that assignment task instead; its
  `course_action` key is retained in the task's `Source` provenance and the
  action's review/source details remain in `generated_content`. A reviewed
  action date fills a missing assignment date, while a disagreement with the
  assignment's effective Canvas date produces `Due Conflict=__YES__` and
  `Due Choices` rather than silently replacing the date. An action without a
  stable `id` is omitted with a warning, because deriving an identity from
  paraphrasable prose would create duplicate tasks.
- `events` is a list of explicit review items with `id`, `title`, `start_at`,
  optional `end_at`, `all_day`, `location_name`, `source_url`, and
  `source_ref`. Each becomes a `timetable` record with a source key whose
  type is `reviewed_event` and whose ID is `<course-id>:<event-id>`. Missing
  `start_at` or a stable ID is omitted with a warning. Date-only values stay
  date-only; no time is invented. The source reference and URL are rendered
  in the timetable body.

Only the reviewed course item (or an explicitly reviewed top-level packet)
authorizes these actions/events. Raw syllabus prose cannot create them.

Without reviewed enrichment, announcement `generated_content` contains:

```json
{
  "summary": "[PENDING REVIEW]",
  "summary_status": "pending_review",
  "action_items": ["[PENDING REVIEW]"],
  "action_items_status": "pending_review"
}
```

No action task is created from an unreviewed announcement. A reviewed
announcement with an explicit empty `action_items: []` is treated as reviewed
and creates no action task. A reviewed action item with `assignment_id` that
matches a snapshot assignment is appended to that assignment task's `Source`
rich-text provenance and `generated_content.reviewed_actions`; it does not create a
duplicate task. An unresolved or ambiguous `assignment_id` produces a warning
and keeps the action as a source-backed announcement task.

Syllabus summaries and important information use the same reviewed gate. When
syllabus HTML exists but reviewed enrichment is absent, the formal course gets
pending markers and a pending syllabus note. Raw syllabus HTML is never
truncated into a synthetic summary. Explicit reviewed text is bounded at
2,000 characters, with a truncation marker; raw Canvas bodies are not used as
generated summaries. Caller-owned `user_notes` remain under the record's
top-level `user_content` and are never copied into `generated_content` or a
synced property.

`render_generated_content(record)` is a public deterministic renderer for
the host's managed page wrapper. It returns bounded readable Markdown with Chinese
section labels, ordinary task bullets plus a reminder to use Notion's `Done`
property, announcement action checkboxes, dates, pending/conflict markers,
original-source blocks, and source links. Course and syllabus-note pages have
a 6,000-character body budget so a reviewed packet is not cut off at the
task-sized limit; other kinds have smaller per-kind budgets. If a body exceeds
its budget, it is marked in Chinese as an excerpt and all source links are
appended outside the bounded body. Original source blocks that were bounded
at extraction time are labelled `（节选）`. It does not add a user-section
wrapper or merge caller-owned notes. Each record also carries
`display_timezone` from `term.timezone` (with the user's timezone as a
fallback); only the rendered text uses this timezone. Official Notion date
properties retain their precise ISO values.

## Views and comparison

`views.cross_course_source_index` is a linked-database specification over
source-backed tasks, announcements, resources, notes, and timetable records.
It filters on a non-empty `Source Key`, then groups by `Course` and `Type`.
`views.term_filtered` is a linked-database collection over all six databases
filtered on the snapshot term key. These are declarative specs for the host's
Notion orchestration; projection does not create views.

`comparison.plan_hash_sha256` is calculated from the complete plan without the
`comparison` member. It deliberately excludes the snapshot's ephemeral
`generated_at`, so a fresh collection does not mass-update unchanged records.
`record_keys` and `record_keys_sha256` give a concise state comparison for a
later apply run. `bindings` is accepted by
`build_plan` for API compatibility with the host's apply phase; symbolic relation
values intentionally remain unchanged in this projection step.

## Optional ICS export

`tasks_to_ics(plan)` (also exported as `export_tasks_ics`) writes only tasks
with one `date:Due:start` value and skips missing or conflicting dates. UID values derive
from `Source Key`; aware date-times are converted to explicit UTC values with
the RFC 5545 trailing `Z`. Naïve date-times use the record's
`display_timezone`; if no valid timezone is available they are skipped rather
than guessed as UTC. A fixed `DTSTAMP` keeps the output reproducible. This is
a convenience export and is not a Notion database mutation.

# Design

## 1. Purpose and boundaries

`crawlbudget` is a deterministic planning and controlled-execution system for
small, synthetic, multi-site collection jobs. It demonstrates how authorization,
robots policy, quotas, rate limits, leases, retries, audit records, and reporting
fit together without relying on external infrastructure.

The design assumes a single operator and a local machine. It does not attempt to
provide distributed scheduling, crawling discovery, authentication, or a
production-scale fetcher. Network scope is intentionally narrow: numeric
loopback HTTP endpoints only.

## 2. High-level flow

```text
YAML config ──> validation ──> scope + robots + budget planner ──> leases
                                      │                              │
                                      v                              v
                              planned_requests                 SQLite ledger
                                      │                              │
                                      v                              │
                              guarded executor ──> request_events ──┤
                                      │                              │
                                      v                              │
                              block/warning records ────────────────┤
                                                                     v
                                                       summary/report renderers
                                                                     │
                                                                     v
                                                   JSON · Markdown · HTML + golden
```

1. `config.py` loads YAML, aggregates validation issues, resolves fixture paths,
   and computes config/seed/robots fingerprints.
2. `planner.py` reads seed URL files, validates each URL with `scope.py`, applies
   the matching robots policy, deduplicates by stable URL hash, allocates budget,
   assigns virtual offsets, and creates deterministic leases.
3. `pipeline.py` persists the run, leases, planned requests, planning blocks, and
   warnings, then invokes the executor unless the run is dry.
4. `executor.py` revalidates each URL, enforces budget and retry limits, performs
   one guarded `GET` attempt at a time, records request/blocked events, and marks
   planned rows complete.
5. `report.py` builds the summary from the ledger, computes the report
   fingerprint, and persists JSON/Markdown/HTML. `golden.py` compares stored
   reports with fixtures.
6. `review_server.py` serves a read-only audit view over the same ledger.

## 3. Module responsibilities

| Module | Responsibility |
| --- | --- |
| `config.py` | YAML parsing, aggregate validation, path resolution, fingerprints. |
| `models.py` | Typed records shared by planning, execution, storage, and reporting. |
| `scope.py` | URL parsing, loopback/site-prefix enforcement, canonicalization, hashing. |
| `robots.py` | Offline robots parser, group selection, rule matching, crawl delay. |
| `budget.py` | Integer token bucket, quota helpers, lease identity/expiry/reason logic. |
| `planner.py` | Deterministic plan construction and budget allocation. |
| `storage.py` / `storage_schema.py` | SQLite ledger, idempotent writes, queries. |
| `executor.py` | Guarded HTTP execution, revalidation, retries, body caps, receipt admission. |
| `receipt.py` | Stable dispatch identities, offline HMAC receipts, verdict logic, recovery client. |
| `pipeline.py` | End-to-end orchestration, resume, reset, inspect. |
| `report.py` | Summary construction, report persistence. |
| `report_render.py` | Stable Markdown and self-contained HTML rendering. |
| `golden.py` | Field-level and Markdown golden comparison. |
| `demo_site.py` | Flask synthetic sites and background server lifecycle. |
| `review_server.py` | Read-only Flask review console with CSP. |
| `cli.py` | Argument parsing, command dispatch, exit codes. |

Most Python files are kept below 300 lines; the split follows responsibility, not
line-count targets. Modules that coordinate several responsibilities
(`executor.py`, `storage.py`) intentionally exceed it rather than hide coupling
behind artificial seams.

## 4. Run lifecycle

A run has one of these persisted statuses:

- `running` — created, executing;
- `completed` — every granted request has a terminal planned outcome;
- `partial` — execution finished but at least one granted request is incomplete;
- `dry_run` — planning and reporting only, with no HTTP attempts.

The run row stores `created_at`, virtual `started_at`/`finished_at`, the virtual
date, `dry_run`, status, and three input fingerprints. The `created_at` and
`started_at` values remain stable on resume. `finished_at` is recomputed as the
maximum granted offset plus one second (or the virtual start for a dry run).

### Resume semantics

- Reusing a run ID loads the existing started time and reuses its created time.
- The config/seed/robots fingerprints must match exactly; otherwise the run is
  rejected before any new work.
- Planned rows and leases are upserted, not duplicated.
- Request events are unique per `(run_id, site_id, url_hash, attempt)`.
- A completed planned request is skipped. A non-terminal event continues from
  the next attempt number, bounded by the site retry policy and the run attempt
  limit.
- Re-running a finished demo therefore makes zero HTTP calls and reproduces the
  same reports.

## 5. Determinism

Determinism is a design invariant, not an incidental property.

- Time is virtual. Plan offsets are integer milliseconds from `virtual_start`.
  The executor uses them only to timestamp audit records; it does not sleep
  between attempts except through the injected pacing sleeper.
- Jobs are sorted by `(priority, job_id)`; sites are reported in sorted ID order;
  seed URLs preserve file order after deduplication.
- Rates use integer arithmetic. `interval_ms = ceil(1000 / max_rps)` is
  conservative for fractional rates. A token bucket with `burst` capacity and a
  crawl-delay floor yields a stable offset list.
- Lease IDs are SHA-256 derived. URLs are canonicalized and hashed with SHA-256.
- JSON is emitted with sorted keys and no insignificant whitespace in canonical
  fingerprints; report JSON uses stable indentation.
- The report fingerprint is computed over the summary dict while the
  `fingerprints.report` field is blank, then inserted. This avoids a fixed-point
  problem and makes the fingerprint reproducible.
- Golden fixtures contain no absolute paths, wall-clock timestamps, or local
  usernames.

## 6. Data model

### `runs`

Primary key `run_id`; fields `created_at`, `started_at`, `finished_at`,
`virtual_date`, `dry_run`, `status`, and the config/seed/robots/report
fingerprints. `create_run` upserts status and fingerprints but preserves the
original `started_at`.

### `site_leases`

Primary key `(run_id, lease_id)`. Records site, job, granted/used/remaining
requests, expiry, and reason. `used_requests` increments only when a planned
request first transitions to completed, so retries do not multiply lease usage.

### `planned_requests`

Primary key `(run_id, site_id, url_hash, attempt)`. Stores order, canonical URL,
lease, allow/block decision, block evidence, virtual offset, robots rule/evidence,
seed origin, completion flag, final outcome, and attempt number. Upserts refresh
plan data while keeping execution state.

### `request_events`

Unique per `(run_id, site_id, url_hash, attempt)`. Records the actual HTTP
attempt: status, outcome, elapsed milliseconds, retained bytes, content type,
body SHA-256, error code, evidence JSON, and virtual timestamp.

### `blocked_events`

Unique per `(run_id, site_id, url_hash, stage, reason)`. Captures planning and
execution blocks with structured evidence. `INSERT OR IGNORE` makes repeated
planning idempotent.

### `warnings`

Unique per `(run_id, code, message, context_json)`. Used for malformed robots
lines, duplicate seeds, unreadable seed files, and similar non-fatal conditions.

### `run_reports`

Primary key `run_id`; stores JSON, Markdown, and HTML report bodies, the report
fingerprint, and creation time. Persisting a report also updates the run row's
report fingerprint.

Foreign-key enforcement is enabled; WAL mode is used for file databases and
memory mode for `:memory:`. Runtime directories are created automatically.

## 7. Planning algorithm

For each job, in priority order:

1. Read seed URLs from the registered fixture. Unreadable files become
   `SEED_FILE_UNREADABLE` warnings and contribute no URLs.
2. Deduplicate exact repeated strings within the job and normalized
   `(site_id, url_hash)` pairs across jobs. Duplicates produce warnings and are
   not planned.
3. Validate scope. A blocked URL gets a stable `scope_*` reason.
4. Evaluate the robots policy only for in-scope URLs.
5. Choose the first applicable block reason: disabled job, disabled site, scope,
   robots. Attach scope and robots evidence.
6. Count eligible, unblocked requests per job.

After all jobs are read, for each site:

1. Compute `quota_left` from the previous daily attempt count.
2. Compute `run_limit = min(max_requests_per_run, quota_left)`.
3. Grant the first `min(eligible_count, run_limit)` eligible requests in job/seed
   order.
4. Schedule granted offsets with the token bucket and the effective crawl floor.
5. Mark the remainder with `quota_exceeded` or `run_limit_exceeded` and evidence.

Lease reasons are chosen after allocation: disabled job/site, granted work,
quota/run-limit limits, the most common block reason, or
`no_eligible_requests`.

## 8. Guarded execution

For every granted planned request:

1. Re-run scope validation against the current URL and site registration. A
   failure is an execution block (`EXEC_PREFLIGHT_BLOCKED`) and no HTTP call is
   made.
2. If a terminal event already exists, mark the planned row according to that
   outcome and skip.
3. Before each attempt, verify the remaining attempt budget. If exhausted,
   record a budget block and a `budget_exhausted` final outcome.
4. Apply pacing via the injected sleeper, then issue `GET` with a bounded
   timeout, `allow_redirects=False`, `stream=True`, `trust_env=False`, and a
   fixed `User-Agent`.
5. `3xx` responses are never followed. The target is classified; a non-loopback
   location produces `redirect_non_loopback`, otherwise `redirect_blocked`.
6. `2xx` bodies are read in chunks and capped by `Content-Length` and the actual
   retained size. Oversized bodies produce `body_too_large` and are not stored.
7. Retryable statuses (`408`, `425`, `429`, `500`, `502`, `503`, `504`) and
   timeout/transport failures retry up to `max_retries`; the last attempt is
   terminal. A non-retryable HTTP status is terminal immediately.
8. Successful and terminal events close the response, record the event, and
   mark the planned row complete exactly once.

Attempts are counted per site from persisted request events, so an interrupted
process can resume without exceeding the run limit.

### 8.1 Pre-dispatch intents and receipt admission

Before each GET, a `dispatch_intent` is persisted with a stable identity over
`run_id / site_id / job_id / url_hash / attempt / lease_id`. On resume, any
intent still in the open `dispatched` state is verified against the site before
a second GET is allowed:

- a verified receipt settles the spend under the `audit` or `redeliver`
  contract without re-dispatching;
- a definite site-side absence retires the identity and permits one budgeted
  re-dispatch;
- an unverifiable, identity-conflicting, or lease-conflicting claim quarantines
  the spend on the dispatching lease and blocks re-dispatch.

The receipt proves only site acceptance, never local delivery. Quarantined and
receipt-settled spend is projected into `lease_accounting`; the report's receipt
attestations carry the truthful delivery conclusion for each identity.

## 9. Reporting and golden verification

`build_summary` reads planned rows, leases, request events, blocks, and warnings.
It reports totals, per-site metrics, lease usage, status counts, blocked-reason
counts, top blocked reasons, robots match evidence, and warnings. Stable sort
orders make the output reproducible.

Markdown rendering uses fixed table headers and empty-state text. HTML uses the
same section data and inline CSS; it references no scripts, fonts, or images.
`check-golden` compares the summary recursively and then the Markdown text. It
returns per-field old/new values, making drift actionable.

## 10. Extending the system

Appropriate extensions preserve the safety model:

- Add a new local fixture site and register its loopback base URL.
- Add plan/report fields with deterministic ordering and update the golden
  fixtures.
- Add a new read-only review route that renders existing ledger data.
- Add tests under `tests/` using the hermetic config/server helpers.

Extensions that add arbitrary remote hosts, redirect following, credential
handling, or bypass switches are explicitly outside the design.

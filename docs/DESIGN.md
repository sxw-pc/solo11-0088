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
| `executor.py` | Guarded HTTP execution, revalidation, retries, body caps. |
| `pipeline.py` | End-to-end orchestration, resume, reset, inspect. |
| `report.py` | Summary construction, report persistence. |
| `report_render.py` | Stable Markdown and self-contained HTML rendering. |
| `golden.py` | Field-level and Markdown golden comparison. |
| `demo_site.py` | Flask synthetic sites and background server lifecycle. |
| `catalog_offers.py` | Offer extraction only from a successful body whose SHA matches the capture event; descriptive snapshot basket quotes. |
| `promotion_economics.py` | Shared stock reservation and per-order money math: free-shipping threshold, vendor rebate, merchant contribution floor. |
| `promotion_plan.py` | Whole-batch, per-contract branch-and-bound promotion search with budget, profit, stock, and stable tie-break rules. |
| `catalog_promotion.py` | Greedy per-cart preview (baseline) plus the verified batch report CLI. |
| `review_server.py` | Read-only Flask review console with CSP. |
| `cli.py` | Argument parsing, command dispatch, exit codes. |

Each Python file is kept below 300 lines; the split follows responsibility, not
line-count targets.

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

## 9. Reporting and golden verification

`build_summary` reads planned rows, leases, request events, blocks, and warnings.
It reports totals, per-site metrics, lease usage, status counts, blocked-reason
counts, top blocked reasons, robots match evidence, and warnings. Stable sort
orders make the output reproducible.

Markdown rendering uses fixed table headers and empty-state text. HTML uses the
same section data and inline CSS; it references no scripts, fonts, or images.
`check-golden` compares the summary recursively and then the Markdown text. It
returns per-field old/new values, making drift actionable.

## 10. Whole-batch promotion planning

`catalog_offers.extract_offers` only reads a body whose capture event is
`outcome=success` and whose SHA-256 matches the event, so every price and stock
figure used downstream is tied to a verified capture. `quote_basket` supplies
the descriptive pre-discount basket total.

`catalog_promotion.preview_promotions` remains a deliberately naive baseline:
it takes the highest-`uplift_points` offer per cart independently. Those picks
can share a budget they collectively exceed (the fixture's A+C+E discounts
total 2500 against a 1300 merchant cap) or breach the merchant contribution
floor, so they are not a signable customer offer.

`promotion_plan.plan_promotions` builds the signable plan for each authorized
funding contract:

1. Carts are quoted against the real catalog and reserve shared stock in
   sorted `cart_id` order; a cart whose SKUs are missing, understocked, or
   already committed is blocked and never signed.
2. For each offer, both possible funders are evaluated where allowed. A vendor
   rebate exists only for `vendor_eligible` offers and only when the contract
   has a positive vendor cap; an offer is funded by exactly one party.
3. Per-order economics are: net merchandise = gross - discount; a 600 shipping
   fee applies whenever net is below the 4000 free-shipping threshold;
   merchant contribution = net + actual shipping - goods cost - merchant
   shipping cost + vendor rebate, and must be at least 300 per signed order.
4. A bounded depth-first branch-and-bound enumerates every feasible
   "no offer or one (offer, funder)" assignment per cart (at most 9 choices
   per cart, 8 carts). Budget overflows are pruned and counted; the uplift
   bound only discards subtrees strictly below the incumbent, so no
   equal-score tie-break candidate is lost.
5. The objective maximizes total `uplift_points`, then total merchant
   contribution, then the smallest stable `(cart_id, offer_id, funder)` key.
   `uplift_points` are fictional quote-priority scores, not causal claims.

The two fixture contracts illustrate why funding assignment is a cross-cart
causal decision rather than a renamed block switch: under `merchant_pays`
(merchant cap 1300, vendor cap 0) cart-1/A is profit-ineligible (contribution
200) and the optimum is C+F at 14 points; under `vendor_eligible` (vendor cap
1000, merchant cap 800) the same A becomes eligible via vendor rebate
(contribution 1200, customer pays 3800), freeing merchant budget so A(vendor)
+C(merchant) reaches 20 points. Run:

```bash
python -m crawlbudget.catalog_promotion --fixture fixtures/catalog_promotion/intake.json
```

## 11. Extending the system

Appropriate extensions preserve the safety model:

- Add a new local fixture site and register its loopback base URL.
- Add plan/report fields with deterministic ordering and update the golden
  fixtures.
- Add a new read-only review route that renders existing ledger data.
- Add tests under `tests/` using the hermetic config/server helpers.

Extensions that add arbitrary remote hosts, redirect following, credential
handling, or bypass switches are explicitly outside the design.

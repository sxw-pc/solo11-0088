# 采限预算台 (crawlbudget)

`crawlbudget` is a fully local, offline, reproducible **crawl-budget control plane**.
It plans and executes small multi-site collection jobs under explicit robots.txt
authorization, per-site daily quotas, request-rate limits, per-run request
budgets, deterministic audit leases, and a durable SQLite ledger.

The project is a reference implementation for controlled collection. It is not
a general-purpose scraper: every request targets a synthetic Flask site on a
loopback address, every decision is recorded, and there is no configuration
switch that permits external network access.

## Quick start

Requirements: Python 3.11+ and the dependencies in `requirements.txt`.

```bash
python -m pip install -r requirements.txt

# Plan, start the three loopback mock sites, execute, and write reports.
python -m crawlbudget demo --reset

# Run the test suite.
pytest -q

# Inspect the persisted run and compare it with the golden fixtures.
python -m crawlbudget inspect-run --run-id demo-run
python -m crawlbudget check-golden --run-id demo-run

# Start the read-only review console.
python -m crawlbudget serve --port 8765
# Open http://127.0.0.1:8765/
```

The demo is deterministic. A successful run prints counts in this shape:

```text
run: demo-run status: completed
planned=21 granted=14 executed=14 attempts=17 blocked=9
reports: .../var/reports/demo-run/summary.json | .../summary.md | .../summary.html
```

Re-running with the same run ID performs zero new HTTP requests and produces
byte-identical JSON and Markdown reports. `check-golden` exits with
`GOLDEN_OK demo-run`.

## Commands

| Command | Purpose |
| --- | --- |
| `python -m crawlbudget demo --reset` | Start loopback mock sites, run the full deterministic demo, and write reports. |
| `python -m crawlbudget run` | Plan and execute a configured run; starts mock sites unless `--dry-run` or `--no-mock`. |
| `python -m crawlbudget plan` | Print the deterministic plan without making HTTP requests. |
| `python -m crawlbudget serve --port 8765` | Start the read-only local review console (loopback only). |
| `python -m crawlbudget inspect-run --run-id ID` | Print the stored run summary as JSON. |
| `python -m crawlbudget lease-action --run-id ID ...` | Cancel, transfer, or expire a staged lease grant and refresh its accounting. |
| `python -m crawlbudget check-golden --run-id ID` | Compare the stored report with `fixtures/expected/`. |

Common run options include `--config`, `--db`, `--reports-dir`, `--run-id`,
`--virtual-start`, and `--dry-run`. Use `--help` on any subcommand for details.

The offline `fixtures/lease_lifecycle.yaml` scenario has two jobs competing for
two site grants. Stage it with `run --dry-run --config fixtures/lease_lifecycle.yaml`,
then use `lease-action` with a planned URL hash from `inspect-run`. `cancel`
returns a grant, `transfer` moves one live grant to an otherwise quota-blocked
URL in another job on the same site, and `expire` settles unspent grants at the
lease's UTC boundary. Each action needs a unique ID, UTC `--as-of`, and a
`--reason`. For `expire`, `--source` is the site ID. Actions are recorded before
HTTP dispatch, survive replanning of the same run, and produce per-job rows in
`lease_accounting` alongside the original request ledger.

Exit codes: `0` success, `1` general/storage error, `2` configuration or CLI
error, `3` golden mismatch, `4` demo-server error, `130` interrupted.

## Configuration at a glance

Sites and jobs are declared in YAML. Every site is a synthetic loopback service
with its own robots fixture, quota, rate limit, response cap, and retry policy.

```yaml
sites:
  - site_id: mock-alpha
    base_url: http://127.0.0.1:5021/mock-alpha/
    robots_file: robots/mock-alpha.txt
    daily_quota: 6
    max_requests_per_run: 10
    max_rps: 20
    burst: 3
    crawl_delay_ms: 20
    max_response_bytes: 65536
    max_retries: 0
    timeout_ms: 2000
    enabled: true
jobs:
  - job_id: job-alpha-main
    site_id: mock-alpha
    priority: 10
    seed_file: seeds/mock-alpha_urls.txt
    enabled: true
```

See [docs/CONFIG.md](docs/CONFIG.md) for every field, default, validation rule,
URL normalization rule, and error code.

## Budget and authorization model

For each run and site, the planner computes:

```text
quota_left      = max(0, daily_quota - attempts_already_recorded_today)
run_limit       = min(max_requests_per_run, quota_left)
granted         = min(number_of_eligible_urls, run_limit)
```

- **Daily quota** counts HTTP attempts (including retries) recorded in
  `request_events` for the same virtual UTC date. A run may exclude its own
  earlier attempts when recomputing prior usage.
- **Per-run limit** caps attempts, not merely successful responses.
- **Token bucket** uses integer virtual milliseconds. The refill interval is
  `ceil(1000 / max_rps)`, which is conservative for fractional rates. The
  effective inter-request floor is the larger of the bucket interval and the
  site's `crawl_delay_ms` / robots `Crawl-delay`.
- **Leases** are deterministic: `lease-` plus the first 16 hex characters of
  `SHA-256(run_id U+001F site_id U+001F job_id)`. They expire at the next UTC
  midnight after the virtual start.
- **Block reasons** are stable strings: `robots_disallow`, `robots_missing`,
  `quota_exceeded`, `run_limit_exceeded`, `job_disabled`, `site_disabled`,
  `scope_*`, `redirect_non_loopback`, `response_body_too_large`, and related
  execution codes.

Planning is independent of wall-clock time and process scheduling. Given the
same config, fixtures, run ID, virtual start, and prior ledger state, the plan,
lease IDs, offsets, reports, and fingerprints are identical.

## Remote receipt verification

A collection can reach the site yet never reach the local ledger when the
process is interrupted between the GET and the `request_events` write. Three
facts are therefore kept strictly separate:

1. **remote receipt** — the site accepted one collection;
2. **page delivery** — the local ledger retained the response body;
3. **local quota consumed** — the owning lease was billed.

Before every GET the executor persists a `dispatch_intent` whose stable
identity binds `run_id / site_id / job_id / url_hash / attempt / lease_id`
(`SHA-256` of the canonical binding). The loopback mock site answers a matched
GET with an HMAC-signed receipt verifiable fully offline; the key is derived
deterministically from the site id, so no key-distribution endpoint is trusted.

On resume, an open intent is verified against the site's `__receipt__`
endpoint before any re-dispatch:

- **matched receipt** — the spend is settled under one of two explicit
  contracts selected with `--receipt-contract`:
  - `audit` (default): bill the spent quota but keep the truthful
    `not_delivered` conclusion; a receipt alone never reports a retained page.
  - `redeliver`: re-fetch the same-identity body from `__recover__` at **zero
    new collection cost** and mark it `recovered_after_receipt`, never claiming
    the original response was delivered.
- **definite absence** (signed site 404) — the identity is retired and the
  request may be re-dispatched once, within budget, under a fresh intent.
- **unverifiable claim, identity conflict, or lease-ownership conflict** — the
  quota is quarantined on the lease that dispatched it; the request is never
  re-sent and an empty local event table is never treated as unspent budget.

Receipt spend always follows the current lease binding: a quota transferred
from `priority` to `waiting` is settled against the waiting lease, and the
priority lease can never reclaim it. robots/scope-blocked URLs never reach the
admission gate. Receipt rows land in `dispatch_intents` / `dispatch_receipts`,
flow into `lease_accounting`, and surface in the report's
"Receipt attestations" section.

## Safety properties

- Only `http://127.0.0.1:<port>/` and `http://[::1]:<port>/` targets are
  registrable or executable. No DNS names, external domains, TLS, proxies, or
  environment-configured sessions are used.
- Every URL is revalidated at execution time. A mutated planned URL is blocked
  before any HTTP call.
- Requests are `GET` only, with `allow_redirects=False`, bounded timeouts, and
  bounded response bodies.
- robots.txt is parsed offline from a registered fixture. A missing or
  unreadable fixture denies every path conservatively.
- The review console has only read routes, binds to loopback, and exposes no
  mutation controls.
- Reports and the review console reference no external assets; the HTML report
  is self-contained.

See [docs/COMPLIANCE.md](docs/COMPLIANCE.md) for the full threat model and
operator checklist.

## Ledger and reports

The SQLite ledger contains `runs`, `site_leases`, `planned_requests`,
`request_events`, `dispatch_intents`, `dispatch_receipts`, `blocked_events`,
`warnings`, and `run_reports`. Unique keys make event insertions and repeated
runs idempotent. Reusing a run ID resumes the run; reusing it with different
config/seed/robots fingerprints is rejected.

Each run writes three reports under `var/reports/<run-id>/`:

- `summary.json` — canonical structured summary;
- `summary.md` — stable Markdown tables;
- `summary.html` — self-contained HTML with inline CSS and no external assets.

The report fingerprint is the SHA-256 of the canonical summary with the report
fingerprint field blank. Golden comparison reports old and new values per JSON
path and the first differing Markdown line.

## Project layout

```text
crawlbudget/           application modules
fixtures/              synthetic sites, robots files, seeds, pages, golden reports
tests/                 unit, integration, resume, safety, and golden tests
docs/                  design, configuration, compliance, and testing notes
var/                   runtime ledger and reports (not source)
```

## Documentation

- [docs/DESIGN.md](docs/DESIGN.md) — architecture, lifecycle, data model, determinism.
- [docs/CONFIG.md](docs/CONFIG.md) — YAML schema, normalization, errors, golden workflow.
- [docs/COMPLIANCE.md](docs/COMPLIANCE.md) — authorization, loopback model, threat model.
- [docs/TESTING.md](docs/TESTING.md) — test strategy, fixtures, commands, coverage map.

## Limitations

- The implementation deliberately supports only plain HTTP to numeric loopback
  hosts. It cannot be pointed at production sites or behind a proxy.
- Robots.txt is read from a local fixture; it is never fetched over the network.
- HTML parsing, link discovery, JavaScript execution, authentication, cookies,
  and POST/PUT/DELETE are out of scope.
- The review console is a local audit view, not a multi-user service. It has no
  authentication because it binds only to loopback and mutates nothing.
- The mock sites use fixed ports from the fixture; tests that need isolation use
  ephemeral ports and hermetic site configuration.

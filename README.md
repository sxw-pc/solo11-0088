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
| `python -m crawlbudget check-golden --run-id ID` | Compare the stored report with `fixtures/expected/`. |

Common run options include `--config`, `--db`, `--reports-dir`, `--run-id`,
`--virtual-start`, and `--dry-run`. Use `--help` on any subcommand for details.

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
`request_events`, `blocked_events`, `warnings`, and `run_reports`. Unique keys
make event insertions and repeated runs idempotent. Reusing a run ID resumes the
run; reusing it with different config/seed/robots fingerprints is rejected.

Each run writes three reports under `var/reports/<run-id>/`:

- `summary.json` — canonical structured summary;
- `summary.md` — stable Markdown tables;
- `summary.html` — self-contained HTML with inline CSS and no external assets.

The report fingerprint is the SHA-256 of the canonical summary with the report
fingerprint field blank. Golden comparison reports old and new values per JSON
path and the first differing Markdown line.

## Subscriber billing (downstream settlement)

`crawlbudget.subscriber_billing` settles synthetic subscribers that share
collection sessions. It is a downstream step: it never issues a GET, never
re-authorizes a request, and never changes lease usage.

```bash
python -m crawlbudget.subscriber_billing --fixture fixtures/subscriber_billing/intake.json
```

- `billing_inputs.py` validates the intake, including the 6-subscriber and
  capacity-3 bounds, duplicate ids, and non-negative directed navigation costs.
- `billing_model.py` computes the exact minimum cost `c(S)` for **every**
  subscriber subset by exhaustive session-partition and directed-route search;
  subset costs are never derived by deleting a stop from the full route.
- `billing_receipts.py` reads executor success events and `billing_verify.py`
  turns each into a billable weight only after re-checking the 2xx success, the
  recorded `response_bytes`/`body_sha256`, the local fixture bytes, the mapped
  page URL, scope, robots, and a granted-and-consumed lease.
- `billing_contracts.py` signs two independent contracts: `byte_share` (floor to
  the cent, then largest fractional remainder, ties by `subscriber_id`) and
  `marginal_share` (exact Shapley value over re-solved subset costs). Both must
  be non-negative, at or below each solo price, and sum exactly to `c(all)`;
  otherwise the contract refuses instead of trimming a bill.

For the shipped fixture, `c(A)=20`, `c(B)=18`, `c(C)=16`, `c(AB)=21`,
`c(AC)=c(BC)=23`, `c(ABC)=37`. The optimal full plan is `D->A->B->D` (21) plus
`D->C->D` (16). `byte_share` bills 19.12 / 9.80 / 8.08; `marginal_share` bills
13 / 12 / 12. The report includes every subset plan, receipt identities, and
refusal evidence. The CLI exits `2` and prints `REFUSED ...` when delivery or a
contract cannot be attested.

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

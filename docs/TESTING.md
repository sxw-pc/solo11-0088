# Testing Guide

## 1. Running the suite

```bash
python -m pip install -r requirements.txt
pytest -q
```

The suite is fully offline. It uses local fixtures, an in-process replay session,
in-memory SQLite, temporary directories, and ephemeral loopback Flask servers.
No external service, DNS name, credential, or network access is required.

For a focused module:

```bash
pytest -q tests/test_robots.py
pytest -q tests/test_executor.py tests/test_resume.py
pytest -q tests/test_golden.py
```

## 2. Test layers

| Layer | Files | What it proves |
| --- | --- | --- |
| Unit | `test_config.py`, `test_robots.py`, `test_scope.py`, `test_budget.py` | Validation aggregation, robots semantics, URL canonicalization/safety, token bucket, lease helpers. |
| Planning | `test_planner.py` | Deterministic ordering, quotas, run limits, robots evidence, disabled job/site, duplicate seeds, missing robots. |
| Storage | `test_storage.py` | Schema behavior, idempotent events, lease usage, quota aggregation, reports, reset, run lifecycle. |
| Execution | `test_executor.py` | Full replayed run, redirects not followed, body cap, retries, preflight block, interruption/resume, hard attempt cap. |
| Orchestration | `test_resume.py`, `test_report.py` | Pipeline resume, fingerprint mismatch, dry run, summary contract, report stability, ledger rebuild. |
| CLI/integration | `test_cli_demo.py` | Real loopback mock servers on ephemeral ports, demo idempotency, plan, inspect, config exit code, golden mismatch, dry run. |
| Safety | `test_safety.py` | `trust_env=False`, fixture-local-only resources, mock prefix routing, loopback redirect blocked, read-only console and loopback bind. |
| Golden | `test_golden.py` | Fixture equality, field-level old/new differences, missing fixture error, CLI success, no absolute paths/local time, type mismatch. |
| Catalog offers | `test_catalog_offers.py` | Verified-body extraction, SHA/outcome gating, snapshot basket quotes, multistore cohort. |
| Promotion acceptance | `test_catalog_promotion.py` | Captured catalog identity, greedy over-budget draft, the 14- and 20-point contract optima, report envelope and CLI. |
| Promotion constraints | `test_promotion_constraints.py` | Free-shipping threshold, vendor eligibility, contribution floor, shared budgets, missing/under/double-committed stock, failed GET and SHA mismatch. |
| Promotion planning | `test_promotion_planning.py` | Uplift/contribution/lexicographic tie-breaks, input reordering, price/stock propagation, bounded 8-cart scale, and an independent brute-force oracle. |

## 3. Deterministic fixtures

The shared demo fixtures exercise all major paths:

- `fixtures/demo_sites.yaml` — three sites with different quotas, run limits,
  retry counts, rates, and response caps.
- `fixtures/robots/*.txt` — exact `crawlbudget` groups, wildcard groups,
  `Allow`/`Disallow`, `*`, `$`, comments, and crawl delays.
- `fixtures/seeds/*.txt` — allowed pages, robots-disallowed pages, duplicate
  seeds, out-of-site paths, non-loopback placeholders, and budget overflows.
- `fixtures/pages/manifest.yaml` plus HTML files — 200, 302, 404, 500,
  fail-first, and oversized-body responses. All HTML is synthetic and local.

`tests/fakes.py` provides a deterministic `ReplaySession` that returns the same
statuses, headers, and bytes as the mock sites without opening sockets. This
keeps unit execution fast and reproducible while integration tests exercise the
real Flask servers.

`tests/conftest.py` provides:

- `demo_config` and `demo_plan` — the canonical loaded config and plan;
- `config_factory` — writes a temporary valid YAML, with fixture paths made
  absolute so it remains valid outside `fixtures/`;
- `copy_fixture_tree` — copies fixtures for fingerprint tests;
- `write_simple_site_config` — a hermetic one-site config used by CLI tests with
  an ephemeral port;
- plan-preparation helpers for in-memory execution tests.

## 4. What the demo proves

The golden demo asserts a fixed, deterministic result:

```text
planned=21 granted=14 executed=14 attempts=17 blocked=9
status counts: 200=10, 302=1, 404=2, 500=4
response bytes: 3189
```

Block reasons include `robots_disallow` (3), `quota_exceeded`,
`run_limit_exceeded`, `scope_non_loopback`, `scope_out_of_site`,
`redirect_non_loopback`, and `response_body_too_large`.

Running the pipeline again with the same run ID must make zero additional HTTP
calls and produce byte-identical JSON and Markdown. `check-golden` compares the
persisted reports with `fixtures/expected/demo_summary.json` and
`fixtures/expected/demo_summary.md`.

## 5. Golden tests

Golden fixtures are intentionally path- and time-independent. They contain the
virtual timestamp `2026-01-15T00:00:00Z` but no `created_at`, wall-clock time, or
absolute filesystem path. If a report-affecting change is intentional and
reviewed, regenerate the fixtures:

```bash
python -m crawlbudget demo --reset
cp var/reports/demo-run/summary.json fixtures/expected/demo_summary.json
cp var/reports/demo-run/summary.md fixtures/expected/demo_summary.md
python -m crawlbudget check-golden --run-id demo-run
```

A mismatch exits `3` and prints `GOLDEN_MISMATCH <path>` with expected and actual
values. Missing fixtures or a missing persisted report raise a `GoldenError`
with an explicit input code.

## 6. Adding tests

- Prefer unit tests for pure decisions and integration tests for lifecycle.
- Use `config_factory` for invalid-config cases so all validation issues can be
  asserted together.
- Use `ReplaySession` for executor cases; use `write_simple_site_config` plus a
  free loopback port when a real server is needed.
- Use `:memory:` storage for fast ledger tests and `tmp_path` for report files.
- Never add a test that contacts a real host, resolves a domain, or requires
  credentials.
- For a new deterministic report field, update the golden fixtures and confirm
  no absolute paths or wall-clock values leak in.
- For a new block/outcome reason, assert it appears in both the ledger and the
  report.

## 7. Interpreting failures

| Symptom | Likely cause |
| --- | --- |
| `CFG_*` in CLI stderr, exit 2 | Invalid YAML or a fixture path resolved unexpectedly. |
| `GOLDEN_MISMATCH` | Report content changed or the stored run predates a code change; inspect the field path. |
| Bind error in CLI test | Ephemeral port was taken between allocation and server start; rerun or use a fresh port. |
| Resume test sees extra calls | Completed planned rows or terminal events were not respected. |
| Report HTML contains `http://` | An external reference was introduced; only loopback strings are allowed. |
| Body-size test sees retained bytes | The body cap check was bypassed or performed after buffering. |

## 8. Size and structure checks

The project enforces two structural constraints:

- no single Python file over 300 lines (enforced by splitting responsibility,
  e.g. promotion money math in `promotion_economics.py` and the combinatorial
  search in `promotion_plan.py`);
- a total Python source plus test line target of 2,500 to 4,500.  The catalog
  feature work grew the tree past the upper bound (the baseline already
  exceeded it at 4,760 lines); new files stay individually under 300 lines and
  the overrun is tracked rather than hidden by deleting required coverage.

Check with:

```bash
wc -l crawlbudget/*.py tests/*.py
```

The split is by responsibility (config, scope, robots, budget, planner, storage,
executor, reporting, golden, demo sites, review console, CLI), not by artificial
line compression. Tests are consolidated with parametrization where cases share
setup, but each safety class retains explicit assertions.

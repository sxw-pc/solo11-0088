# Configuration Reference

Configuration is a YAML document with exactly two top-level lists: `sites` and
`jobs`. Unknown top-level keys are ignored; unknown keys inside a site or job are
also ignored. Missing required fields and invalid values are collected and
reported together as a `ConfigError`, so one invocation shows every problem.

## 1. Site fields

| Field | Type | Required | Constraint |
| --- | --- | --- | --- |
| `site_id` | string | yes | Non-empty, unique across sites. |
| `base_url` | string | yes | `http://127.0.0.1:<port>/<prefix>/` or `http://[::1]:<port>/<prefix>/`; explicit port; normalized path ending in `/`; no query, fragment, or userinfo. |
| `robots_file` | string | yes | Path to a local robots.txt fixture. May be missing if the run is expected to deny all paths conservatively. |
| `daily_quota` | integer | yes | Greater than zero. Counts HTTP attempts per virtual UTC date. |
| `max_requests_per_run` | integer | yes | Greater than zero. Maximum attempts for this site in this run. |
| `max_rps` | number | yes | Greater than zero. The refill interval is `ceil(1000 / max_rps)` milliseconds. |
| `burst` | integer | yes | Greater than zero. Token-bucket capacity. |
| `crawl_delay_ms` | integer | yes | Zero or greater. Hard floor between grants; the robots `Crawl-delay` may raise it. |
| `max_response_bytes` | integer | yes | Greater than zero. Applies to declared and streamed response bodies. |
| `max_retries` | integer | no | Default `0`; zero or greater. Total attempts per request are `max_retries + 1`. |
| `timeout_ms` | integer | no | Default `2000`; greater than zero. Used as the requests timeout in seconds. |
| `enabled` | boolean | no | Default `true`. A disabled site blocks every planned URL with `site_disabled`. |

`base_url` is not merely decorative. Its host and port must match every seed URL,
and its normalized path is the permitted prefix. `http://127.0.0.1:5021/mock-alpha/`
permits `/mock-alpha/...` but not `/mock-beta/...` or `/mock-alpha-other/...`.

## 2. Job fields

| Field | Type | Required | Constraint |
| --- | --- | --- | --- |
| `job_id` | string | yes | Non-empty, unique across jobs. |
| `site_id` | string | yes | Must name a declared site. |
| `priority` | integer | yes | Zero or greater; lower values are planned earlier. Ties break by `job_id`. |
| `seed_file` | string | yes | Must exist and be readable UTF-8 text. |
| `enabled` | boolean | yes | A disabled job still produces a zero-grant lease and `job_disabled` blocks. |

## 3. Example

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

## 4. Path resolution

Relative `robots_file` and `seed_file` paths are resolved in this order:

1. relative to the directory containing the configuration file;
2. relative to the project root (`--project-root`, default: current directory).

The first existing candidate wins. If neither exists, the config-file-relative
path is retained. A missing robots fixture is **not** a configuration error: the
run proceeds and denies all paths with `robots_missing`. A missing seed file is a
configuration error (`CFG_SEED_FILE_MISSING`).

## 5. Seed URLs

Seed files are UTF-8 text with one URL per line. Blank lines and lines whose
first non-whitespace character is `#` are ignored. URLs are otherwise taken
verbatim up to surrounding whitespace.

A seed is accepted only when all of these hold:

- scheme is `http` (case-insensitive);
- there is no username or password;
- the host is exactly `127.0.0.1` or `::1`;
- an explicit port is present and equals the registered site port;
- the normalized path starts with the site's base path;
- the path contains no `.` or `..` dot segments (including `%2e` / `%2E`);
- no query parameter name is in the sensitive-name set (`token`, `api_key`,
  `apikey`, `key`, `secret`, `password`, `auth`, `session`, case-insensitive).

Accepted URLs are canonicalized: the fragment is removed, query pairs are sorted,
host/port formatting is normalized, and the URL is hashed with SHA-256. Duplicate
raw strings within a job and duplicate `(site_id, url_hash)` pairs across jobs are
skipped with warnings.

## 6. Robots fixtures

Robots files are never fetched over the network. The parser supports:

- `User-agent`, `Allow`, `Disallow`, and `Crawl-delay`;
- comments beginning with `#`;
- multiple user-agent lines in one group;
- exact user-agent groups (for `crawlbudget`) with priority over `*`;
- merging of multiple exact groups;
- `*` wildcards and `$` end anchors;
- longest-pattern specificity, with `Allow` winning ties;
- group resets at blank lines or when a new group's user-agent follows rules.

Warnings are recorded for malformed lines, orphan rules, invalid crawl delays,
unknown directives, and missing matching groups. A missing or unreadable robots
fixture yields `robots_missing` warnings and denies every path for that site.
`Crawl-delay` is rounded up to integer milliseconds; the planner uses the larger
of the fixture value and the site's `crawl_delay_ms`.

## 7. Budget semantics

- `daily_quota` is enforced across runs for the same site and virtual UTC date.
  It counts all request attempts (retries included) in `request_events`.
- `--virtual-start` establishes the virtual date and lease expiry. It defaults to
  `2026-01-15T00:00:00Z`.
- `max_requests_per_run` caps attempts for the site in this run. The effective
  attempt limit is the smaller of this cap and remaining daily quota.
- Token-bucket offsets are integer milliseconds from the virtual start. Burst
  requests may share offset `0`. The refill interval is conservative for
  fractional rates.
- Lease expiry is the next UTC midnight after the virtual start, formatted with
  a `Z` suffix.

Examples from the demo: alpha quota `6` and run cap `10` yield 6 grants; beta
quota `10`, run cap `10`, and retries `2` yield 6 grants and 10 attempts; gamma
run cap `2` yields 2 grants.

## 8. Validation error codes

| Code | Meaning |
| --- | --- |
| `CFG_INVALID_YAML` | YAML could not be parsed or the top level is not a mapping. |
| `CFG_MISSING_FIELD` | A required field or non-empty top-level list is absent. |
| `CFG_INVALID_VALUE` | Wrong type, empty string, negative value, or invalid base URL component. |
| `CFG_DUPLICATE_SITE` | Two sites share a `site_id`. |
| `CFG_DUPLICATE_JOB` | Two jobs share a `job_id`. |
| `CFG_INVALID_URL` | Malformed base URL, wrong scheme, missing port, userinfo, query/fragment, or unnormalized path. |
| `CFG_NON_LOOPBACK_TARGET` | Base URL host is not `127.0.0.1` or `::1`. |
| `CFG_NON_POSITIVE_LIMIT` | A quota, size, rate, timeout, or burst is not positive. |
| `CFG_UNKNOWN_SITE` | A job references an undeclared site. |
| `CFG_SEED_FILE_MISSING` | The seed file cannot be found. |

All issues carry a JSON path such as `sites[0].daily_quota` and a message. The
CLI prints each issue as `CODE path: message` and exits `2`.

## 9. Run IDs, resume, and fingerprints

Three input fingerprints are computed at load time:

- `config`: canonical JSON of normalized site/job field values;
- `seeds`: per-job SHA-256 of normalized seed file text;
- `robots`: per-site SHA-256 of normalized robots file text (or `MISSING`).

Reusing a run ID continues that run only when all three fingerprints match the
stored run. Otherwise the pipeline raises an error before execution. A resumed
run keeps its original `created_at` and `started_at`, skips completed planned
requests, and continues retry numbering from persisted events.

Use a new run ID to intentionally change any input artifact.

## 10. Reports and golden workflow

`demo` and `run` write:

```text
var/reports/<run-id>/summary.json
var/reports/<run-id>/summary.md
var/reports/<run-id>/summary.html
```

`summary.json` contains schema version, run metadata, fingerprints, totals,
per-site metrics, per-site leases, status counts, blocked-reason counts, top three
blocked reasons, robots match evidence, and warnings. The report fingerprint is
computed with its own field blank, then inserted.

To intentionally refresh golden fixtures after a deterministic, reviewed change:

```bash
python -m crawlbudget demo --reset
cp var/reports/demo-run/summary.json fixtures/expected/demo_summary.json
cp var/reports/demo-run/summary.md   fixtures/expected/demo_summary.md
python -m crawlbudget check-golden --run-id demo-run
```

`check-golden` compares the persisted JSON summary and Markdown report. A
difference prints one line per field with expected and actual values and exits
`3`. A missing fixture or persisted report is an explicit golden input error.

## 11. CLI overrides

Common run options:

- `--config PATH` — YAML configuration (default `fixtures/demo_sites.yaml`);
- `--db PATH` — ledger database (default `var/crawlbudget.sqlite3`);
- `--reports-dir PATH` — report root (default `var/reports`);
- `--pages-dir PATH` and `--manifest PATH` — synthetic page fixtures;
- `--run-id ID` — stable audit identity; defaults vary by command;
- `--virtual-start ISO8601` — virtual clock origin;
- `--dry-run` — plan, persist, and report without any HTTP request;
- `--no-mock` — do not start built-in mock servers for `run`.

`plan` prints canonical plan JSON to stdout. `serve` accepts `--host` and
`--port` and refuses non-loopback binds. `inspect-run` and `check-golden` accept `--run-id`
(omitted means the newest run) and `--project-root`.

# Compliance and Safety Model

## 1. Authorization model

`crawlbudget` is designed for **authorized, local, synthetic collection only**.
Authorization is expressed structurally rather than by a trust flag:

- The only registrable targets are numeric loopback HTTP endpoints:
  `127.0.0.1` and `::1`.
- Each target must have a robots.txt fixture. A missing or unreadable fixture
  denies all paths.
- Each target has an explicit daily quota, per-run attempt cap, rate limit, and
  response-size cap.
- Execution is GET-only and redirects are never followed.
- Every planned and blocked request is recorded in a local SQLite ledger.

There is no configuration option to disable loopback enforcement, enable TLS,
follow redirects, use a proxy, add credentials, or raise an operator-defined
"bypass" flag. Adding remote hosts requires code changes that would also change
the documented safety properties.

## 2. Offline and loopback guarantees

### Config registration

`base_url` validation requires:

- scheme `http` exactly;
- a host that is exactly `127.0.0.1` or `::1`;
- an explicit port;
- no userinfo, query, or fragment;
- a normalized path ending in `/`.

### Per-URL scope validation

Every seed and every planned URL is validated by `scope_url`:

- whitespace/empty check;
- `http` only;
- no userinfo;
- numeric loopback host only;
- explicit port equal to the site's registered port;
- site base-path prefix;
- dot-segment rejection, including percent-encoded dots;
- sensitive query-name rejection;
- canonicalization (fragment removed, query sorted, explicit host/port);
- SHA-256 URL identity.

A URL that fails any check is blocked with a stable `scope_*` reason. Scope is
revalidated immediately before execution; a URL mutated after planning is blocked
with `EXEC_PREFLIGHT_BLOCKED` and no request is made.

### HTTP session behavior

The executor creates a `requests.Session` with `trust_env = False`, so proxy
environment variables and netrc files are not consulted. Requests use:

```python
session.get(url, timeout=site.timeout_ms / 1000.0, allow_redirects=False,
            stream=True, headers={"User-Agent": "crawlbudget/1.0 (+local-only)"})
```

Only numeric loopback URLs that already passed scope validation are passed to
the session. The implementation does not resolve arbitrary hostnames. The
reserved name `example.invalid` appears only in fixtures and negative tests and
is never requested.

## 3. robots.txt handling

- Robots content is loaded from the registered local fixture only.
- An absent or unreadable fixture is **fail-closed**: every path is denied with
  `robots_missing`; the run still produces audit records.
- Exact `crawlbudget` groups take priority over `*` groups.
- `*` and `$` are supported; longest specificity wins; `Allow` wins equal ties.
- `Crawl-delay` is rounded up to milliseconds and acts as a hard scheduling
  floor together with the site's `crawl_delay_ms`.
- Malformed or unsupported lines produce warnings rather than silently changing
  authorization.

## 4. Budget enforcement

Budgets are enforced in planning and again at execution:

- **Daily quota**: counts every persisted HTTP attempt for the site on the same
  virtual UTC date, across runs. Retries count.
- **Per-run limit**: caps attempts for the site and run. Remaining attempts are
  recomputed from the ledger before every try, so interruption/resume cannot
  overshoot.
- **Rate**: integer token bucket, `ceil(1000 / max_rps)` refill interval, with a
  crawl-delay floor.
- **Response size**: declared `Content-Length` and streamed bytes are both
  checked; oversized bodies are discarded and blocked.
- **Retries**: only a fixed set of transient statuses and timeout/transport
  failures retry, bounded by `max_retries`.
- **Redirects**: `3xx` is terminal and never followed. A non-loopback `Location`
  is labeled `redirect_non_loopback`.

## 5. Auditability

The ledger records:

- run metadata and input fingerprints;
- deterministic leases with grant/use/remaining counts and expiry;
- every planned request with allow/block status and evidence;
- every HTTP attempt with status, outcome, bytes, body SHA-256, and virtual time;
- every planning or execution block with stage and reason;
- warnings with structured context;
- immutable report bodies and their fingerprint.

Uniqueness constraints make retries, repeated planning, and pipeline re-entry
idempotent. `inspect-run` and the review console expose ledger state without
mutation routes. The console sends a restrictive Content-Security-Policy,
`nosniff`, and `no-store` headers; it binds only to loopback and exposes no
forms, buttons, or state-changing endpoints.

## 6. Data handling

- All fixtures, seed data, page bodies, and site IDs are synthetic.
- No credentials, cookies, tokens, personal data, or real domains are used.
- Sensitive query parameter names are rejected before any request.
- Response bodies are not persisted; only byte count, content type, and SHA-256
  are recorded. The demo's HTML fixtures are static local artifacts.
- Reports contain no absolute paths, wall-clock local timestamps, or secrets.
- The SQLite database is local runtime state and is ignored by version control.

## 7. Threat model

| Risk | Control |
| --- | --- |
| Accidental request to a real host | Config and per-URL loopback enforcement; numeric hosts only; no redirect following. |
| DNS-based exfiltration | No hostname resolution for targets; loopback literals required. |
| Proxy/env redirection | `trust_env = False`; no proxy configuration path. |
| robots bypass | Fixture-only policy; missing fixture fails closed; exact/`*` group rules; evidenced decisions. |
| Runaway request volume | Daily quota, per-run attempt cap, token bucket, crawl-delay floor, retry cap. |
| Resource exhaustion via large bodies | Declared and streaming byte caps; body discarded when over limit. |
| Redirect to an external target | Redirects never followed; target host classified and blocked. |
| Duplicate or replayed work | Unique `(run, site, url_hash, attempt)` events; completed planned rows skipped; fingerprint-checked resume. |
| Audit tampering through resubmission | Upserts preserve execution state; blocks/warnings use insert-or-ignore; reports persisted with fingerprint. |
| Console mutation or injection | No mutation routes; Jinja not used for ledger content; HTML escaped manually; CSP. |
| Credential leakage | No credential fields; sensitive query names rejected; no auth support. |

Residual risks are limited to the local machine and its operator. The system is
not a sandbox: a malicious local process could modify the SQLite file, the Python
installation, or the mock servers. Integrity beyond that boundary is out of
scope.

## 8. Explicit prohibitions

The codebase intentionally contains no support for:

- non-loopback targets, DNS names, public IPs, or TLS;
- POST/PUT/PATCH/DELETE or other non-GET methods;
- redirect following;
- authentication, cookies, API keys, or secrets;
- proxy configuration or environment-derived sessions;
- bypass or "unsafe" configuration switches;
- remote robots fetching, remote page discovery, or JavaScript execution;
- distributed workers, queues, or external databases;
- mutation endpoints in the review console.

## 9. Operator checklist

Before running:

1. Confirm the YAML targets are synthetic loopback sites.
2. Confirm robots fixtures are the intended ones.
3. Confirm quotas, run caps, rates, and response caps are acceptable.
4. Use a unique run ID for changed inputs; reuse a run ID only to resume.
5. Prefer `--dry-run` to review the plan first.

After running:

1. Check `inspect-run` for blocked reasons and warnings.
2. Review `summary.md` or the review console.
3. Run `check-golden` when validating deterministic demo behavior.
4. Preserve the ledger and reports for audit; do not edit them by hand.

## 10. Demo-specific notes

The demo starts Flask mock servers on the configured loopback ports, executes the
planned requests, and stops the servers in a `finally` block. The demo reset
removes ledger tables and report subdirectories but refuses to treat the project
root as a reports directory. Ports `5021`–`5023` are configuration defaults, not
external services.

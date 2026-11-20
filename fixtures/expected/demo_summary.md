# crawlbudget run demo-run

- Status: `completed`
- Dry run: `false`
- Virtual start: `2026-01-15T00:00:00Z`
- Virtual finish: `2026-01-15T00:00:01Z`
- Virtual date: `2026-01-15`

## Totals

| Metric | Value |
|---|---|
| Sites | 3 |
| Planned requests | 21 |
| Granted requests | 14 |
| Executed requests | 14 |
| HTTP attempts | 17 |
| Blocked events | 9 |
| Response bytes | 3189 |

## Fingerprints

| Artifact | SHA-256 |
|---|---|
| Configuration | eebb9bbc3e093b880249302d33e6626cd522b8ebfebd53e655ee43e0a49b2f50 |
| Seed manifests | 2da5a7a22c61ab1ea1797d44bf659528abb455c5852ee10854d3f7d342930bf8 |
| Robots fixtures | 756450fe9483d002a312682f4352106575e6a76483b986a8acd53ee49e32071d |
| Report | bf0dd06d6bb9b6ffb1870cd450d40e57f4b197dc1605669d859ee3c7d5b50544 |

## Sites

| Site | Planned | Granted | Executed | Blocked | HTTP attempts | Bytes | Quota used before |
|---|---|---|---|---|---|---|---|
| mock-alpha | 10 | 6 | 6 | 5 | 6 | 1832 | 0 |
| mock-beta | 7 | 6 | 6 | 2 | 9 | 1141 | 0 |
| mock-gamma | 4 | 2 | 2 | 2 | 2 | 216 | 0 |

## Leases

| Lease | Site | Job | Granted | Used | Remaining | Expires | Reason |
|---|---|---|---|---|---|---|---|
| lease-e7b30fe27e6b7949 | mock-alpha | job-alpha-main | 6 | 6 | 0 | 2026-01-16T00:00:00Z | granted |
| lease-9151a68e609120cf | mock-beta | job-beta-main | 6 | 6 | 0 | 2026-01-16T00:00:00Z | granted |
| lease-7b54871ef063903c | mock-gamma | job-gamma-main | 2 | 2 | 0 | 2026-01-16T00:00:00Z | granted |

## Request status distribution

| Status | Attempts |
|---|---|
| 200 | 10 |
| 302 | 1 |
| 404 | 2 |
| 500 | 4 |

## Blocked reasons

| Reason | Count |
|---|---|
| quota_exceeded | 1 |
| redirect_non_loopback | 1 |
| response_body_too_large | 1 |
| robots_disallow | 3 |
| run_limit_exceeded | 1 |
| scope_non_loopback | 1 |
| scope_out_of_site | 1 |

## Receipt verification

| Metric | Value |
|---|---|
| Intents settled | 17 |
| Receipt-verified, response undelivered | 0 |
| Recovered deliveries | 0 |
| Quarantined receipts | 0 |

## Robots rule matches

| Site | Path | Decision | Rule | Pattern | Group |
|---|---|---|---|---|---|
| mock-alpha | /mock-alpha/private/public-note.html | allow | R002 | /mock-alpha/private/public-note.html$ | crawlbudget |
| mock-alpha | /mock-alpha/private/secret.html | disallow | R001 | /mock-alpha/private/ | crawlbudget |
| mock-beta | /mock-beta/hidden/allowed/note.html | allow | R002 | /mock-beta/hidden/allowed/* | crawlbudget |
| mock-beta | /mock-beta/hidden/secret.html | disallow | R001 | /mock-beta/hidden/* | crawlbudget |
| mock-gamma | /mock-gamma/private/hidden.html | disallow | R001 | /mock-gamma/private/ | crawlbudget |

## Warnings

_No warnings._

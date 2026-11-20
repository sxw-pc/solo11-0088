"""SQLite schema for the crawlbudget ledger."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL, virtual_date TEXT NOT NULL, dry_run INTEGER NOT NULL,
  status TEXT NOT NULL, config_fingerprint TEXT NOT NULL,
  seeds_fingerprint TEXT NOT NULL, robots_fingerprint TEXT NOT NULL,
  report_fingerprint TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS site_leases (
  run_id TEXT NOT NULL, lease_id TEXT NOT NULL, site_id TEXT NOT NULL,
  job_id TEXT NOT NULL, granted_requests INTEGER NOT NULL,
  used_requests INTEGER NOT NULL, remaining_requests INTEGER NOT NULL,
  expires_at TEXT NOT NULL, reason TEXT NOT NULL,
  PRIMARY KEY (run_id, lease_id)
);
CREATE TABLE IF NOT EXISTS planned_requests (
  run_id TEXT NOT NULL, site_id TEXT NOT NULL, job_id TEXT NOT NULL,
  request_order INTEGER NOT NULL, url TEXT NOT NULL, url_hash TEXT NOT NULL,
  lease_id TEXT NOT NULL, allowed INTEGER NOT NULL, block_reason TEXT,
  block_evidence_json TEXT NOT NULL, virtual_offset_ms INTEGER,
  robots_rule_id TEXT, robots_evidence_json TEXT NOT NULL, seeded_url TEXT NOT NULL,
  completed INTEGER NOT NULL DEFAULT 0, final_outcome TEXT, attempt INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (run_id, site_id, url_hash, attempt)
);
CREATE TABLE IF NOT EXISTS request_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  site_id TEXT NOT NULL, job_id TEXT NOT NULL, url_hash TEXT NOT NULL,
  attempt INTEGER NOT NULL, url TEXT NOT NULL, status_code INTEGER,
  outcome TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, response_bytes INTEGER NOT NULL,
  content_type TEXT NOT NULL, body_sha256 TEXT NOT NULL, error_code TEXT,
  evidence_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
  UNIQUE (run_id, site_id, url_hash, attempt)
);
CREATE TABLE IF NOT EXISTS blocked_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  site_id TEXT NOT NULL, job_id TEXT NOT NULL, url_hash TEXT NOT NULL,
  url TEXT NOT NULL, stage TEXT NOT NULL, reason TEXT NOT NULL,
  evidence_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
  UNIQUE (run_id, site_id, url_hash, stage, reason)
);
CREATE TABLE IF NOT EXISTS warnings (
  warning_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  code TEXT NOT NULL, message TEXT NOT NULL, context_json TEXT NOT NULL,
  recorded_at TEXT NOT NULL, UNIQUE (run_id, code, message, context_json)
);
CREATE TABLE IF NOT EXISTS run_reports (
  run_id TEXT PRIMARY KEY, summary_json TEXT NOT NULL, summary_md TEXT NOT NULL,
  summary_html TEXT NOT NULL, report_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_receipts (
  receipt_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  site_id TEXT NOT NULL, url TEXT NOT NULL, request_headers_json TEXT NOT NULL,
  status INTEGER NOT NULL, etag TEXT NOT NULL, vary TEXT NOT NULL,
  body_sha256 TEXT, body_path TEXT, recorded_at TEXT NOT NULL,
  UNIQUE (run_id, site_id, url, status, recorded_at, request_headers_json)
);
CREATE INDEX IF NOT EXISTS idx_receipts_run ON capture_receipts(run_id, site_id, url);
CREATE INDEX IF NOT EXISTS idx_events_run_site ON request_events(run_id, site_id);
CREATE INDEX IF NOT EXISTS idx_planned_run ON planned_requests(run_id, request_order);
CREATE INDEX IF NOT EXISTS idx_blocked_run ON blocked_events(run_id);
"""

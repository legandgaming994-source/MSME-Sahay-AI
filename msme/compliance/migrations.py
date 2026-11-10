"""Schema for the compliance workspace (registered as migration 41 in lambda_erp.database).

Portable DDL (TEXT/INTEGER only). Integrity model:
  * "MSME Compliance Task" is unique per (company, fy, form, period): generation is idempotent.
  * "MSME Workpaper" rows are immutable once submitted; a revision is a new version row.
  * "MSME Audit Event" is a hash chain (seq UNIQUE, each hash covers the previous one).
"""

from __future__ import annotations

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS "MSME Compliance Profile" (
        company TEXT PRIMARY KEY,
        filer_type TEXT NOT NULL,
        state_category TEXT,
        gstin TEXT,
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS "MSME Deadline Override" (
        id TEXT PRIMARY KEY,
        company TEXT NOT NULL,
        form TEXT NOT NULL,
        period TEXT NOT NULL,
        due_date TEXT NOT NULL,
        source TEXT NOT NULL,
        entered_by TEXT NOT NULL,
        entered_at TEXT NOT NULL
    )""",
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_msme_override_key ON "MSME Deadline Override" (company, form, period)',
    """CREATE TABLE IF NOT EXISTS "MSME Compliance Task" (
        id TEXT PRIMARY KEY,
        company TEXT NOT NULL,
        fy_start_year INTEGER NOT NULL,
        form TEXT NOT NULL,
        period TEXT NOT NULL,
        due_date TEXT NOT NULL,
        basis TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        optional INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'open',
        filing_reference TEXT,
        filed_on TEXT,
        filing_note TEXT,
        filing_recorded_by TEXT,
        filing_recorded_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_msme_task_key ON "MSME Compliance Task" (company, fy_start_year, form, period)',
    'CREATE INDEX IF NOT EXISTS idx_msme_task_due ON "MSME Compliance Task" (company, status, due_date)',
    """CREATE TABLE IF NOT EXISTS "MSME Workpaper" (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        content_json TEXT NOT NULL,
        figures_digest TEXT,
        contributors_json TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        submitted_by TEXT,
        submitted_at TEXT,
        decided_by TEXT,
        decided_at TEXT,
        decision_note TEXT,
        FOREIGN KEY (task_id) REFERENCES "MSME Compliance Task"(id)
    )""",
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_msme_workpaper_version ON "MSME Workpaper" (task_id, version)',
    """CREATE TABLE IF NOT EXISTS "MSME Audit Event" (
        id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        ts TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        detail TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        hash TEXT NOT NULL
    )""",
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_msme_audit_seq ON "MSME Audit Event" (seq)',
    'CREATE INDEX IF NOT EXISTS idx_msme_audit_entity ON "MSME Audit Event" (entity_id)',
)


def migrate_compliance_workspace(db) -> None:
    for statement in STATEMENTS:
        db.sql(statement)
    db.conn.commit()

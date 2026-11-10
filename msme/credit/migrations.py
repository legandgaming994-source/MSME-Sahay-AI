"""Schema for credit-readiness and scheme navigation (migration 42).

  * "MSME Credit Fact" is append-only: a new value is a new row; the newest row per (company, kind, fact_key)
    is current and the rest are history. Every row records its source and who entered it.
  * "MSME Scheme Assessment" rows are immutable snapshots of a rule evaluation (rule version + facts digest).
"""

from __future__ import annotations

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS "MSME Credit Fact" (
        id TEXT PRIMARY KEY,
        company TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'fact',
        fact_key TEXT NOT NULL,
        value_json TEXT NOT NULL,
        source TEXT NOT NULL,
        evidence_document_id TEXT,
        entered_by TEXT NOT NULL,
        entered_at TEXT NOT NULL
    )""",
    'CREATE INDEX IF NOT EXISTS idx_msme_credit_fact_key ON "MSME Credit Fact" (company, kind, fact_key, id)',
    """CREATE TABLE IF NOT EXISTS "MSME Scheme Assessment" (
        id TEXT PRIMARY KEY,
        company TEXT NOT NULL,
        scheme_id TEXT NOT NULL,
        as_of TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        status TEXT NOT NULL,
        result_json TEXT NOT NULL,
        facts_digest TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    'CREATE INDEX IF NOT EXISTS idx_msme_scheme_assessment ON "MSME Scheme Assessment" (company, scheme_id, id)',
)


def migrate_credit(db) -> None:
    for statement in STATEMENTS:
        db.sql(statement)
    db.conn.commit()

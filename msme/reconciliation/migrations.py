"""Schema for reconciliation matches (migration 43).

Integrity (enforced by the database, not just the application):
  * at most ONE confirmed match per document and per bank transaction (partial unique indexes);
  * a (document, transaction) pair exists at most once across all statuses, so a rejected pair is remembered.
"""

from __future__ import annotations

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS "MSME Reconciliation Match" (
        id TEXT PRIMARY KEY,
        company TEXT NOT NULL,
        document_id TEXT NOT NULL,
        bank_transaction TEXT NOT NULL,
        status TEXT NOT NULL,
        classification TEXT NOT NULL,
        score INTEGER,
        basis_json TEXT NOT NULL,
        difference TEXT,
        algorithm_version TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        decided_by TEXT,
        decided_at TEXT,
        note TEXT,
        FOREIGN KEY (document_id) REFERENCES "MSME Document"(id),
        FOREIGN KEY (bank_transaction) REFERENCES "Bank Transaction"(name)
    )""",
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_msme_match_pair ON "MSME Reconciliation Match" (document_id, bank_transaction)',
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_msme_match_doc_confirmed ON "MSME Reconciliation Match" (document_id) '
    "WHERE status = 'confirmed'",
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_msme_match_txn_confirmed ON "MSME Reconciliation Match" (bank_transaction) '
    "WHERE status = 'confirmed'",
    'CREATE INDEX IF NOT EXISTS idx_msme_match_company_status ON "MSME Reconciliation Match" (company, status)',
)


def migrate_reconciliation(db) -> None:
    for statement in STATEMENTS:
        db.sql(statement)
    db.conn.commit()

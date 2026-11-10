"""Schema for document intelligence (registered as migration 40 in lambda_erp.database).

Portable DDL (TEXT/INTEGER/REAL only) so it runs unchanged on SQLite and PostgreSQL.

Integrity model
  * "MSME Document".sha256 is UNIQUE: re-uploading identical bytes can never create a second record.
  * "MSME Extracted Field".original_value is written once (first machine reading) and never updated.
  * "MSME Field Correction" is append-only (the application never updates or deletes rows).
  * An issue is never silently cleared: a reviewer may ACKNOWLEDGE it (who/why/when kept on the row); the
    acknowledgement is dropped automatically if the underlying values or message change.
"""

from __future__ import annotations

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS "MSME Document" (
        id TEXT PRIMARY KEY,
        filename TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        storage_key TEXT NOT NULL,
        uploaded_by TEXT,
        status TEXT NOT NULL DEFAULT 'uploaded',
        document_type TEXT,
        business_key TEXT,
        duplicate_of TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        error_code TEXT,
        error_message TEXT,
        extractor TEXT,
        extractor_version TEXT,
        approved_by TEXT,
        approved_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        processed_at TEXT
    )""",
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_msme_document_sha256 ON "MSME Document" (sha256)',
    'CREATE INDEX IF NOT EXISTS idx_msme_document_business_key ON "MSME Document" (business_key)',
    'CREATE INDEX IF NOT EXISTS idx_msme_document_status ON "MSME Document" (status, updated_at)',
    """CREATE TABLE IF NOT EXISTS "MSME Extracted Field" (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        field_path TEXT NOT NULL,
        original_value TEXT,
        machine_value TEXT,
        current_value TEXT,
        confidence REAL,
        page INTEGER,
        quote TEXT,
        bbox_json TEXT,
        location_status TEXT NOT NULL DEFAULT 'none',
        corrected INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (document_id) REFERENCES "MSME Document"(id)
    )""",
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_msme_field_doc_path ON "MSME Extracted Field" (document_id, field_path)',
    """CREATE TABLE IF NOT EXISTS "MSME Field Correction" (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        field_path TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        reason TEXT NOT NULL,
        corrected_by TEXT NOT NULL,
        corrected_at TEXT NOT NULL,
        FOREIGN KEY (document_id) REFERENCES "MSME Document"(id)
    )""",
    'CREATE INDEX IF NOT EXISTS idx_msme_correction_doc ON "MSME Field Correction" (document_id, field_path)',
    """CREATE TABLE IF NOT EXISTS "MSME Validation Issue" (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        code TEXT NOT NULL,
        severity TEXT NOT NULL,
        message TEXT NOT NULL,
        field_path TEXT,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        acknowledged_by TEXT,
        acknowledged_at TEXT,
        acknowledgement_reason TEXT,
        FOREIGN KEY (document_id) REFERENCES "MSME Document"(id)
    )""",
    'CREATE INDEX IF NOT EXISTS idx_msme_issue_doc ON "MSME Validation Issue" (document_id)',
)


def migrate_document_intelligence(db) -> None:
    for statement in STATEMENTS:
        db.sql(statement)
    db.conn.commit()

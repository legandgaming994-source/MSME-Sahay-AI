# Compliance workspace (C-1, C-2)

Code: `msme/compliance/` (service, figures, audit chain, reports, migration 41) and `api/routers/msme_compliance.py`.
**Preparation only.** Nothing here files a return, calls a government system, or claims a filing happened.

## Lifecycle
```
profile (filer type, QRMP category, GSTIN) -> generate tasks for a financial year (from the versioned rule engine)
task: open -> in_preparation -> in_review -> approved_for_filing -> filing_recorded
        workpaper: draft -> submitted -> approved | rejected      (immutable once submitted; revisions are new versions)
```
- `approved_for_filing` is an INTERNAL approval: "ready for the responsible person to file on the portal".
- `filing_recorded` means **the user typed in their own portal reference and date**. It is labelled "Recorded by the user; not
  verified against the GST portal" in the API, the PDF and the audit event (`verified_against_portal: false`).
- A new workpaper revision after approval supersedes it and returns the task to `in_preparation`.
- `overdue` / `days_to_due` are derived from the clock, never stored.

## Deadlines
Generated from `msme/rules/data/gst_calendar.json` (effective-dated, with source and verification status; see `RULE_SOURCES.md`).
Generation is idempotent (unique per company, year, form, period) and updates dates only when the rule/override changed.
Government extensions are **not** applied automatically: a manager enters them as an override with a cited notification
(5-300 chars); overrides are marked `USER_SUPPLIED` and stamp the task basis `EXTENSION_OVERRIDE`. Holiday shifting is not modelled.
QRMP filers must state their GSTR-3B state category (X/Y); it is never inferred from the state.

## Workpaper figures
Computed only from **approved** documents (Pass 4) whose issue date falls in the period:
outward if the company GSTIN is the seller, inward if it is the buyer. Not silently summed - listed under `attention`:
`unclassified` (direction unknown, including every document when the profile has no GSTIN), `credit_notes` (not netted),
`excluded_duplicates`, `non_inr`, `incomplete`. A digest of the included documents and their amounts is stored with the workpaper;
if documents change after submission/approval, approval and filing-recording are blocked (`figures_stale`).
Not computed: ITC eligibility, reverse charge, HSN splits, CGST/SGST/IGST allocation, tax payable. The checklist is
**internal guidance** (`msme/compliance/data/checklists.json`), not an official list.

## Maker-checker
The approver may not have created or edited the workpaper. Single-person deployments can set
`MSME_REQUIRE_DISTINCT_APPROVER=0`; every approval then records `"maker_checker":"waived_by_policy"` in the audit event.
(The setting is an environment variable, so changing it is not itself audited by the application; protect deployment config.)

## Audit trail
`MSME Audit Event` is a SHA-256 hash chain written in the same transaction as the change. `GET /audit/verify` recomputes the
chain and reports the first broken event. Detected: edited, deleted, inserted or reordered events (tested).
**Limit:** the chain is in the same database, so an actor who can rewrite the entire table consistently can forge a valid chain.
Record the head hash (shown on every report and in `/audit/verify`) outside the system to detect that. Appends are a
serialisation point: on PostgreSQL concurrent appends conflict on the unique `seq` and are retried (67 retries across 24 threads
in a stress run, all events landed); on SQLite the write lock serialises them (0 retries). Extreme contention can return HTTP 503.
Only compliance events are chained today; document approvals/corrections keep their own tables (append-only by convention).

## Reports
`GET /workpapers/{id}/report.pdf` (banner "DRAFT PREPARATION WORKPAPER - NOT A FILED RETURN", checklist, figures, documents,
approval trail, any user-recorded filing as unverified, rule version/source, audit head) and `GET /calendar.csv`.
User text is HTML-escaped, the PDF renderer is blocked from fetching any URL, and CSV text cells are neutralised against
formula injection.

## API (/api/msme/compliance)
| Method / path | Role |
|---|---|
| GET `/profile`, `/tasks`, `/tasks/{id}`, `/upcoming`, `/calendar.csv`, `/workpapers/{id}`, `/workpapers/{id}/report.pdf`, `/audit`, `/audit/verify` | viewer |
| PUT `/profile`; POST `/overrides`, `/tasks/generate`, `/tasks/{id}/workpapers`, `/tasks/{id}/filing`; PUT `/workpapers/{id}`; POST `/workpapers/{id}/submit`, `/approve`, `/reject` | manager (not demo `public_manager`) |

## Verified (tests)
`tests/test_msme_compliance.py` (service, 70 checks incl. real approved documents from the Pass 4 service, concurrent approvals,
concurrent audit appends, tamper detection, PDF text extraction) and `tests/test_msme_compliance_api.py` (roles over HTTP),
on file SQLite and PostgreSQL 16 (backend asserted). Mutation checks: removing maker-checker, the stale-figure guard, the
"not verified" audit flag or the PDF label each fails a test. The authorization matrix and SQL-injection fuzzing cover the new routes.

## NOT verified / not built
- Deadline rule data is secondary-sourced (see `RULE_SOURCES.md`); primary CBIC/gazette confirmation outstanding.
- No notifications are sent (email/SMS/push): `/upcoming` is a pull endpoint for a UI.
- No UI, no GSTN/portal integration of any kind, no tax computation, no ITC logic.
- Only GST return deadlines are modelled; other statutory calendars (TDS, MSME-1, ROC, income tax) are not.
- Cross-document reconciliation (purchase/sales/payment matching) is not built; figures are per-period sums of approved documents.
- Audit anchoring outside the database is manual; no scheduled export.

# Reconciliation (F-1) and turnover cross-check (R-5)

Code: `msme/reconciliation/` (matching, service, migration 43), `msme/credit/turnover.py`, `api/routers/msme_reconciliation.py`.
Suggestions are **heuristic until a person confirms them**. Bank data is **read-only** here.

## What is matched
Approved, non-duplicate, INR **invoices** (direction from the company GSTIN, as in the workpaper figures) against booked,
non-reversed, INR **bank transactions** of that company's bank accounts: outward invoices <-> deposits, inward <-> withdrawals.
Excluded and listed instead (never silently dropped): duplicates, credit notes, non-INR, unclassified documents; informational,
reversed and foreign-currency transactions. The ERP's own bank reconciliation fields (`allocated_amount`, `status`) are never
written: a full before/after snapshot test proves no `Bank Transaction` row changes.

## Scoring (match-v1, fully explained)
Each suggestion lists named reasons with points; the score is their sum (max 100).

| Reason | Points | Rule |
|---|---|---|
| AMOUNT_EXACT | 50 | payment equals the invoice total within 0.01 |
| AMOUNT_SHORTFALL | 20 | paid up to 10% less: flagged as a *possible* deduction at source or part payment; no rate is assumed |
| REFERENCE | 35 | normalised invoice number (>= 4 chars; numeric-only >= 5) appears in the narration/remittance/reference |
| NAME | up to 15 | counterparty name similarity >= 50% (company-form words ignored) |
| TIMING | 5 | payment between issue date and due date (+15 days) |

Hard filters: direction/flow compatibility and payment date from 30 days before to 180 days after the invoice date.
Classes: STRONG >= 85, PROBABLE >= 60, POSSIBLE >= 40 (lower is discarded). Assignment is greedy one-to-one with ties broken by id,
so runs are reproducible; a pair is flagged `ambiguous` when an identical-score rival exists (e.g. two identical invoices and one
payment): the engine cannot choose and a person must. Alternatives (up to 2 per invoice) are kept.

## Review lifecycle
`suggested | alternative -> confirmed | rejected`; `confirmed -> rejected` via unmatch (with reason); competing live matches become
`superseded` when one is confirmed. A **rejected pair is never re-suggested**. A person can create a **manual** match (classified
MANUAL, with the amount difference recorded) when the engine missed it. Match ids are deterministic per (company, document,
transaction), so a reviewer's open screen survives someone else re-running the matcher, and a manual match of a previously
rejected pair reuses that row (the audit trail keeps both events).
Database guarantees: partial unique indexes allow at most **one confirmed match per document and per payment**, enforced even
against direct SQL (tested). Every action is a hash-chained audit event written in the same transaction.

## Queues and summary
`GET /unmatched/documents|transactions` (oldest first, with age in days) and `GET /summary` (matched/unmatched counts and rupee
values by direction, match counts by status, plus the attention lists).

## Concurrency (found and fixed with PostgreSQL)
Two people confirming competing matches for one payment deadlocked on PostgreSQL (`DeadlockDetected`); the shared transaction
helper only retried unique violations, so the loser crashed. It now also retries deadlocks and serialization failures
(`msme/compliance/audit.py: is_retryable`, used by compliance, credit and reconciliation). Measured over 25 races per backend
(`tests/stress_msme_confirm_race.py`): without the fix 14/25 crashed on PostgreSQL; with it 0/25 (16 retries), and SQLite needs none.

## Turnover cross-check (R-5): `GET /api/msme/credit/turnover-check`
Compares the declared `turnover_inr` with approved outward invoices (12 complete months before the current month) and bank credits
(trailing 365 days). Flags are **HEURISTIC** only: `DOCUMENTED_EXCEEDS_DECLARED`, `LOW_DOCUMENT_COVERAGE` (< 50%),
`LOW_BANK_CREDITS` (< 50%), plus an INFO `NO_GSTIN`. Three caveats always accompany the result (period of the declaration is
unknown, exempt/export/credit notes not distinguished, bank credits include loans and transfers). It is a prompt to look,
never an accusation or a determination.

## Verified
`tests/test_msme_matching.py` (14 pure tests), `tests/test_msme_audit_retry.py` (4), `tests/test_msme_reconciliation.py`
(50 checks incl. real approved documents, real bank rows, the DB constraint, concurrent confirmation, bank immutability,
turnover totals) and `tests/test_msme_reconciliation_api.py` (33 checks), on file SQLite and PostgreSQL 16 (backend asserted).
Mutation checks (bytecode cleared each run): bank write, missing unique index, rejected-pair filter, severity change, the
already-matched pre-check, matching direction/reference/shortfall rules. Two mutants first survived; tests were added for both.

## NOT verified / not built
- **One invoice to one payment only.** Split payments, one payment for several invoices, advances and refunds are not matched
  (they surface in the unmatched queues).
- A shortfall is flagged, never explained: no TDS/TCS rate logic, no Form 26AS/AIS reconciliation.
- INR only; no foreign-currency or FX handling.
- No link to the ERP's ledger or its bank reconciliation (by design, to avoid dual writers); confirming a match does not post entries.
- Scoring weights and thresholds are reasoned defaults, **not tuned on real data**. No accuracy numbers exist for real statements.
- Narration parsing is substring-based; UPI/NEFT reference formats beyond invoice-number containment are not parsed.
- No UI, no bulk-confirm, no scheduled runs.

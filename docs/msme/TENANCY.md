# Tenancy and isolation decision (D6)

Evidence date: 2026-10-17. Tests: `tests/test_authz_matrix.py` (Parts A-E), `tests/test_sql_injection_probes.py`.

## What upstream provides (verified)
- Roles admin > manager > viewer, enforced server-side per route; demotion and disabling take effect on live sessions.
- Per-user ownership for chat sessions, chat attachments and API keys (cross-user read/modify/delete attempts fail; admin may
  download any attachment, by design). API keys cannot out-rank their owner.
- Cookie: HttpOnly, SameSite=Lax, Secure behind an HTTPS proxy.
- Anonymous reach limited to login/register/health/status/OAuth discovery (+ MCP dynamic client registration when enabled).

## What it does NOT provide (verified)
**No row-level tenancy.** `User` has no company/tenant link. Any authenticated viewer can read every company's masters and
ledgers in the database, including `tax_id`, `iban` and accounts (characterisation check in Part E).
Therefore `Company` is a business dimension, **not a security boundary**.

## Decision D6 (hard to reverse; flagged)
**The deployment (application instance + its own database) is the tenant boundary for v1.**
One MSME/organisation per deployment. Users within a deployment are trusted to see all of that deployment's companies.

Rationale
1. ~130 operations, LLM chat tools, MCP, semantic reports and CAMT import all read business tables. A retrofit of row-level
   scoping touches every one of them; one missed path is a silent cross-tenant leak and the existing suite would not catch it.
2. A deployment boundary is enforceable at the infrastructure layer and is testable with the matrix above.
3. Matches the MSME use case (each business owns its books). A CA-firm "many clients per login" model is a v2 feature.

Trade-offs accepted: higher per-tenant infrastructure cost; no cross-client dashboard; onboarding needs provisioning.

## If row-level tenancy is later required (scope estimate, not started)
User->Company membership table; a single scoped-query helper used by documents, masters, reports, analytics, bank
reconciliation/statements, search, chat tools, MCP and PDF generation; matrix extended to two-tenant fixtures for every route;
fail-closed default (no membership = no data). Do not ship partial scoping.

## Residual risks
- R-1 MCP dynamic client registration is anonymous (spec-required, bounded). Anonymous callers can exhaust the 1000/hour
  cap: rate-limit at the edge.
- R-2 Denied session access returns HTTP 200 with a "not found" body (`api/chat.py`); no data leak, but clients may mistake it for
  success. Recommend 404 (breaking change: frontend must be checked).
- R-3 Report-draft ownership and PDF artifact access not covered by cross-user tests yet.
- R-4 WebSocket/stream endpoints (if any) are outside the OpenAPI inventory.

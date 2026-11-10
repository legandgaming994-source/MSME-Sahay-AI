# Delivery phases

Each phase shipped as its own zip with a manifest (`history/PASSn_MANIFEST.md`); this repository is the sum of all of them.

| Phase | Delivered | Notable finding or fix |
|---|---|---|
| 0 Baseline | Verified baseline of the supplied tree (upstream Lambda ERP v1.1.10, no MSME code, no assessment brief) | 1 genuine upstream test failure; 13 pytest failures are a shared-state artefact (`history/PASS0_BASELINE.md`) |
| 1 Research + backlog | Seven candidate repositories inspected from source; Lambda ERP foundation, docket-idp as a dependency; gap register | two candidates rejected for having no licence file (`history/PASS1_RESEARCH_AND_GAP_REGISTER.md`) |
| 2 Rules + upload security | Effective-dated MSME/GST/scheme rule engine; content-based upload validation; JWT algorithm pinning; CI scanning | uploads trusted the client's content type; OIDC `alg` taken from the token header |
| 3 Authorization evidence | OpenAPI-driven authorization matrix, cross-user isolation tests, SQL-injection fuzzing; tenancy decision D6 | no row-level tenancy upstream (verified); the shipped Terraform is a single-replica SQLite demo |
| 4 Document intelligence | docket adapter, cited fields, corrections, duplicates, deterministic checks (GSTIN check digit verified), PostgreSQL parity | docket reports an LLM outage as an empty review item; tests had silently used in-memory SQLite |
| 5 Compliance workspace | Persisted deadlines, versioned workpapers, maker-checker, hash-chained audit, PDF/CSV reports | an edited original invoice was flagged as a duplicate of its own copy (Pass 4 bug, fixed) |
| 6 Credit readiness + schemes | Append-only facts, derived classification, evidence-linked scheme criteria, immutable assessments, PDF summary | only two schemes encoded (PMMY, CGTMSE) |
| 7 Reconciliation | Explainable invoice-to-bank matching, DB-enforced single confirmed match, turnover cross-check | PostgreSQL deadlock in concurrent confirmations (14/25 races crashed; fixed, 0/25) |
| 8 Deployment readiness | Render Blueprint, privilege-dropping entrypoint, structured logs + request ids, readiness + admin status, scheduled sweep, blob/local storage factory, restore drill | Render plan names I first used were outdated; a root-owned disk would have failed the first deploy |

# Pass 3 delivery — apply on top of Pass 2 (copy `files/` over the repo root)

## NEW files
| Path | Purpose |
|---|---|
| tests/test_authz_matrix.py | OpenAPI-driven route allowlists (anon/viewer), cross-user isolation, role escalation, cookie flags, tenancy characterisation. `python -m tests.test_authz_matrix` |
| tests/test_sql_injection_probes.py | 3,640 injection probes over 28 GET operations; `SQLI_POSITIVE_CONTROL=1` proves the detector fires. `python -m tests.test_sql_injection_probes` |
| docs/msme/TENANCY.md | Decision D6 (deployment = tenant boundary), evidence, residual risks R-1..R-4 |

## MODIFIED files (relative to Pass 2)
| Path | Change |
|---|---|
| .github/workflows/ci.yml | run the two new suites |
| docs/msme/GAP_REGISTER.md | v3 statuses incl. new X-2 (production persistence) |

No application code changed in this pass: the matrix found no authorization defect.

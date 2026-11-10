# Pass 2 delivery — apply on top of upstream `lambda-erp-master` (v1.1.10)

Apply: copy `files/` over the repo root (new + modified files at exact paths), or
`patch -p1 < modified-files.patch` for modified files then copy the NEW files.
Verified: patch applies cleanly to the pristine upload; new suites pass on that clean tree.

## NEW files
| Path | Purpose |
|---|---|
| msme/__init__.py | package marker |
| msme/rules/__init__.py, engine.py | effective-dated engine: MSME classification, GST deadlines, scheme criteria |
| msme/rules/data/msme_classification.json | S.O. 1364(E) + previous limits, with source records |
| msme/rules/data/gst_calendar.json | GST due-date rules (monthly / QRMP / composition) |
| msme/rules/data/schemes.json | PMMY (2 versions) and CGTMSE criteria, unverified details listed |
| msme/security/__init__.py, upload_validation.py | magic-byte upload validation, zip limits, safe Content-Disposition |
| tests/test_msme_rules.py | 23 tests (pytest) |
| tests/test_upload_security.py | S-2 API regression (`python -m tests.test_upload_security`) |
| tests/test_upload_validation_unit.py | 6 pure unit tests (pytest) |
| tests/test_oauth_alg_pinning.py | OIDC algorithm pinning (`python -m ...`) |
| .github/workflows/security.yml | pip-audit, npm audit, bandit, gitleaks |
| docs/msme/SECURITY_EXCEPTIONS.md, RULE_SOURCES.md, GAP_REGISTER.md | registers |

## MODIFIED files
| Path | Change |
|---|---|
| api/attachments.py | validate content after read; stored extension from detected type; safe download headers (nosniff, no-store, inline only for PDF/images) |
| api/oauth.py | pin ID-token algorithm to RS256; reject other `alg` with 400 |
| api/routers/setup.py | `hashlib.md5(..., usedforsecurity=False)` (demo-data hash; bandit High) |
| pyproject.toml | add `msme` to wheel packages and sdist |
| Dockerfile | `COPY msme ./msme` |
| .github/workflows/ci.yml | run new suites |

## Behaviour change to know about (hard to notice)
Uploads whose bytes contradict their declared type are now rejected (400); stored files no longer keep
user-chosen extensions. Legitimately mislabelled files (CSV as vnd.ms-excel, Office as octet-stream) still pass.

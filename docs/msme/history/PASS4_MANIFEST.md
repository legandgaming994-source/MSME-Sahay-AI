# Pass 4 delivery — document intelligence (apply on top of Pass 3)

Apply: copy `files/` over the repo root, or `patch -p1 < modified-files.patch` (modified files) and copy the NEW files.
Verified: Pass-3 tree + patch + NEW files reproduces the tested tree byte-for-byte.

## NEW files
| Path | Purpose |
|---|---|
| msme/documents/migrations.py | DDL for 4 tables (portable SQLite/PostgreSQL), registered as migration 40 |
| msme/documents/storage.py | private content-addressed storage (0600/0700, atomic writes, traversal-proof keys) |
| msme/documents/extraction.py | `Extractor` seam + `DocketExtractor` adapter (outage -> retryable failure, secrets scrubbed) |
| msme/documents/checks.py | deterministic validation: arithmetic, GSTIN check character, dates, evidence quality, business key |
| msme/documents/service.py | ingest / claim-based process / review / correct / acknowledge / approve / sweep |
| msme/documents/__init__.py | package marker |
| api/routers/msme_documents.py | REST API (viewer reads; non-demo manager writes; interactive admin sweep) |
| tests/test_msme_checks.py | 18 pure tests (real GSTIN vectors) |
| tests/test_msme_documents.py | service scenarios: dedupe race, exclusive claim, retries, stale recovery, history, tamper |
| tests/test_msme_documents_api.py | HTTP roles, hostile uploads, review flow |
| tests/test_msme_docket_integration.py | REAL docket + real Tesseract scan; only the model is stubbed |
| tests/_msme_db.py | backend selection + `assert_backend` (guards against silent in-memory SQLite) |
| docs/msme/DOCUMENT_INTELLIGENCE.md, THIRD_PARTY.md | architecture/limits/config; licence attribution incl. docket NOTICE |

## MODIFIED files (relative to Pass 3)
| Path | Change |
|---|---|
| lambda_erp/database.py | +`_m040_msme_document_intelligence` and registry entry 40 |
| api/main.py | import + `include_router(msme_documents.router)` |
| pyproject.toml | optional extra `msme = ["docket-idp>=0.5,<0.6"]` |
| Dockerfile | apt `tesseract-ocr`; `pip install ".[msme]"` (UNVERIFIED: no Docker in sandbox) |
| .github/workflows/ci.yml | install tesseract + extras + pymupdf; run new suites on SQLite and PostgreSQL |
| .github/workflows/security.yml | audit `.[msme]` dependency tree |
| docs/msme/GAP_REGISTER.md | v4 |

## Operator notes
- Set `DOCKET_TEXT_MODEL` and a model endpoint, and `MSME_STORAGE_DIR` on persistent private storage, before use.
- Hosted model providers receive document text/images; see DOCUMENT_INTELLIGENCE.md.

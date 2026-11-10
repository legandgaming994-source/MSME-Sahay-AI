# Document intelligence (D-1, D-2, D-3)

Code: `msme/documents/` (service, checks, extraction adapter, storage, migration 40) and `api/routers/msme_documents.py`.

## Flow
```
upload (manager) -> content validation (magic bytes, size, type) -> SHA-256 dedupe -> private storage
   -> BackgroundTasks: claim job (compare-and-set) -> docket (text layer / Tesseract -> model -> cited fields)
   -> persist fields (+ page, bbox, quote, confidence) -> deterministic checks -> status
   -> human review: correct fields (append-only history) / acknowledge issues -> approve
```
States: `uploaded -> processing -> extracted | needs_review | failed`; `approved` only when no unacknowledged CONFIRMED issue;
any edit revokes approval; `failed` retries up to 3 times, then needs an explicit forced reprocess; a `processing` job older
than 15 minutes is reclaimable (crash recovery).

## Evidence model
Each field stores `original_value` (first machine reading, never updated), `machine_value` (latest reading), `current_value`
(what downstream uses), `confidence`, `page`, `quote`, normalised `bbox` and `location_status`
(`verified | fuzzy | conflicting | unlocated | none | manual`). A value with no resolvable citation has **no confidence** and is
flagged; confidence is never invented. Corrections survive reprocessing; the machine's newer reading is kept beside them.

## Issues (confirmed vs heuristic)
| Severity | Meaning | Examples |
|---|---|---|
| CONFIRMED | a deterministic rule is violated by the values as recorded | line/subtotal/total mismatch (round-off beyond 0.50), invalid GSTIN check character, missing required field, due before issue, docket "value not on cited line", "rows cite no source" |
| HEURISTIC | plausibility signal | possible round-off (<= 0.50), tax-rate mismatch (mixed rates), low confidence (< 0.80) or unlocated/conflicting citation on key fields, future date, duplicate candidate |

CONFIRMED issues block approval until the data is corrected **or acknowledged with a reason** (who/why/when stay on the issue).
An acknowledgement is dropped automatically if the underlying numbers/message change.

GSTIN check: structure + check character (verified against three independently published GSTINs; 1,470/1,470 single-character
substitutions and 35/35 adjacent transpositions detected). It is a GSTN convention, not statute text, and proves nothing about
registration status.

## Duplicates
Exact bytes: UNIQUE `sha256` -> same document returned (also under concurrent upload). Re-issued content: business key
`supplier GSTIN (or name) | invoice number | issue date | total`. Within a key group **the earliest upload is the original** and
every other member gets `duplicate_of` + a HEURISTIC `DUPLICATE_CANDIDATE`. The group is recomputed whenever membership or a key
changes, so editing the original never marks it a duplicate of its copy (a Pass 4 defect found in Pass 5 and fixed, with
regression tests), a departing original hands the role to the next-oldest, and an already-approved copy that becomes a duplicate
because an older upload is processed later is demoted to review. `created_at` has microsecond precision so upload order is stable.

## API (all under /api/msme/documents)
| Method / path | Role |
|---|---|
| GET `` (status, limit<=200, offset), GET `/{id}`, GET `/{id}/file`, GET `/{id}/history` | viewer |
| POST `` (multipart `file`, `process=true|false`), POST `/{id}/process?force=`, PUT `/{id}/fields`, POST `/{id}/issues/acknowledge`, POST `/{id}/approve` | manager (not demo `public_manager`) |
| POST `/sweep` | interactive admin |

## Configuration (production)
| Variable | Purpose |
|---|---|
| `MSME_STORAGE_DIR` | **Must point at persistent, private storage.** Default `/tmp/lambda-erp-msme-documents` is ephemeral (gap P-1) |
| `DOCKET_TEXT_MODEL` | **Required.** Docket refuses to run without a text model name |
| `DOCKET_LLM_PROVIDER`, `DOCKET_LLM_BASE_URL`, `DOCKET_LLM_API_KEY` | model endpoint (Ollama local, or OpenAI-compatible hosted) |
| `DOCKET_VISION_MODEL` | optional; enables the vision-model OCR fallback. Unset = no fallback (clear OCR failures instead of "vlm unavailable") |
| Tesseract binary | installed by the Dockerfile (`tesseract-ocr`) |

**Data flow warning:** with a hosted provider, page text/images go to that provider. Use a local model or a provider agreement
that fits the data (invoices contain tax IDs, bank details and personal data).

## Verified (see tests)
- Real docket pipeline on real PDFs and a rasterised scan through real Tesseract; only the model is stubbed.
  Synthetic clean 200 DPI scan: 16/17 cited fields `verified`, 1 `fuzzy`, total_amount confidence 0.96, lowest 0.64.
- Concurrency: 8 simultaneous identical uploads -> one record; 5 simultaneous process calls -> extractor runs once.
- Backends: file SQLite and PostgreSQL 16 (backend asserted by the test).
- Outage mapping: docket reports an unreachable model as `needs_review` with nothing extracted; the adapter converts that to
  a retryable `failed/extraction_empty` (found by running unstubbed; covered by a test).

## NOT verified / limits
- **Model accuracy is not evaluated.** The model is scripted in tests. No labelled Indian-invoice evaluation set exists yet.
  OCR numbers above are for a clean synthetic scan; photographed or skewed documents will do worse.
- Not implemented: HSN/SAC and GST-rate validation, CGST/SGST/IGST split checks, e-invoice IRN/QR parsing, multi-page
  statement handling beyond docket defaults, languages other than English OCR.
- Not implemented: retention/deletion workflow, per-user quotas, rate limits, object storage, scheduled sweep (the endpoint
  exists; nothing calls it yet), malware scanning, DB-level enforcement that corrections are append-only (application convention).
- Background processing uses FastAPI BackgroundTasks (in-process, not a durable queue). Job claiming is a database
  compare-and-set, tested across threads on SQLite and PostgreSQL; multi-process / multi-instance contention is NOT tested.
- Prompt injection: extraction is data-only (model output only fills schema-validated fields; no tool calls) and docket
  rejects values that their own citation does not print. Adversarial document corpora against a real model are not yet run (S-3).
- Docker image changes (tesseract, `.[msme]`) are unverified: no Docker in the build sandbox.

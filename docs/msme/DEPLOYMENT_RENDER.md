# Deploying on Render

Blueprint: `render.yaml` (repository root). Status: **written and tested locally; NOT deployed** (no Render account or
credentials were available) and **NOT validated by Render's own validator** (`render blueprints validate` runs server-side and needs
`render login`). Run it before the first sync. Everything below marked "verified" was checked against Render's published
documentation or by a test in this repository; everything else is marked as not verified.

## What the Blueprint creates
| Resource | Setting | Why |
|---|---|---|
| Web service `lambda-erp-msme` | Docker, plan `1c-2g` (1 CPU / 2 GB), region `singapore`, **1 instance** | OCR (Tesseract), PDF rendering and the extraction stack do not fit in 512 MB |
| Persistent disk `msme-data` | 10 GB at `/var/data` | documents (`/var/data/msme-documents`) and chat attachments (`/var/data/chat-uploads`) |
| PostgreSQL `msme-db` | plan `0.5c-1g`, version 16, `ipAllowList: []` | paid plan => continuous point-in-time recovery; no external connections, only Render's private network |
| Health check | `/api/ready` | 200 only if the database answers, migrations are at head and storage is writable |
| Deploys | `autoDeployTrigger: checksPass` | deploys only when the branch's GitHub Actions checks pass |

Approximate monthly cost from Render's pricing page when this was built (verify before committing): web ~$25, database ~$19,
disk ~$2.50 (`$0.25/GB`), plus LLM usage. There are no cost controls in the Blueprint; plans are fixed-price.

## Minimum actions to deploy (the external dependencies)
1. Push this repository to GitHub (Render deploys from Git).
2. `render login`, then `render blueprints validate render.yaml` (fix anything it reports; plan IDs and regions follow Render's
   current compute naming).
3. Render Dashboard -> New -> Blueprint -> select the repository. Render prompts for every `sync: false` value:
   `LAMBDA_ERP_ADMIN_EMAIL`, `LAMBDA_ERP_ADMIN_PASSWORD` (seeds the first admin so a stranger cannot claim it by registering first),
   `OPENAI_API_KEY` (chat agent), `DOCKET_TEXT_MODEL`, `DOCKET_LLM_PROVIDER`, `DOCKET_LLM_BASE_URL`, `DOCKET_LLM_API_KEY`
   (document extraction). **These prompts appear only on the first sync**; later changes are made in the Dashboard.
4. After the first deploy: open `https://<service>.onrender.com/api/ready` (expect `{"status":"ready"}`), sign in as the seeded admin and
   open `GET /api/ops/status` (admin only): it lists configuration warnings.
5. In the Dashboard configure notifications for failed deploys and service failures (not expressible in a Blueprint) and connect a log
   stream if you want retention beyond Render's.

## Hard-to-reverse decisions
- **Region** (`singapore`): fixed at creation for the service and the database; they must match for private networking.
- **Database name/user**, and Postgres major version changes, are not edited in place.
- **Persistent disk => single instance and a short gap on every deploy** (Render stops the old instance before the new one starts; verified in
  Render's disk documentation). Scaling out later means moving documents to object storage first.
- **Disks are invisible to builds, pre-deploy commands and one-off jobs** (verified), so migrations run inside the app at startup (no
  `preDeployCommand`) and the periodic document sweep runs inside the web service (`MSME_SWEEP_INTERVAL_SECONDS`, 300 s here).

## Container behaviour
`docker-entrypoint.sh` starts as root only to create and chown the configured data directories (Render mounts a new disk root-owned, so a
non-root process could not write it), then drops to the unprivileged `app` user and `exec`s uvicorn (so SIGTERM reaches the app directly). Tested
with a root-owned directory, a throwaway user, signal delivery and hostile paths (`tests/test_docker_entrypoint.py`). The Docker build itself
was **not run** (no Docker in the build environment).

## Configuration reference
| Variable | Set by | Purpose |
|---|---|---|
| `LAMBDA_ERP_DB` | Blueprint (`fromDatabase`) | PostgreSQL connection string |
| `MSME_STORAGE_DIR`, `LAMBDA_ERP_UPLOAD_DIR` | Blueprint | directories on the persistent disk |
| `LAMBDA_ERP_AUTO_DEMO` | Blueprint (`0`) | the image defaults to `1` (demo data): never for real books |
| `JWT_SECRET_KEY` | Render (`generateValue`) | session signing key |
| `LAMBDA_ERP_ADMIN_EMAIL` / `_PASSWORD` | prompted | first admin |
| `MSME_REQUIRE_DISTINCT_APPROVER` | Blueprint (`1`) | maker-checker; `0` only for a one-person business (every waived approval is audited) |
| `MSME_SWEEP_INTERVAL_SECONDS` | Blueprint (`300`) | in-process sweep of queued/stuck documents (clamped 30..86400; `0` = off) |
| `DOCKET_*`, `OPENAI_API_KEY` | prompted | model providers. **With a hosted provider, document text and images are sent to it.** |
| `LOG_LEVEL` | Blueprint (`INFO`) | JSON logs on stderr; each line carries a request id; credentials are redacted |
| `MSME_STORAGE_BACKEND` | optional | `local` (default) or `azure_blob` (an Azure option; not used on Render) |

## Observability
JSON logs (stderr) with `X-Request-ID` correlation: quote the id a user sees in a 500 response to find the stack trace. Health polls are not logged at INFO.
`GET /api/ops/status` (admin) shows versions, schema version, document queue (stuck, dead-letter, oldest queued age), overdue compliance tasks,
the audit-chain head (`?verify_audit=true` verifies the whole chain) and configuration warnings. Alerting itself is a Dashboard task.

## Backup and restore
| Data | Mechanism | RPO | Verified |
|---|---|---|---|
| PostgreSQL | Render point-in-time recovery (3-day window on a Hobby workspace, 7-day on Pro; cannot restore to within ~10 minutes of now; a recovery creates a **new** instance) and dashboard exports kept 7 days | minutes | documentation only |
| Documents + chat attachments | Render disk snapshots: **daily**, kept at least 7 days, dashboard-only, all-or-nothing restore | **up to 24 hours** | documentation only |
| Both, logical | `python -m tests.test_msme_restore_drill`: real `pg_dump`/`pg_restore` and file copy, original destroyed, restored system compared | n/a | **tested on PostgreSQL 16 and SQLite** |

The drill proves the procedure and the data model (row counts, indexes, schema version, **audit hash chain identical**, every document checksum, post-backup
changes absent, tampering detected). It does not prove Render's snapshots or PITR themselves.
**Documents are the weakest link:** a daily snapshot can lose a day of uploads, and a database restored to a later point than the disk references files
that are missing (reads fail loudly with `storage_corrupt` rather than returning wrong data). Closing the gap needs an off-platform, automated copy of the
document store (object storage or `rclone`/`rsync` from a Render shell on a schedule). **That automation is not built** (gap D-4).
Restore order: restore the database (new instance), re-point `LAMBDA_ERP_DB`, restore the disk snapshot, then run `GET /api/ops/status?verify_audit=true`.

## Security notes
- The database accepts only private-network connections (`ipAllowList: []`); to run `pg_dump` from outside, temporarily add your IP, then remove it.
- Public endpoints: login/register/health/`/api/ready` (minimal body) and, when MCP is enabled, OAuth client registration (see `TENANCY.md`).
- Tenancy: **one organisation per deployment** (decision D6). Every authenticated user can read every company in the database.
- Sign-up: public sign-up is off by default; additional users are invited by an admin.

## Not verified / not built
- Render's validator, an actual deploy, the Docker image build, and GitHub Actions on a real runner.
- Off-platform automated backup of documents (D-4); zero-downtime deploys (impossible with a disk); horizontal scaling.
- Rate limiting and per-user quotas; Render-side alerts as code.

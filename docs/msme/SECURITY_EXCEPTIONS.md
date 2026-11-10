# Security exceptions register

| Advisory | Package | Why accepted | Mitigation / evidence | Review trigger |
|---|---|---|---|---|
| PYSEC-2026-4298 | python-jose 3.5.0 | No upstream fix. Exploit needs a PEM/DER public key used as an HMAC secret with unpinned algorithms. | Every `jwt.decode` pins `algorithms` server-side; OIDC ID-token alg pinned to RS256 (`tests/test_oauth_alg_pinning.py`). Key material comes from JWK dicts, which python-jose refuses as HMAC keys. | Migrate to PyJWT (recommended follow-up); re-check on any python-jose release. |
| PYSEC-2026-1325 | ecdsa 0.19.2 (transitive) | Minerva timing attack on P-256 *signing*; maintainers will not fix. | App signs JWTs with HS256 only; no ECDSA signing path in app code. | Removed with python-jose migration. |

Not exceptions (fixed): bandit B324 (MD5, demo-data only) flagged High -> `usedforsecurity=False`.
Open: 45 bandit B608 (SQL built with f-strings) mediums need triage (gap S-4).

# Third-party components (v2, Pass 4)

Compliance record, not legal advice. Review with counsel before commercial redistribution.

| Component | How used | Licence | Obligations / status |
|---|---|---|---|
| Lambda ERP (TORUS INVESTMENTS AG) | Foundation (modified) | Apache-2.0 | Keep LICENSE; modified files are listed in each delivery MANIFEST |
| docket-idp 0.5.0 (Artem Kazakov Kozlov) | Runtime dependency via the optional `msme` extra; imported by `msme/documents/extraction.py` only. **No docket source is copied or vendored.** | Apache-2.0 + NOTICE | Reproduce docket's NOTICE (below) when redistributing images containing it |
| docket's bundled e-invoice artefacts: KoSIT XRechnung config/Schematron | Shipped inside the docket wheel; **not used** by this platform | Apache-2.0 | Keep notices (they are inside the installed package under `docket/einvoice/resources/licenses/`) |
| docket's bundled OASIS UBL 2.1 schemas | same | OASIS notice | Retain the copyright/permission notice (unmodified in the wheel) |
| docket's bundled CEN EN 16931 XSLT (ConnectingEurope) | same | **EUPL-1.2** | Include licence text; provide source or a durable source location (docket's `manifest.json` links the pinned archive); files must stay unmodified |
| Tesseract OCR (host binary) | OCR engine, installed by apt in the Docker image / CI | Apache-2.0 | Distributed by the OS package; no action beyond the package's own notices |
| pdfplumber, pypdfium2, pytesseract, scikit-learn, httpx, pydantic, pillow | docket dependencies | MIT / BSD / Apache-2.0 (as published) | Standard notice retention; audited with pip-audit in CI |
| pymupdf | **Test-only** (generates synthetic PDFs/scans) | AGPL-3.0 / commercial | Never installed in production images; CI/test use only. Do not add to runtime dependencies without a licence decision |
| azure-storage-blob, azure-identity (optional `azure` extra) | optional Azure Blob document backend | MIT | standard notice retention; audited with pip-audit |
| Azurite (npm, test-only) | Azure Storage emulator for `tests/test_msme_storage.py` | MIT | never shipped |
| Render CLI v2.34.0 | used once to check what `render blueprints validate` requires; not part of the product | Apache-2.0 | none |
| util-linux `setpriv` | privilege drop in `docker-entrypoint.sh` | GPL-2.0+ (OS package) | provided by the base image; not redistributed by this project |
| Candidate repos not used as code: TaxHacker (MIT), ai-document-intake (MIT), openinvoice (MIT) | Reference only | MIT | None while no code is copied |
| Invoice-Manager, open-invoice | Rejected | no LICENSE file | Not used |

## Decision: the unused e-invoice artefacts
Docket ships them in the wheel; this platform never calls the e-invoicing paths. Options: (a) keep as shipped and honour the
notices above (current); (b) delete `docket/einvoice/resources/` in the Docker build to avoid redistributing EUPL material
(docket treats missing resources as a supported "fetch on request" state). Option (b) is not implemented because it changes
an installed package and could not be tested in this sandbox (no Docker). Recommend counsel pick one before a public release.

## docket NOTICE (verbatim)
```
docket
Copyright 2026 Artem Kazakov Kozlov

This product is licensed under the Apache License, Version 2.0 (see LICENSE).

This product includes third-party electronic-invoice validation artefacts:

- XRechnung validator configuration and Schematron rules, copyright KoSIT
  and contributors, licensed under Apache License 2.0.
- EN 16931 validation artefacts maintained by the ConnectingEurope project,
  licensed under EUPL 1.2.
- OASIS Universal Business Language 2.1 schemas, copyright OASIS Open 2013.
  The OASIS notice and permissions are preserved with the distribution.

It does not include the UN/CEFACT CII D16B schemas, the Peppol BIS Billing
3.0 rules or the Factur-X (FNFE-MPE / FeRD) artefacts: `docket einvoice
fetch` downloads them from their publishers on the user's request.

Full available notices are under
`docket/einvoice/resources/licenses/`. See `docs/THIRD_PARTY_LICENSES.md` for
source links, obligations, and unresolved redistribution questions.
```

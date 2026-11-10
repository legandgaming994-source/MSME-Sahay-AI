# Rule data source register (checked 2026-10-17)

Status key: PRIMARY = official document seen; SECONDARY = law-firm/news/bank summaries only. Rule data in
`msme/rules/data/*.json` carries the same status per version; **SECONDARY rules must be confirmed against the
primary notification before any customer-facing use.**

| Rule | Value encoded | Effective | Evidence status | Primary source still to obtain |
|---|---|---|---|---|
| MSME classification (S.O. 1364(E), 21 Mar 2025) | Micro 2.5cr/10cr, Small 25cr/100cr, Medium 125cr/500cr (investment/turnover); composite, both limits | 1 Apr 2025 | SECONDARY (6+ consistent sources) | Gazette notification (MSME Ministry / egazette) |
| Previous classification (S.O. 2119(E)) | 1cr/5cr, 10cr/50cr, 50cr/250cr | to 31 Mar 2025 | SECONDARY; start date 1 Jul 2020 NOT verified | Gazette notification |
| GST due dates | GSTR-1 11th; GSTR-3B 20th; QRMP GSTR-1 13th, GSTR-3B 22nd/24th, PMT-06 25th; IFF 13th; CMP-08 18th; GSTR-4 30 Apr; GSTR-9 31 Dec | statutory default | SECONDARY (cross-checked against published FY2026-27 tables; matched) | CBIC notifications / gst.gov.in calendar |
| QRMP state category (X=22nd, Y=24th) | NOT encoded; caller must choose | - | NOT VERIFIED | CBIC notification for QRMP categories |
| PMMY limits | Shishu <=50k, Kishor <=5L, Tarun <=10L, Tarun Plus <=20L (needs prior Tarun repayment) | 24 Oct 2024 | PRIMARY (PIB document) | - |
| PMMY pre-2024 limit | INR 10 lakh | 8 Apr 2015 - 23 Oct 2024 | SECONDARY | MUDRA/PIB archive |
| CGTMSE | Udyam + Micro/Small + manufacturing/service + collateral-free; cover cap INR 5 crore from 1 Apr 2023 | 1 Apr 2023 | SECONDARY, sources conflict | CGTMSE operational guidelines |

## Deliberately NOT modelled (conflicting or unverified)
- CGTMSE guarantee percentages (75/80/85/90% in different sources) and annual guarantee fee schedule.
- Whether CGTMSE facilities above INR 5 crore are ineligible or merely capped (sources conflict).
- Trading-sector eligibility for CGTMSE.
- Mudra / CGTMSE document checklists are labelled UNVERIFIED_TYPICAL.
- GST due-date shifts for holidays and ad-hoc extensions (supported only via explicit `overrides`).
- Section 8(6) fiscal-year grace and export-turnover exclusions in MSME classification.

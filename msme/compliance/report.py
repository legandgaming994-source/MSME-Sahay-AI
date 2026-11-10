"""Downloadable reports. Every output says, prominently, that it is a preparation document and not a filing."""

from __future__ import annotations

import csv
import html
import io
import json
from typing import Iterable

from . import audit

BANNER = "DRAFT PREPARATION WORKPAPER \u2014 NOT A FILED RETURN"
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _deny_fetch(url, *args, **kwargs):  # user text must never make the renderer fetch anything
    raise ValueError(f"External resource blocked: {url[:60]}")


def csv_safe(value) -> str:
    """Neutralise spreadsheet formula injection in text cells (OWASP CSV injection)."""
    text = "" if value is None else str(value)
    return ("'" + text) if text.startswith(_FORMULA_PREFIXES) else text


def calendar_csv(rows: Iterable[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["company", "fy_start_year", "form", "period", "due_date", "basis", "status", "overdue",
                     "days_to_due", "optional", "filing_reference_user_recorded"])
    for r in rows:
        writer.writerow([csv_safe(r["company"]), r["fy_start_year"], csv_safe(r["form"]), csv_safe(r["period"]),
                         r["due_date"], csv_safe(r["basis"]), csv_safe(r["status"]), "yes" if r["overdue"] else "no",
                         int(r["days_to_due"]), "yes" if r["optional"] else "no", csv_safe(r.get("filing_reference"))])
    return out.getvalue()


def workpaper_html(service, workpaper_id: str) -> str:
    from lambda_erp.database import get_db

    wp = service.get_workpaper(workpaper_id)
    task, content = wp["task"], wp["content"]
    fig = content["figures"]
    head = audit.head(get_db())
    basis = ("Statutory default date; government extensions are not applied" if task["basis"] == "STATUTORY_DEFAULT"
             else "Extension entered by the user (not verified)")
    rows = "".join(
        f"<tr><td>{_e(i['label'])}{' *' if i['required'] else ''}</td><td>{_e(i['status'])}</td>"
        f"<td>{_e(i['note'])}</td><td>{_e(', '.join(i['document_ids']))}</td></tr>" for i in content["checklist"])
    docs = "".join(
        f"<tr><td>{_e(d['id'])}</td><td>{_e(d['invoice_number'])}</td><td>{_e(d['issue_date'])}</td>"
        f"<td>{_e(d['direction'])}</td><td class=n>{_e(d['subtotal'])}</td><td class=n>{_e(d['tax'])}</td>"
        f"<td class=n>{_e(d['total'])}</td></tr>" for d in fig["documents"]) or "<tr><td colspan=7>None</td></tr>"
    attention = "".join(f"<li>{_e(k.replace('_', ' '))}: {_e(', '.join(v))}</li>" for k, v in fig["attention"].items() if v)
    filing = ""
    if task["status"] == "filing_recorded":
        filing = (f"<h2>Filing record (entered by the user)</h2><p>Reference {_e(task['filing_reference'])}, "
                  f"date {_e(task['filed_on'])}, recorded by {_e(task['filing_recorded_by'])}. "
                  f"<b>Not verified against the GST portal.</b></p>")
    approval = (f"Prepared by: {_e(', '.join(wp['contributors']))}<br>Submitted by: {_e(wp['submitted_by'])} "
                f"at {_e(wp['submitted_at'])}<br>Decision: {_e(wp['status'])} by {_e(wp['decided_by'])} "
                f"at {_e(wp['decided_at'])}<br>{_e(wp['decision_note'])}")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body{{font:10pt/1.4 sans-serif;color:#111}} h1{{font-size:15pt;margin:0 0 4pt}} h2{{font-size:11pt;margin:14pt 0 4pt}}
.banner{{background:#7a1f1f;color:#fff;padding:6pt 8pt;font-weight:bold;margin-bottom:8pt}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #999;padding:3pt 5pt;text-align:left;vertical-align:top}}
.n{{text-align:right}} .small{{font-size:8pt;color:#444}}
</style></head><body>
<div class="banner">{_e(BANNER)}</div>
<h1>{_e(task['form'])} \u2014 {_e(task['period'])} \u2014 {_e(task['company'])}</h1>
<p>Due date {_e(task['due_date'])} ({_e(basis)}); rule version {_e(task['rule_version'])}. Task status: {_e(task['status'])}.
Workpaper version {_e(wp['version'])} ({_e(wp['status'])}).</p>
<h2>Preparation checklist <span class=small>({_e(content['checklist_source'])}; * = required)</span></h2>
<table><tr><th>Item</th><th>Status</th><th>Note</th><th>Documents</th></tr>{rows}</table>
<p class=small>{_e(content['checklist_notice'])}</p>
<h2>Figures from approved documents ({_e(fig['from'])} to {_e(fig['to'])})</h2>
<table><tr><th></th><th>Documents</th><th>Taxable value</th><th>Tax</th><th>Total</th></tr>
<tr><td>Outward</td><td class=n>{fig['outward']['count']}</td><td class=n>{_e(fig['outward']['taxable_value'])}</td><td class=n>{_e(fig['outward']['tax'])}</td><td class=n>{_e(fig['outward']['total'])}</td></tr>
<tr><td>Inward</td><td class=n>{fig['inward']['count']}</td><td class=n>{_e(fig['inward']['taxable_value'])}</td><td class=n>{_e(fig['inward']['tax'])}</td><td class=n>{_e(fig['inward']['total'])}</td></tr></table>
<p class=small>{_e(fig['disclaimer'])}</p>
<h2>Documents included</h2><table><tr><th>Id</th><th>Invoice</th><th>Date</th><th>Direction</th><th>Taxable</th><th>Tax</th><th>Total</th></tr>{docs}</table>
{('<h2>Needs attention (not included in totals)</h2><ul>' + attention + '</ul>') if attention else ''}
<h2>Notes</h2><p>{_e(content['notes'])}</p>
<h2>Approval trail</h2><p>{approval}</p>{filing}
<p class=small>Figures digest {_e(wp['figures_digest'])}. Audit chain head: #{head['seq']} {_e(head['hash'])}
(record this outside the system to detect wholesale rewriting of the audit log).</p>
</body></html>"""


def workpaper_pdf(service, workpaper_id: str) -> bytes:
    from weasyprint import HTML

    return HTML(string=workpaper_html(service, workpaper_id), url_fetcher=_deny_fetch).write_pdf()

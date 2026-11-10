"""Exportable application-preparation summary. It states plainly that it is not an application and not a decision."""

from __future__ import annotations

import html

BANNER = "APPLICATION PREPARATION SUMMARY \u2014 NOT AN APPLICATION AND NOT A LENDER DECISION"
STATUS_TEXT = {"CRITERIA_NOT_MET": "Published criteria NOT met on the stated facts",
               "CRITERIA_APPEARS_MET": "Published criteria appear met on the stated facts (not an approval)",
               "INSUFFICIENT_EVIDENCE": "Insufficient evidence to tell"}


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _deny_fetch(url, *args, **kwargs):
    raise ValueError(f"External resource blocked: {url[:60]}")


def summary_html(data: dict) -> str:
    a, cls = data["assessment"], data["classification"]
    src = a["source"]
    crit = "".join(f"<tr><td>{_e(c['description'])}</td><td>{_e(c['status'])}</td><td>{_e(c['reason'])}</td>"
                   f"<td>{_e(c['evidence_source'] or 'none provided')}</td></tr>" for c in a["criteria"])
    facts = "".join(f"<tr><td>{_e(f['key'])}</td><td>{_e(f['value'])}</td><td>{_e(f['source'])}</td>"
                    f"<td>{_e(f['entered_by'])} {_e(f['entered_at'])}</td><td>{_e(f['evidence_document_id'])}</td></tr>"
                    for f in data["facts"]) or "<tr><td colspan=5>No facts entered</td></tr>"
    missing = "".join(f"<li><b>{_e(m['key'])}</b>: {_e(m['description'])}</li>" for m in a["missing_facts"])
    ready = "".join(f"<tr><td>{_e(i['label'])}</td><td>{_e(i['status'])}</td><td>{_e(i['what_is_missing'])}</td></tr>"
                    for i in data["readiness"]["items"])
    docs = "".join(f"<li>{_e(d)}</li>" for d in a["typical_documents"].get("items", []))
    unverified = "".join(f"<li>{_e(u)}</li>" for u in a["unverified_details"])
    notes = "".join(f"<li>{_e(n)}</li>" for n in a["notes"])
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body{{font:10pt/1.4 sans-serif;color:#111}} h1{{font-size:15pt;margin:0 0 4pt}} h2{{font-size:11pt;margin:14pt 0 4pt}}
.banner{{background:#7a1f1f;color:#fff;padding:6pt 8pt;font-weight:bold;margin-bottom:8pt}}
.status{{border:2px solid #333;padding:6pt;font-weight:bold}} table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #999;padding:3pt 5pt;text-align:left;vertical-align:top}} .small{{font-size:8pt;color:#444}}
</style></head><body>
<div class="banner">{_e(BANNER)}</div>
<h1>{_e(a['scheme_name'])} \u2014 {_e(data['company'])}</h1>
<p>Prepared as of {_e(data['as_of'])} against rule version {_e(a['rule_version'])} (effective {_e(a['effective_from'])}).</p>
<p class="status">{_e(STATUS_TEXT.get(a['status'], a['status']))}</p>
<p class="small">{_e(a['disclaimer'])} A lender or the scheme administrator makes any decision.</p>
<h2>Enterprise classification</h2>
<p>{_e(cls['status'])}{(': ' + _e(cls['category'])) if cls['category'] else ''}; rule {_e(cls['rule_version'])};
{_e('; '.join(cls['explanation']))}. Source: {_e(cls['source']['title'])} ({_e(cls['source']['verification'])}).</p>
<h2>Criteria and the evidence used</h2>
<table><tr><th>Criterion</th><th>Result</th><th>Basis</th><th>Evidence source</th></tr>{crit}</table>
{('<h2>What to provide next</h2><ul>' + missing + '</ul>') if missing else ''}
<h2>Facts on file</h2>
<table><tr><th>Fact</th><th>Value</th><th>Source</th><th>Entered by</th><th>Document</th></tr>{facts}</table>
<h2>Document readiness</h2>
<table><tr><th>Item</th><th>Status</th><th>What is missing</th></tr>{ready}</table>
<p class="small">{_e(data['readiness']['notice'])}</p>
<h2>Typical documents for this scheme ({_e(a['typical_documents'].get('verification', 'UNVERIFIED_TYPICAL'))})</h2><ul>{docs}</ul>
<h2>How to apply</h2><p>{_e(a['application_route'])}</p>
{('<h2>Scheme notes</h2><ul>' + notes + '</ul>') if notes else ''}
{('<h2>Details deliberately not modelled (unverified or conflicting sources)</h2><ul>' + unverified + '</ul>') if unverified else ''}
<h2>Official source</h2>
<p>{_e(src['title'])}<br>Verification status of this rule: <b>{_e(src['verification'])}</b>.
Official site: {_e(src.get('primary_url') or 'not yet located')} ({_e(src.get('primary_url_status'))}). Confirm current terms there before applying.</p>
<p class="small">Facts digest {_e(data['facts_digest'])}. Values marked user_declared were typed in by a person and are not verified by this platform.</p>
</body></html>"""


def summary_pdf(data: dict) -> bytes:
    from weasyprint import HTML

    return HTML(string=summary_html(data), url_fetcher=_deny_fetch).write_pdf()

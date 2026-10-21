"""Markdown (for the README) and standalone HTML (with inline SVG charts) reports."""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from .ensemble import THRESHOLD
from .evaluate import Report


def _f(x, d=3):
    return "n/a" if x is None else (f"{x:.{d}f}" if isinstance(x, float) else str(x))


def markdown(r: Report) -> str:
    o, nv = r.overall, r.naive
    out = ["## Measured results", "",
           f"Seed {r.seed}: {r.points:,} points over three weeks, six services, "
           f"{len(r.incidents)} injected incidents and {sum(v['events'] for v in r.benign_events.values())} "
           f"labeled benign events. {r.queued} candidates queued ({r.noise['candidates_per_day']}/day), "
           f"{r.held_out} held out of the queue as change-correlated or recurring.", "",
           "| | Ensemble | Naive z-score |", "|---|---:|---:|",
           f"| Incidents detected | {len([i for i in r.incidents if i.detected])} / {len(r.incidents)} | "
           f"{len(r.incidents) - nv['fn']} / {len(r.incidents)} |",
           f"| False positives | {o['fp']} | {nv['fp']} |",
           f"| Precision | {_f(o['precision'])} | {_f(nv['precision'])} |",
           f"| Recall | {_f(o['recall'])} | {_f(nv['recall'])} |",
           f"| F1 | {_f(o['f1'])} | {_f(nv['f1'])} |",
           f"| Median detection latency | {_f(o['median_latency_min'], 1)} min | — |",
           f"| Shape classified correctly | {_f(o['shape_accuracy'] * 100, 0)}% | — |", "",
           "### By incident type", "", "| Type | Incidents | Detected | Median latency |", "|---|---:|---:|---:|"]
    for k, d in sorted(r.by_kind.items()):
        out.append(f"| {k} | {d['incidents']} | {d['detected']} | {_f(d['median_latency_min'], 1)} min |")
    out += ["", "### Benign events", "",
            "| Event | Occurrences | Expected handling | Queued | Queued wrongly | Held out |",
            "|---|---:|---|---:|---:|---:|"]
    for k, d in sorted(r.benign_events.items()):
        out.append(f"| {k} | {d['events']} | {d['expectation']} | {d['queued']} | {d['queued_wrongly']} | {d['held_out']} |")
    out += ["", "### Operating point", "",
            f"Precision / recall across fused-score thresholds (the lab runs at {THRESHOLD}):", "",
            "| Threshold | Candidates | Precision | Recall | F1 |", "|---:|---:|---:|---:|---:|"]
    for s in r.sweep:
        out.append(f"| {s['threshold']} | {s['candidates']} | {_f(s['precision'])} | {_f(s['recall'])} | {_f(s['f1'])} |")
    q = r.queue_quality
    out += ["", "### Queue quality", "",
            f"- Top 5 of the queue: {q['top5_true_incidents']} real incidents.",
            f"- First false positive appears at rank {q['first_false_positive_rank'] or 'none'}.",
            f"- Critical services in the top 3: {q['critical_services_in_top3']}.",
            f"- Every candidate is advisory (`requires_human_review`, no `auto_action_taken`): "
            f"{'yes' if r.noise['advisory_only'] else 'NO'}.", ""]
    return "\n".join(out)


def _svg_pr(sweep: list[dict], naive: dict) -> str:
    if not sweep:
        return ""
    w, h, pad = 420, 260, 36
    pts = [(s["recall"] or 0, s["precision"] or 0, s["threshold"]) for s in sweep]

    def x(v):
        return pad + v * (w - 2 * pad)

    def y(v):
        return h - pad - v * (h - 2 * pad)
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(r):.1f},{y(p):.1f}" for i, (r, p, _) in enumerate(pts))
    dots = "".join(f'<circle cx="{x(r):.1f}" cy="{y(p):.1f}" r="4" fill="#0969da"><title>thr {t}: '
                   f'P {p:.2f} R {r:.2f}</title></circle>' for r, p, t in pts)
    nx, ny = x(naive.get("recall") or 0), y(naive.get("precision") or 0)
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="precision-recall">'
            f'<rect width="{w}" height="{h}" fill="#fff"/>'
            f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="#999"/>'
            f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{h - pad}" stroke="#999"/>'
            f'<text x="{w / 2}" y="{h - 8}" font-size="11" text-anchor="middle">recall</text>'
            f'<text x="12" y="{h / 2}" font-size="11" transform="rotate(-90 12 {h / 2})" text-anchor="middle">precision</text>'
            f'<path d="{path}" fill="none" stroke="#0969da" stroke-width="2"/>{dots}'
            f'<rect x="{nx - 5}" y="{ny - 5}" width="10" height="10" fill="#cf222e"><title>naive z-score</title></rect>'
            f'<text x="{nx + 8}" y="{ny + 4}" font-size="10" fill="#cf222e">naive</text></svg>')


def html_page(r: Report, candidates: list[dict]) -> str:
    esc = lambda s: html.escape(str(s))  # noqa: E731
    inc_rows = "".join(
        f"<tr class='{'ok' if i.detected else 'miss'}'><td>{esc(i.id)}</td><td>{esc(i.service)}</td>"
        f"<td>{esc(i.kind)}</td><td>{'yes' if i.detected else 'MISSED'}</td><td>{_f(i.latency_min, 1)}</td>"
        f"<td>{esc(i.shape_reported or '')}</td><td>{esc(i.severity or '')}</td></tr>" for i in r.incidents)
    cand_rows = "".join(
        f"<tr><td>{k + 1}</td><td>{esc(c['service'])}</td><td class='{esc(c['severity'])}'>{esc(c['severity'])}</td>"
        f"<td>{c['priority']:.2f}</td><td>{esc(c['shape'])}</td><td>{c['duration_min']}</td>"
        f"<td>{esc(', '.join(c['metrics']))}</td><td>{esc(c['recommended_next_step'][:90])}…</td></tr>"
        for k, c in enumerate(candidates[:20]))
    o = r.overall
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Log Anomaly Triage report</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem auto;max-width:1100px;padding:0 1rem;color:#1b1f24}}
table{{border-collapse:collapse;width:100%;font-size:.9rem;margin:.6rem 0}}th,td{{border:1px solid #d0d7de;padding:.35rem .55rem;text-align:left}}
th{{background:#f6f8fa}}.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.6rem}}
.kpi div{{border:1px solid #d0d7de;border-radius:8px;padding:.6rem .8rem}}.kpi b{{display:block;font-size:1.4rem}}
tr.miss td{{background:#ffebe9}}td.critical{{color:#a40e26;font-weight:600}}td.high{{color:#bc4c00;font-weight:600}}
small{{color:#57606a}}</style></head><body>
<h1>Log Anomaly Triage — evaluation report</h1>
<small>Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · seed {r.seed} · synthetic data only</small>
<div class="kpi"><div><b>{r.points:,}</b>points</div><div><b>{r.queued}</b>queued · {r.held_out} held</div>
<div><b>{_f(o['precision'])}</b>precision</div><div><b>{_f(o['recall'])}</b>recall</div>
<div><b>{_f(o['median_latency_min'], 1)} min</b>median latency</div>
<div><b>{_f(r.naive['precision'])} / {_f(r.naive['recall'])}</b>naive P / R</div></div>
<h2>Precision–recall across thresholds</h2>{_svg_pr(r.sweep, r.naive)}
<h2>Incidents</h2><table><tr><th>ID</th><th>Service</th><th>Type</th><th>Detected</th><th>Latency (min)</th><th>Shape</th><th>Severity</th></tr>{inc_rows}</table>
<h2>Queue (top 20)</h2><table><tr><th>#</th><th>Service</th><th>Severity</th><th>Priority</th><th>Shape</th><th>Min</th><th>Metrics</th><th>Next step</th></tr>{cand_rows}</table>
<h2>Raw</h2><pre>{esc(json.dumps({'overall': o, 'naive': r.naive, 'queue_quality': r.queue_quality, 'benign': r.benign_events}, indent=1))}</pre>
</body></html>"""

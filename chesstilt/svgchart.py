"""Tiny dependency-free SVG charts for the HTML report.

Colors come from CSS custom properties defined in the report page, so every
chart follows the page's light/dark theme. Each mark carries data-tip-*
attributes for the hover/focus tooltip, and every chart has a table view.
"""
from __future__ import annotations

import html
import math

_uid = [0]


def _id(prefix="c"):
    _uid[0] += 1
    return f"{prefix}{_uid[0]}"


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def nice_ticks(lo: float, hi: float, n: int = 5):
    if not (math.isfinite(lo) and math.isfinite(hi)):
        lo, hi = -1.0, 1.0
    if hi - lo < 1e-12:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / max(1, n)
    mag = 10 ** math.floor(math.log10(raw))
    step = mag
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            step = m * mag
            break
    start = math.floor(lo / step) * step
    end = math.ceil(hi / step) * step
    ticks = []
    v = start
    while v <= end + step * 1e-6:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _fmt_tick(v: float, step: float) -> str:
    dec = 0 if step >= 1 else (1 if step >= 0.1 else 2)
    s = f"{v:.{dec}f}"
    if s.startswith("-") and float(s) == 0:
        s = s[1:]
    return s.replace("-", "−")


def _tip(value: str, label: str) -> str:
    return f'tabindex="0" data-tip-value="{esc(value)}" data-tip-label="{esc(label)}"'


def table_view(headers, rows) -> str:
    th = "".join(f"<th>{esc(h)}</th>" for h in headers)
    trs = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return (f'<details class="tv"><summary>Show as table</summary>'
            f'<div class="tablewrap"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div></details>')


def forest(rows, *, unit="points", fmt="{:+.1f}", width=720, label_w=230, row_h=34,
           note_col=None, zero_label="no difference") -> str:
    """Horizontal estimates with 95% intervals, one row per item.

    rows: dicts with label, est, lo, hi (already in display units), optional n/note.
    """
    rows = [r for r in rows if r.get("est") is not None and math.isfinite(r["est"])]
    if not rows:
        return '<p class="muted">Not enough data for this chart.</p>'
    val_w = 120
    pad_t, pad_b = 8, 36
    plot_x0, plot_x1 = label_w, width - val_w
    h = pad_t + row_h * len(rows) + pad_b
    lo = min(min(r["lo"] for r in rows), 0.0)
    hi = max(max(r["hi"] for r in rows), 0.0)
    span = hi - lo
    lo -= span * 0.06 + 1e-9
    hi += span * 0.06 + 1e-9
    ticks = nice_ticks(lo, hi, 5)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
    step = ticks[1] - ticks[0] if len(ticks) > 1 else 1

    def X(v):
        return plot_x0 + (v - lo) / (hi - lo) * (plot_x1 - plot_x0)

    out = [f'<svg class="chart" viewBox="0 0 {width} {h}" role="img" aria-label="Estimates with 95% intervals">']
    y_axis = pad_t + row_h * len(rows)
    for t in ticks:
        x = X(t)
        out.append(f'<line class="grid" x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{y_axis}"/>')
        out.append(f'<text class="tick" x="{x:.1f}" y="{y_axis + 16}" text-anchor="middle">{_fmt_tick(t, step)}</text>')
    out.append(f'<line class="zero" x1="{X(0):.1f}" y1="{pad_t - 4}" x2="{X(0):.1f}" y2="{y_axis}"/>')
    out.append(f'<text class="axis-title" x="{(plot_x0 + plot_x1) / 2:.1f}" y="{h - 4}" text-anchor="middle">{esc(unit)}</text>')
    for i, r in enumerate(rows):
        cy = pad_t + row_h * i + row_h / 2
        tip_val = f"{fmt.format(r['est'])} {unit} (95% range {fmt.format(r['lo'])} to {fmt.format(r['hi'])})"
        tip_lab = r["label"] + (f" · {r['n']:,} games" if r.get("n") else "")
        cls = "s1" if not r.get("muted") else "s0"
        out.append(f'<g class="mark" {_tip(tip_val, tip_lab)}>')
        out.append(f'<rect class="hit" x="0" y="{cy - row_h / 2:.1f}" width="{width}" height="{row_h}"/>')
        out.append(f'<text class="rowlabel" x="{label_w - 12}" y="{cy + 4:.1f}" text-anchor="end">{esc(r["label"])}</text>')
        out.append(f'<line class="ci {cls}" x1="{X(r["lo"]):.1f}" y1="{cy:.1f}" x2="{X(r["hi"]):.1f}" y2="{cy:.1f}"/>')
        out.append(f'<circle class="dot {cls}" cx="{X(r["est"]):.1f}" cy="{cy:.1f}" r="5"/>')
        out.append(f'<text class="val" x="{width - 8}" y="{cy + 4:.1f}" text-anchor="end">{esc(fmt.format(r["est"]).replace("-", chr(8722)))}'
                   f'<tspan class="muted"> ±{(r["hi"] - r["lo"]) / 2:.1f}</tspan></text>')
        out.append("</g>")
    out.append("</svg>")
    table = table_view(["Item", f"Estimate ({unit})", "95% range", "Games"],
                       [(r["label"], fmt.format(r["est"]), f"{fmt.format(r['lo'])} to {fmt.format(r['hi'])}",
                         f"{r['n']:,}" if r.get("n") else "") for r in rows])
    return "".join(out) + table


def _wrap(label: str, max_px: float):
    """Split a label into at most two lines that fit max_px (about 6.6 px per character)."""
    if len(label) * 6.6 <= max_px:
        return [label]
    best = None
    for i, ch in enumerate(label):
        if ch in " -":
            a, b = label[: i + 1].rstrip(), label[i + 1:].lstrip()
            w = max(len(a), len(b))
            if best is None or w < best[0]:
                best = (w, [a, b])
    return best[1] if best else [label]


def line(series, xlabels, *, unit="points", fmt="{:+.1f}", width=720, height=300, zero=True,
         markers_only=False, xtitle="", band=False, ylim=None) -> str:
    """Line chart over ordered categories with optional 95% whiskers or band.

    series: list of dicts: name, slot (1..3 or 0 for gray), points = [(i, est, lo, hi) | (i, est)].
    """
    pts_all = [p for s in series for p in s["points"] if p[1] is not None and math.isfinite(p[1])]
    if not pts_all:
        return '<p class="muted">Not enough data for this chart.</p>'
    n = len(xlabels)
    slot_px = (width - 72) / max(1, n)
    wrapped = [_wrap(str(lab), slot_px - 6) for lab in xlabels]
    two_lines = any(len(w) > 1 for w in wrapped)
    too_wide = any(len(part) * 6.6 > slot_px - 2 for w in wrapped for part in w)
    pad_l, pad_r, pad_t = 56, 16, 30
    pad_b = (46 if xtitle else 30) + (14 if two_lines else 0)
    height += 14 if two_lines else 0
    vals = [p[1] for p in pts_all] + [p[2] for p in pts_all if len(p) > 3] + [p[3] for p in pts_all if len(p) > 3]
    if zero:
        vals.append(0.0)
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    ticks = nice_ticks(lo - span * 0.05, hi + span * 0.05, 5)
    if ylim:
        ticks = [t for t in nice_ticks(ylim[0], ylim[1], 5) if ylim[0] - 1e-9 <= t <= ylim[1] + 1e-9]
    lo, hi = ticks[0], ticks[-1]
    step = ticks[1] - ticks[0] if len(ticks) > 1 else 1
    x0, x1 = pad_l, width - pad_r
    y0, y1 = pad_t, height - pad_b
    slot_w = (x1 - x0) / n

    def X(i):
        return x0 + slot_w * (i + 0.5)

    def Y(v):
        return y1 - (v - lo) / (hi - lo) * (y1 - y0)

    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Line chart">']
    for t in ticks:
        y = Y(t)
        out.append(f'<line class="grid" x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}"/>')
        out.append(f'<text class="tick" x="{x0 - 8}" y="{y + 4:.1f}" text-anchor="end">{_fmt_tick(t, step)}</text>')
    if zero and lo <= 0 <= hi:
        out.append(f'<line class="zero" x1="{x0}" y1="{Y(0):.1f}" x2="{x1}" y2="{Y(0):.1f}"/>')
    every = 2 if too_wide else max(1, math.ceil(n / 12))
    for i, parts in enumerate(wrapped):
        if i % every == 0:
            spans = "".join(f'<tspan x="{X(i):.1f}" dy="{0 if j == 0 else 14}">{esc(t)}</tspan>'
                            for j, t in enumerate(parts))
            out.append(f'<text class="tick" x="{X(i):.1f}" y="{y1 + 18}" text-anchor="middle">{spans}</text>')
    if xtitle:
        out.append(f'<text class="axis-title" x="{(x0 + x1) / 2:.1f}" y="{height - 6}" text-anchor="middle">{esc(xtitle)}</text>')
    out.append(f'<text class="axis-title" x="4" y="14" text-anchor="start">{esc(unit)}</text>')
    for s in series:
        cls = f"s{s.get('slot', 1)}"
        pts = [p for p in s["points"] if p[1] is not None and math.isfinite(p[1])]
        if band and pts and len(pts[0]) > 3:
            up = " ".join(f"{X(p[0]):.1f},{Y(p[3]):.1f}" for p in pts)
            dn = " ".join(f"{X(p[0]):.1f},{Y(p[2]):.1f}" for p in reversed(pts))
            out.append(f'<polygon class="band {cls}" points="{up} {dn}"/>')
        elif pts and len(pts[0]) > 3:
            for p in pts:
                out.append(f'<line class="ci {cls}" x1="{X(p[0]):.1f}" y1="{Y(p[2]):.1f}" x2="{X(p[0]):.1f}" y2="{Y(p[3]):.1f}"/>')
        if not markers_only and len(pts) > 1:
            # Break the line where categories are missing.
            segs, cur = [], [pts[0]]
            for a, b in zip(pts, pts[1:]):
                if b[0] - a[0] == 1 and not s.get("break_at") == b[0]:
                    cur.append(b)
                else:
                    segs.append(cur)
                    cur = [b]
            segs.append(cur)
            for seg in segs:
                if len(seg) > 1:
                    d = " ".join(f"{X(p[0]):.1f},{Y(p[1]):.1f}" for p in seg)
                    out.append(f'<polyline class="ln {cls}" points="{d}"/>')
        for p in pts:
            rng = f" (95% range {fmt.format(p[2])} to {fmt.format(p[3])})" if len(p) > 3 else ""
            tv = f"{fmt.format(p[1])} {unit}{rng}"
            tl = f"{s['name']} · {xlabels[p[0]]}" if s.get("name") else xlabels[p[0]]
            out.append(f'<g class="mark" {_tip(tv, tl)}><rect class="hit" x="{X(p[0]) - slot_w / 2:.1f}" y="{y0}" '
                       f'width="{slot_w:.1f}" height="{y1 - y0}"/>'
                       f'<circle class="dot {cls}" cx="{X(p[0]):.1f}" cy="{Y(p[1]):.1f}" r="4.5"/></g>')
    out.append("</svg>")
    legend = ""
    named = [s for s in series if s.get("name")]
    if len(named) >= 2:
        items = "".join(f'<span class="key"><i class="sw s{s.get("slot", 1)}"></i>{esc(s["name"])}</span>' for s in named)
        legend = f'<div class="legend">{items}</div>'
    headers = ["Category"] + [s.get("name") or "Value" for s in series]
    rows = []
    for i, lab in enumerate(xlabels):
        row = [lab]
        for s in series:
            p = next((p for p in s["points"] if p[0] == i), None)
            if p is None or p[1] is None:
                row.append("")
            elif len(p) > 3:
                row.append(f"{fmt.format(p[1])} ({fmt.format(p[2])} to {fmt.format(p[3])})")
            else:
                row.append(fmt.format(p[1]))
        rows.append(row)
    return legend + "".join(out) + table_view(headers, rows)

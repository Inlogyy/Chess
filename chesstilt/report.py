"""Build the HTML report (one self-contained file) from the analysis results."""
from __future__ import annotations

import html
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from . import svgchart as C

Z = 1.96
SPEED_ORDER = ["UltraBullet", "Bullet", "Blitz", "Rapid", "Classical"]


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def _ok(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


def pts(x):
    """Score fraction -> percentage points."""
    return None if not _ok(x) else 100.0 * x


def sgn(x, dec=1) -> str:
    if not _ok(x):
        return "n/a"
    s = f"{x:+.{dec}f}"
    if float(s) == 0:
        s = f"{0:.{dec}f}"
    return s.replace("-", "−")


def num(x, dec=0) -> str:
    if not _ok(x):
        return "n/a"
    return f"{x:,.{dec}f}"


def ci(est, se, scale=100.0):
    if not (_ok(est) and _ok(se)):
        return None, None, None
    return est * scale, (est - Z * se) * scale, (est + Z * se) * scale


def clear(est, se) -> bool:
    return _ok(est) and _ok(se) and se > 0 and abs(est) > Z * se


def _row(rows, by="All", grp="L"):
    for r in rows or []:
        if r.get("by") == by and r.get("grp") == grp:
            return r
    return None


def _by(rows, by="All"):
    for r in rows or []:
        if r.get("by") == by:
            return r
    return None


def _diff_point(r, label, est_key="diff", se_key="se_diff", n_key="n"):
    if not r:
        return None
    e, lo, hi = ci(r.get(est_key), r.get(se_key))
    if e is None:
        return None
    return {"label": label, "est": e, "lo": lo, "hi": hi, "n": r.get(n_key)}


def _section(title, body, sid):
    return f'<section id="{sid}"><h2>{esc(title)}</h2>{body}</section>'


def _p(text):
    return f"<p>{text}</p>"


def _phrase_gap(d, se, elo_per_pt):
    """Plain-language sentence for an (after loss - after win) difference."""
    e, lo, hi = ci(d, se)
    if e is None:
        return "There is not enough data to compare games after losses and after wins."
    rng = f"(95% range {sgn(lo)} to {sgn(hi)} points)"
    if not clear(d, se):
        return (f"Games after a loss and games after a win came out about the same: "
                f"{sgn(e)} points {rng}. That is not distinguishable from zero.")
    elo = abs(e) / elo_per_pt if elo_per_pt else None
    elo_txt = f" That is about what you would expect from a player <b>{elo:.0f} rating points weaker</b>." if elo else ""
    if e < 0:
        return (f"In the next game after a loss, players scored <b>{abs(e):.1f} percentage points less</b> "
                f"than their ratings predicted, compared with the next game after a win {rng}.{elo_txt}")
    return (f"In the next game after a loss, players scored <b>{e:.1f} percentage points more</b> "
            f"than after a win {rng}, which is the opposite of tilt.")


CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#6f6d68;
--grid:#e1e0d9;--axis:#c3c2b7;--zero:#898781;--border:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;
--s0:#898781;--good:#006300;--bad:#c23534;--tip:#0b0b0b;--tipink:#f0efec}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--page:#0d0d0d;
--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#9a988f;--grid:#2c2c2a;--axis:#383835;--zero:#898781;
--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s0:#898781;--good:#0ca30c;--bad:#e66767;
--tip:#f0efec;--tipink:#0b0b0b}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--muted:#9a988f;--grid:#2c2c2a;--axis:#383835;--zero:#898781;--border:rgba(255,255,255,.10);--s1:#3987e5;
--s2:#d95926;--s3:#199e70;--s0:#898781;--good:#0ca30c;--bad:#e66767;--tip:#f0efec;--tipink:#0b0b0b}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:960px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:30px;line-height:1.2;margin:0 0 6px}
h2{font-size:21px;margin:44px 0 10px;padding-top:8px}
h3{font-size:16px;margin:22px 0 6px}
p{margin:8px 0;max-width:72ch}
.sub{color:var(--ink2);margin:0 0 20px}
.muted{color:var(--muted)}
nav{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:14px;margin:6px 0 10px}
nav a{color:var(--ink2)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:22px 0 8px}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.kpi .label{font-size:14px;color:var(--ink2)}
.kpi .value{font-size:30px;font-weight:600;margin:4px 0 2px}
.kpi .note{font-size:13px;color:var(--muted)}
figure{margin:14px 0 6px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 14px 8px}
figcaption{font-weight:600;margin:0 0 2px}
figure .cap2{font-size:14px;color:var(--ink2);margin:0 0 8px}
svg.chart{width:100%;height:auto;display:block;overflow:visible}
svg text{fill:var(--ink2);font-size:12px;font-family:inherit}
svg .tick{fill:var(--muted);font-variant-numeric:tabular-nums}
svg .axis-title{fill:var(--muted);font-size:12px}
svg .rowlabel{fill:var(--ink);font-size:13px}
svg .val{fill:var(--ink);font-size:13px;font-variant-numeric:tabular-nums}
svg .val .muted{fill:var(--muted)}
svg .grid{stroke:var(--grid);stroke-width:1}
svg .zero{stroke:var(--zero);stroke-width:1}
svg .ln{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg .ci{stroke-width:2;stroke-linecap:round;opacity:.45}
svg .band{opacity:.12;stroke:none}
svg .dot{stroke:var(--surface);stroke-width:2}
svg .hit{fill:transparent}
svg .mark:focus{outline:none}
svg .mark:hover .dot,svg .mark:focus .dot{stroke:var(--ink);stroke-width:1.5}
svg .mark:hover .hit,svg .mark:focus .hit{fill:var(--grid);opacity:.35}
.s1{stroke:var(--s1);fill:var(--s1)}.s2{stroke:var(--s2);fill:var(--s2)}.s3{stroke:var(--s3);fill:var(--s3)}
.s0{stroke:var(--s0);fill:var(--s0)}
svg polyline.s1,svg polyline.s2,svg polyline.s3,svg polyline.s0{fill:none}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--ink2);margin:2px 0 6px}
.key{display:inline-flex;align-items:center;gap:6px}
.sw{display:inline-block;width:14px;height:3px;border-radius:2px;background:var(--s1)}
.sw.s2{background:var(--s2)}.sw.s3{background:var(--s3)}.sw.s0{background:var(--s0)}
details.tv{margin:6px 0 4px;font-size:14px}
details.tv summary{cursor:pointer;color:var(--ink2)}
.tablewrap{overflow-x:auto;max-width:100%}
table{border-collapse:collapse;font-size:14px;margin:8px 0;width:100%}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--grid);vertical-align:top}
td{font-variant-numeric:tabular-nums}
th{color:var(--ink2);font-weight:600}
.good{color:var(--good)}.bad{color:var(--bad)}
.callout{background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--s1);border-radius:8px;padding:10px 14px;margin:12px 0}
ul{padding-left:22px;margin:6px 0}li{margin:4px 0;max-width:72ch}
code{font-size:13px;background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:1px 4px}
#tip{position:fixed;pointer-events:none;background:var(--tip);color:var(--tipink);border-radius:6px;padding:6px 9px;
font-size:13px;line-height:1.35;max-width:300px;display:none;z-index:9;box-shadow:0 2px 8px rgba(0,0,0,.18)}
#tip b{display:block;font-size:14px}
.theme{float:right;font-size:13px;background:none;border:1px solid var(--border);color:var(--ink2);border-radius:6px;padding:3px 8px;cursor:pointer}
@media (max-width:560px){h1{font-size:24px}.kpi .value{font-size:26px}figure{overflow-x:auto}svg.chart{min-width:600px}}
"""

JS = """
(function(){
  var tip=document.getElementById('tip');
  function show(el,x,y){
    tip.textContent='';
    var b=document.createElement('b');b.textContent=el.getAttribute('data-tip-value');
    var s=document.createElement('span');s.textContent=el.getAttribute('data-tip-label');
    tip.appendChild(b);tip.appendChild(s);tip.style.display='block';
    var w=tip.offsetWidth,h=tip.offsetHeight;
    var left=Math.min(window.innerWidth-w-8,Math.max(8,x+14));
    var top=y-h-12<8?y+16:y-h-12;
    tip.style.left=left+'px';tip.style.top=top+'px';
  }
  function hide(){tip.style.display='none';}
  document.querySelectorAll('[data-tip-value]').forEach(function(el){
    el.addEventListener('pointermove',function(e){show(el,e.clientX,e.clientY);});
    el.addEventListener('pointerleave',hide);
    el.addEventListener('focus',function(){var r=el.getBoundingClientRect();show(el,r.left+r.width/2,r.top+r.height/2);});
    el.addEventListener('blur',hide);
  });
  var btn=document.getElementById('theme');
  if(btn){btn.addEventListener('click',function(){
    var root=document.documentElement;
    var dark=root.getAttribute('data-theme')==='dark'||(!root.getAttribute('data-theme')&&window.matchMedia('(prefers-color-scheme: dark)').matches);
    root.setAttribute('data-theme',dark?'light':'dark');
  });}
})();
"""


def _fig(title, caption, chart_html):
    cap2 = f'<p class="cap2">{caption}</p>' if caption else ""
    return f"<figure><figcaption>{esc(title)}</figcaption>{cap2}{chart_html}</figure>"


def _table(headers, rows, cls=""):
    th = "".join(f"<th>{h}</th>" for h in headers)
    trs = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="tablewrap"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


def _cmp_cell(r, scale=100.0, dec=1, good_if="neg"):
    """'diff (lo to hi)' with colour when the difference is clear."""
    if not r or not _ok(r.get("diff")):
        return "n/a"
    e, lo, hi = ci(r["diff"], r.get("se_diff"), scale)
    txt = f"{sgn(e, dec)} <span class='muted'>({sgn(lo, dec)} to {sgn(hi, dec)})</span>"
    return txt


def build_report(R: dict, out_file: Path, title_note: str = "") -> Path:
    ov = R.get("overview", {})
    slope = ov.get("mean_slope") or 0.00144
    elo_per_pt = slope * 100.0  # percentage points per rating point
    head_all = _row(R.get("headline"), "All", "L")
    d_all = head_all.get("diff") if head_all else None
    se_all = head_all.get("se_diff") if head_all else None
    ss = _by(R.get("robust", {}).get("same_sitting"), "All")
    qacc = _row(R.get("quality", {}).get("accuracy"), "All", "L")

    t_min = datetime.fromtimestamp(ov["t_min"], tz=timezone.utc).strftime("%d %b %Y") if ov.get("t_min") else "?"
    t_max = datetime.fromtimestamp(ov["t_max"], tz=timezone.utc).strftime("%d %b %Y") if ov.get("t_max") else "?"
    games = ov.get("games", 0)
    files = R.get("extract", {}).get("files", [])
    parts = []

    # ---------------------------------------------------------------- header & KPIs
    parts.append('<button class="theme" id="theme" type="button">Light / dark</button>')
    parts.append("<h1>Post-loss tilt</h1>")
    src = ", ".join(Path(f).name for f in files) if files else "your PGN files"
    parts.append(f'<p class="sub">{num(games)} rated Lichess games from {esc(src)} · {t_min} to {t_max}'
                 f'{esc(title_note)}</p>')
    parts.append('<nav><a href="#s1">Headline</a><a href="#s2">Tilt or bad day?</a><a href="#s3">Streaks</a>'
                 '<a href="#s4">Cooling off</a><a href="#s5">Kinds of losses</a><a href="#s6">By rating</a>'
                 '<a href="#s7">Move quality</a><a href="#s8">Behaviour</a><a href="#s9">Data &amp; method</a></nav>')
    kpis = []
    e, lo, hi = ci(d_all, se_all)
    kpis.append(("Next game after a loss", f"{sgn(e)} pts" if e is not None else "n/a",
                 "score vs. ratings, compared with after a win"))
    if e is not None and elo_per_pt:
        kpis.append(("Rating-point equivalent", f"{sgn(e / elo_per_pt, 0)} Elo",
                     f"like playing that much {'weaker' if e < 0 else 'stronger'}"))
    if ss:
        e2, _, _ = ci(ss.get("est"), ss.get("se"))
        kpis.append(("Tilt beyond a bad day", f"{sgn(e2)} pts", "same sitting, vs. the same games in random order"))
    if qacc and _ok(qacc.get("diff")):
        kpis.append(("Accuracy after a loss", f"{sgn(qacc['diff'], 1)} %",
                     "Lichess-style accuracy vs. after a win"))
    parts.append('<div class="kpis">' + "".join(
        f'<div class="kpi"><div class="label">{esc(a)}</div><div class="value">{esc(b)}</div>'
        f'<div class="note">{esc(c)}</div></div>' for a, b, c in kpis) + "</div>")
    parts.append(f'<div class="callout">{_phrase_gap(d_all, se_all, elo_per_pt)} '
                 f'"Points" are percentage points of score (win = 1, draw = ½, loss = 0), measured against '
                 f'the score the two ratings predict.</div>')

    # ---------------------------------------------------------------- 1 headline
    rows = []
    for sp in SPEED_ORDER + ["All"]:
        pt = _diff_point(_row(R.get("headline"), sp, "L"), "All speeds" if sp == "All" else sp)
        if pt:
            rows.append(pt)
    body = _p("Each row compares a player's <b>next game after a loss</b> with their <b>next game after a win</b>, "
              "in the same sitting (the next game started within "
              f"{esc(R['config'].get('session_gap_minutes', 30))} minutes). Below zero means worse after a loss.")
    body += _fig("Next-game score after a loss, compared with after a win", "Points vs. the rating prediction; "
                 "dot = estimate, line = 95% range.", C.forest(rows, unit="points"))
    lvl_rows = []
    for sp in SPEED_ORDER + ["All"]:
        cells = []
        for g in ("L", "D", "W"):
            r = _row(R.get("headline"), sp, g)
            cells.append(f"{sgn(pts(r['mean']))} <span class='muted'>({num(r['n'])})</span>" if r else "n/a")
        wr = _row(R.get("outcomes", {}).get("win"), sp, "L")
        ww = _row(R.get("outcomes", {}).get("win"), sp, "W")
        ex_l = _row(R.get("outcomes", {}).get("expected"), sp, "L")
        ex_w = _row(R.get("outcomes", {}).get("expected"), sp, "W")
        if not _row(R.get("headline"), sp, "L"):
            continue
        lvl_rows.append([("All speeds" if sp == "All" else sp)] + cells + [
            f"{num(pts(wr['mean']), 1)}% / {num(pts(ww['mean']), 1)}%" if wr and ww else "n/a",
            f"{num(pts(ex_l['mean']), 1)}% / {num(pts(ex_w['mean']), 1)}%" if ex_l and ex_w else "n/a"])
    body += "<h3>Score vs. prediction after each result</h3>"
    body += _table(["Speed", "After a loss", "After a draw", "After a win", "Win rate (after L / W)",
                    "Predicted score (after L / W)"], lvl_rows)
    body += _p("<span class='muted'>Numbers in brackets are games. After a loss your rating drops, so Lichess pairs "
               "you with slightly weaker opponents; the prediction already accounts for that.</span>")
    parts.append(_section("1. How much worse after a loss?", body, "s1"))

    # ---------------------------------------------------------------- 2 robustness
    rob = R.get("robust", {})
    lk = R.get("luck", {})
    rrows = []
    for r, label, ek, sk in (
            (head_all, "Simple comparison (section 1)", "diff", "se_diff"),
            (_row(rob.get("player_adjusted"), "All", "L"), "Each player vs. their own average", "diff", "se_diff"),
            (_by(rob.get("controlled"), "All"), "Like-for-like games only", "est", "se"),
            (ss, "Same sitting vs. random order", "est", "se"),
            ((lk.get("raw") or [None])[0], "Engine-analysed losses: simple comparison", "diff", "se_diff"),
            (_by(lk.get("controlled"), "All"), "Engine-analysed losses: same accuracy", "est", "se")):
        pt = _diff_point(r, label, ek, sk)
        if pt:
            rrows.append(pt)
    body = _p("Losses cluster even without tilt. Someone who is tired, distracted or off form loses more, and keeps "
              "playing worse whether or not the loss upset them. The checks below separate the two:")
    body += ("<ul><li><b>Each player vs. their own average</b> removes differences between players "
             "(for example, improving players who win a lot and are under-rated).</li>"
             "<li><b>Like-for-like</b> compares only games with the same speed, rating band, game number in the "
             "sitting and event type.</li>"
             "<li><b>Same sitting vs. random order</b> is the main bad-day test. A bad day affects every game in a "
             "sitting, so the program asks whether a loss is followed by a worse <i>next</i> game than by a random "
             "other game from the same sitting. Anything that stays the same through the sitting cancels out.</li>"
             "<li><b>Same accuracy</b> compares losses and wins in which the player played equally well (engine-"
             "analysed games only), so the result was mostly down to the opponent. Compare it with the simple "
             "comparison on the same engine-analysed games, the row above it.</li></ul>")
    body += _fig("The post-loss gap, measured six ways", "Points; below zero = worse after a loss than after a win.",
                 C.forest(rrows, unit="points"))
    if ss:
        e3, lo3, hi3 = ci(ss.get("est"), ss.get("se"))
        simple = pts(d_all)
        if clear(ss.get("est"), ss.get("se")) and e3 < 0:
            msg = (f"Within the same sitting, a loss is still followed by a game <b>{abs(e3):.1f} points</b> worse "
                   f"(95% range {sgn(lo3)} to {sgn(hi3)}) than a win is. A bad day cannot explain that part, so it is "
                   f"the best estimate of tilt itself")
            if simple is not None and simple < 0 and 0 < e3 / simple <= 1.5:
                msg += f", about {min(e3 / simple, 1) * 100:.0f}% of the simple gap"
            body += _p(msg + ". Two small biases pull in opposite directions: tilt that lasts several games is "
                       "partly cancelled (so it can understate tilt), while form that changes <i>during</i> a sitting "
                       "is not (so it can overstate it). In tests on simulated games it slightly understated tilt. "
                       "The true effect of the loss itself most likely lies between this and the simple comparison.")
        elif e3 is not None:
            body += _p(f"Within the same sitting the gap is {sgn(e3)} points (95% range {sgn(lo3)} to {sgn(hi3)}). "
                       "If this is near zero while the simple comparison is clearly negative, the post-loss gap is "
                       "mostly players being off form for the whole sitting, not the loss itself upsetting them.")
    if lk.get("raw"):
        lr = lk["raw"][0]
        body += _p(f"<span class='muted'>The engine-analysed rows use {num(lr.get('n'))} games after a loss whose "
                   "previous game had Stockfish evaluations.</span>")
    parts.append(_section("2. Is it tilt, or just a bad day?", body, "s2"))

    # ---------------------------------------------------------------- 3 streaks
    order = ["L5", "L4", "L3", "L2", "L1", "D", "W1", "W2", "W3", "W4", "W5"]
    lab = {"L5": "5+ losses", "L4": "4 losses", "L3": "3 losses", "L2": "2 losses", "L1": "1 loss", "D": "a draw",
           "W1": "1 win", "W2": "2 wins", "W3": "3 wins", "W4": "4 wins", "W5": "5+ wins"}
    st = {r["grp"]: r for r in R.get("streak", [])}
    points = []
    for i, g in enumerate(order):
        r = st.get(g)
        if r and _ok(r.get("mean")):
            e6, lo6, hi6 = ci(r["mean"], r["se"])
            points.append((i, e6, lo6, hi6))
    body = _p("Score vs. prediction in the next game, by what happened just before in the same sitting.")
    body += _fig("Next game after a run of losses or wins", "Points vs. the rating prediction (0 = played to "
                 "rating).", C.line([{"name": "", "slot": 1, "points": points}], [lab[g] for g in order],
                                   unit="points", xtitle="Results in a row just before the game"))
    l1, l3 = st.get("L1"), st.get("L3")
    if l1 and l3 and _ok(l1.get("mean")) and _ok(l3.get("mean")):
        body += _p(f"After one loss players score {sgn(pts(l1['mean']))} points vs. prediction; after three in a row, "
                   f"{sgn(pts(l3['mean']))}. Long streaks mix tilt with bad days more than single losses do, so "
                   "read them with section 2 in mind.")
    parts.append(_section("3. Losing streaks", body, "s3"))

    # ---------------------------------------------------------------- 4 cooling off
    gp = {}
    for r in R.get("gap", []):
        if r["grp"] == "L":
            gp[r["order"]] = r
    keys = sorted(gp)
    points, labels = [], []
    for i, k in enumerate(keys):
        r = gp[k]
        e7, lo7, hi7 = ci(r.get("diff"), r.get("se_diff"))
        labels.append(r["label"])
        if e7 is not None:
            points.append((i, e7, lo7, hi7))
    body = _p("Does the effect fade if you take a break? Here the next game can come any time up to three days "
              "later; the gap is measured from the end of the previous game to the start of the next.")
    body += _fig("Post-loss gap by time until the next game", "Loss minus win, in points.",
                 C.line([{"name": "", "slot": 1, "points": points}], labels, unit="points",
                        xtitle="Break between the two games"))
    parts.append(_section("4. Does a break help?", body, "s4"))

    # ---------------------------------------------------------------- 5 loss types
    rows = []
    for r in R.get("loss_types", []):
        pt = _diff_point(r, r["grp"])
        if pt:
            rows.append(pt)
    body = _p("Next-game score after different kinds of losses, compared with after a win. The groups overlap "
              "(a long game can also be lost on time). The last two need engine-analysed games.")
    body += _fig("Which losses hurt the next game most?", "Points vs. after a win.", C.forest(rows, unit="points"))
    parts.append(_section("5. Which losses hurt most?", body, "s5"))

    # ---------------------------------------------------------------- 6 rating bands
    bands = [r for r in R.get("bands", []) if r["grp"] == "L"]
    allb = sorted([r for r in bands if r["speed"] == "All"], key=lambda r: r["band_order"])
    labels = [r["band"] for r in allb]
    points = []
    for i, r in enumerate(allb):
        e8, lo8, hi8 = ci(r.get("diff"), r.get("se_diff"))
        if e8 is not None:
            points.append((i, e8, lo8, hi8))
    body = _fig("Post-loss gap by rating", "Loss minus win, in points, all speeds; shaded = 95% range.",
                C.line([{"name": "", "slot": 1, "points": points}], labels, unit="points", band=True,
                       xtitle="Rating before the game"))
    trows = []
    for sp in SPEED_ORDER:
        sb = {r["band"]: r for r in bands if r["speed"] == sp}
        if not sb:
            continue
        trows.append([sp] + [(_cmp_cell(sb.get(b)) if sb.get(b) else "") for b in labels])
    if trows:
        body += "<h3>By speed and rating (points, 95% range)</h3>" + _table(["Speed"] + labels, trows)
    het = R.get("heterogeneity", {})
    if het.get("players") and _ok(het.get("true_sd")):
        body += _p(f"<b>Does everyone tilt the same?</b> Among {num(het['players'])} players with at least 30 games "
                   f"after losses and 30 after wins, personal post-loss gaps spread out by about "
                   f"±{100 * het['true_sd']:.1f} points (one standard deviation) around the average, after removing "
                   "the spread you would expect from luck alone.")
    parts.append(_section("6. Who tilts?", body, "s6"))

    # ---------------------------------------------------------------- 7 quality
    q = R.get("quality", {})
    qrows = []
    names = [("accuracy", "Accuracy (%)", 1.0, "higher is better"),
             ("acpl", "Average centipawn loss", 1.0, "lower is better"),
             ("blunders", "Blunders per 100 moves", 1.0, "lower is better"),
             ("mistakes", "Mistakes per 100 moves", 1.0, "lower is better"),
             ("inaccuracies", "Inaccuracies per 100 moves", 1.0, "lower is better")]
    for key, name, scale, note in names:
        rl, rw = _row(q.get(key), "All", "L"), _row(q.get(key), "All", "W")
        if not rl or not rw:
            continue
        qrows.append([name, num(rl["mean"], 1), num(rw["mean"], 1), _cmp_cell(rl, scale=1.0, dec=2),
                      f"<span class='muted'>{note}</span>"])
    if qrows:
        body = _p("Engine-analysed games only (Lichess stores Stockfish evaluations for about 6% of games), so these "
                  f"rest on {num(ov.get('eval_pairs'))} games instead of {num(ov.get('pairs'))}.")
        body += _table(["Measure", "After a loss", "After a win", "Difference (95% range)", ""], qrows)
        ac, bc = q.get("accuracy_controlled"), q.get("blunders_controlled")
        if ac and bc:
            a0, b0 = ac[0], bc[0]
            ea, la, ha = ci(a0["est"], a0["se"], 1.0)
            eb, lb, hb = ci(b0["est"], b0["se"], 1.0)
            body += _p(f"Comparing only games against opponents of the same relative strength and rating band: "
                       f"accuracy {sgn(ea, 2)} % ({sgn(la, 2)} to {sgn(ha, 2)}), blunders per 100 moves "
                       f"{sgn(eb, 2)} ({sgn(lb, 2)} to {sgn(hb, 2)}).")
        sp_rows = []
        for sp in SPEED_ORDER:
            r = _row(q.get("accuracy"), sp, "L")
            b = _row(q.get("blunders"), sp, "L")
            if r:
                sp_rows.append([sp, _cmp_cell(r, 1.0, 2), _cmp_cell(b, 1.0, 2) if b else "n/a", num(r.get("n"))])
        if sp_rows:
            body += "<h3>By speed (after a loss minus after a win)</h3>"
            body += _table(["Speed", "Accuracy (%)", "Blunders per 100 moves", "Games after a loss"], sp_rows)
    else:
        body = _p("No engine-analysed games were found, so move quality could not be measured.")
    parts.append(_section("7. Move quality", body, "s7"))

    # ---------------------------------------------------------------- 8 behaviour
    b = R.get("behavior", {})
    brow = []

    def add(name, key, scale, dec, unit, note=""):
        rl, rw = _row(b.get(key), "All", "L"), _row(b.get(key), "All", "W")
        if not rl or not rw:
            return
        brow.append([name, f"{num(rl['mean'] * scale, dec)}{unit}", f"{num(rw['mean'] * scale, dec)}{unit}",
                     _cmp_cell(rl, scale, dec), f"<span class='muted'>{note}</span>"])

    add("Seconds before starting the next game", "gap_mean", 1, 0, " s", "mean break, same sitting")
    add("Started the next game within 15 s", "instant", 100, 1, "%", "“rage queue”")
    add("Lost the next game on time", "flag", 100, 1, "%")
    add("Resigned the next game before move 20", "resign_early", 100, 1, "%")
    add("Abandoned the next game", "abandon", 100, 2, "%")
    add("Berserked in the next arena game", "berserk", 100, 1, "%", "arena games only")
    add("Next game was a rematch", "rematch", 100, 1, "%")
    add("Switched to a faster time control", "faster_tc", 100, 1, "%")
    add("Ended the next game in time trouble", "time_trouble", 100, 1, "%", "under 10% of the clock left")
    add("Half-moves in the next game", "plies", 1, 1, "")
    pr = _row(b.get("pace"), "All", "L")
    if pr and _ok(pr.get("diff")):
        e9, lo9, hi9 = ci(pr["diff"], pr["se_diff"], 1.0)
        pct = lambda v: (math.exp(v) - 1) * 100  # noqa: E731
        brow.append(["Time spent per move", "", "", f"{sgn(pct(e9))}% <span class='muted'>({sgn(pct(lo9))} to "
                     f"{sgn(pct(hi9))})</span>", "<span class='muted'>vs. the player's own usual pace</span>"])
    body = _p("What players <i>do</i> after a loss, compared with after a win (next game in the same sitting).")
    body += _table(["", "After a loss", "After a win", "Difference (95% range)", ""], brow)
    stop = {r["grp"]: r for r in b.get("stop", [])}
    if stop.get("L") and stop.get("W"):
        body += _p(f"<b>Stopping.</b> After a loss, {num(pts(stop['L']['mean']), 1)}% of the time players did not "
                   f"start another game within {esc(R['config'].get('session_gap_minutes', 30))} minutes; after a win, "
                   f"{num(pts(stop['W']['mean']), 1)}%.")
    ss = {r["grp"]: r for r in b.get("stop_streak", [])}
    order2 = ["L5", "L4", "L3", "L2", "L1", "W1", "W2", "W3", "W4", "W5"]
    lab2 = {"L5": "5+ losses", "L4": "4 losses", "L3": "3 losses", "L2": "2 losses", "L1": "1 loss", "W1": "1 win",
            "W2": "2 wins", "W3": "3 wins", "W4": "4 wins", "W5": "5+ wins"}
    points = []
    for i, g in enumerate(order2):
        r = ss.get(g)
        if r and _ok(r.get("mean")):
            e10, lo10, hi10 = ci(r["mean"], r["se"])
            points.append((i, e10, lo10, hi10))
    if points:
        body += _fig("Chance of stopping after a run of results", "Share of games after which the player did not "
                     "start another within the session window, in %.",
                     C.line([{"name": "", "slot": 1, "points": points}], [lab2[g] for g in order2], unit="%",
                            fmt="{:.1f}", zero=False, xtitle="Results in a row, including the last game"))
    parts.append(_section("8. Behaviour after a loss", body, "s8"))

    # ---------------------------------------------------------------- 9 data & method
    ex = R.get("extract", {}).get("stats", {})
    frows = []
    if ex:
        frows.append(["Games read from the PGN files", num(ex.get("games_read"))])
        for k, label in (("skip_correspondence", "Removed: correspondence (days per move)"),
                         ("skip_variant", "Removed: chess variants"),
                         ("skip_unfinished", "Removed: unfinished or aborted"),
                         ("skip_no_rating", "Removed: missing rating"),
                         ("skip_no_time", "Removed: missing date/time"),
                         ("skip_no_player", "Removed: missing player name"),
                         ("skip_bad_time_control", "Removed: unreadable time control"),
                         ("skip_parse_error", "Removed: unreadable game")):
            if ex.get(k):
                frows.append([label, num(ex[k])])
        frows.append(["Games kept", num(ex.get("games_kept"))])
    frows.append(["Player-game rows (each game counted once per player)", num(ov.get("rows"))])
    frows.append(["Left out: games involving a BOT account", num(ov.get("bot_rows"))])
    frows.append(["Left out: casual, rules infraction, unstable (provisional) rating, or speed not analysed",
                  num(ov.get("other_excluded"))])
    frows.append(["Usable player-games", num(ov.get("elig_rows"))])
    frows.append(["Usable games with a previous game in the same sitting (the analysis sample)", num(ov.get("pairs"))])
    frows.append(["  of which with clock times", num(ov.get("clock_pairs"))])
    frows.append(["  of which engine-analysed", num(ov.get("eval_pairs"))])
    frows.append(["Players (approx.)", num(ov.get("players"))])
    frows.append(["Sittings (sessions)", f"{num(ov.get('sessions'))} · games per sitting: median "
                  f"{num(ov.get('session_median_len'))}, mean {num(ov.get('session_mean_len'), 1)}"])
    body = "<h3>Which games were used</h3>" + _table(["Step", "Count"], frows)
    cal = R.get("calibration", [])
    if cal:
        labels = [f"{c['gap']:+d}".replace("-", "−") for c in cal]
        body += _fig("Check: does the rating prediction fit?", "Average score by rating gap (player minus "
                     "opponent, 50-point bins). The dots should sit on the line.",
                     C.line([{"name": "Predicted", "slot": 0, "points": [(i, 100 * c["expected"]) for i, c in enumerate(cal)]},
                             {"name": "Actual", "slot": 1, "points": [(i, 100 * c["actual"]) for i, c in enumerate(cal)]}],
                            labels, unit="% score", fmt="{:.1f}", zero=False, markers_only=False,
                            xtitle="Rating gap", ylim=(0, 100)))
    neg = ov.get("negative_gaps") or 0
    if ov.get("pairs"):
        body += _p(f"<span class='muted'>Timing check: {100 * neg / ov['pairs']:.2f}% of next games appear to start "
                   "more than 30 s before the previous one ended (normally from simultaneous games or clock "
                   "estimates). A large share would mean the start-time header is not what the program assumes.</span>")
    cfg = R.get("config", {})
    body += "<h3>How it works</h3><ul>"
    body += ("<li><b>Score vs. prediction.</b> Every game gets a predicted score from the two ratings, fitted on these "
             "games themselves (by speed, colour and rating band, so White's edge and draw rates are built in). "
             "The program compares the actual score with that prediction.</li>")
    body += (f"<li><b>Sittings.</b> Games belong to the same sitting when the next one starts within "
             f"{esc(cfg.get('session_gap_minutes', 30))} minutes of the previous one ending (end time is estimated "
             "from the clocks).</li>")
    body += (f"<li><b>Filters.</b> Rated standard games only; no BOT accounts, no games ended for rules infractions; "
             f"a game is skipped when the rating moved more than {esc(cfg.get('max_rating_change', 30))} points "
             f"(a provisional, still-settling rating). The previous game must have at least "
             f"{esc(cfg.get('min_trigger_plies', 10))} half-moves and not be abandoned.</li>")
    body += ("<li><b>Error bars.</b> All 95% ranges treat each player as one independent unit (clustered by player), "
             "so heavy players cannot make results look more certain than they are.</li>")
    body += ("<li><b>Per-player comparisons</b> use each player's average from their <i>other</i> games. The naive "
             "version (average after losses minus average after wins, per player) is biased in short records "
             "(the known “hot hand” statistics trap).</li>")
    body += ("<li><b>Same sitting vs. random order.</b> For every sitting, the program compares how strongly "
             "each game's result is linked to the next game's score with how strongly it would be linked if the same "
             "games had been played in a random order (an exact calculation, not a simulation). Effects that last the "
             "whole sitting cancel, and there is none of the small-sample bias of per-sitting averages. Scores are "
             "first adjusted for the game's position in the sitting (warm-up or fatigue).</li>")
    body += "</ul><h3>Caveats</h3><ul>"
    body += ("<li>Only players who keep playing are measured after a loss; people who stop are not. Section 8 shows how "
             "often each happens.</li>")
    body += ("<li>Lichess dumps hold rated standard games only. Casual and variant games a player squeezed in between "
             "are invisible, so a few “next games” are really two games later.</li>")
    body += ("<li>Engine evaluations exist only for games someone asked Lichess to analyse, which is not a random "
             "sample (people often analyse their losses).</li>")
    body += ("<li>This is observational data. The same-sitting and same-accuracy checks remove the obvious "
             "alternative explanations, but no method fully replaces an experiment.</li></ul>")
    body += ("<h3>Files</h3><ul><li><code>tables/</code>: every number in this report as CSV (opens in Excel).</li>"
             "<li><code>../export/player_games/</code>: the clean dataset, one row per player per game "
             "(see <code>../export/README.txt</code>).</li><li><code>results.json</code>: all results in one file.</li></ul>")
    parts.append(_section("9. Data and method", body, "s9"))

    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f"<title>Post-loss tilt report</title><style>{CSS}</style></head>"
           f'<body><main>{"".join(parts)}</main><div id="tip" role="status" aria-live="polite"></div>'
           f"<script>{JS}</script></body></html>")
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(doc, encoding="utf-8")
    return out_file


def write_results(R: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / "results.json"
    p.write_text(json.dumps(R, indent=1, default=str), encoding="utf-8")
    return p

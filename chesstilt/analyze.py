"""Stage 3: the statistics behind the report.

Every number comes with a standard error clustered by player: each player
counts as one independent unit, so people with thousands of games cannot
make an estimate look more precise than it is.

The main quantity is the *residual*: actual score minus the score the two
ratings predict (expected score fitted on the data itself). A residual of
-0.02 means the player scored 2 percentage points less than expected.
"""
from __future__ import annotations

import csv
import json
import math
import time
from pathlib import Path

from .build import _sql_path, connect

SPEED_LABELS = {"0": "UltraBullet", "1": "Bullet", "2": "Blitz", "3": "Rapid", "4": "Classical"}
PREV = "CASE p_s2 WHEN 0 THEN 'L' WHEN 1 THEN 'D' WHEN 2 THEN 'W' END"
THIS = "CASE s2 WHEN 0 THEN 'L' WHEN 1 THEN 'D' WHEN 2 THEN 'W' END"
BAND200 = ("CASE WHEN elo < 800 THEN '0600' WHEN elo >= 2400 THEN '2400' "
           "ELSE lpad(((elo // 200) * 200)::VARCHAR, 4, '0') END")
GAP_BIN = """CASE
  WHEN gap < 15 THEN '01|<15 s' WHEN gap < 30 THEN '02|15-30 s' WHEN gap < 60 THEN '03|30-60 s'
  WHEN gap < 120 THEN '04|1-2 min' WHEN gap < 300 THEN '05|2-5 min' WHEN gap < 600 THEN '06|5-10 min'
  WHEN gap < 1800 THEN '07|10-30 min' WHEN gap < 7200 THEN '08|0.5-2 h' WHEN gap < 28800 THEN '09|2-8 h'
  WHEN gap < 86400 THEN '10|8-24 h' ELSE '11|1-3 days' END"""


def band_label(code: str) -> str:
    v = int(code)
    if v <= 600:
        return "under 800"
    if v >= 2400:
        return "2400+"
    return f"{v}-{v + 199}"


class Stats:
    def __init__(self, con, source="seq"):
        self.con = con
        self.source = source

    def rows(self, sql):
        cur = self.con.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def compare(self, value, group, where, ref, by="'All'", source=None):
        """Mean of `value` per group, and each group's difference from `ref` (clustered SEs)."""
        src = source or self.source
        return self.rows(f"""
        WITH b AS (
          SELECT ({by})::VARCHAR AS by_, ({group})::VARCHAR AS g, pid, ({value})::DOUBLE AS y
          FROM {src} WHERE ({where})
        ),
        pp AS (SELECT by_, g, pid, count(*)::DOUBLE AS n, sum(y) AS s
               FROM b WHERE y IS NOT NULL AND g IS NOT NULL AND by_ IS NOT NULL GROUP BY by_, g, pid),
        gm AS (SELECT by_, g, sum(n) AS nn, sum(s) / sum(n) AS mu, count(*) AS players FROM pp GROUP BY by_, g),
        inf AS (SELECT pp.by_, pp.g, pp.pid, (pp.s - pp.n * gm.mu) / gm.nn AS phi
                FROM pp JOIN gm ON pp.by_ = gm.by_ AND pp.g = gm.g),
        sq AS (SELECT by_, g, sum(phi * phi) AS v FROM inf GROUP BY by_, g),
        cr AS (SELECT a.by_, a.g, sum(a.phi * r.phi) AS c FROM inf a
               JOIN inf r ON a.by_ = r.by_ AND a.pid = r.pid AND r.g = '{ref}' AND a.g <> '{ref}'
               GROUP BY a.by_, a.g),
        rf AS (SELECT gm.by_, gm.mu AS mu0, sq.v AS v0 FROM gm
               JOIN sq ON gm.by_ = sq.by_ AND gm.g = sq.g WHERE gm.g = '{ref}')
        SELECT gm.by_ AS "by", gm.g AS grp, gm.nn::BIGINT AS n, gm.players, gm.mu AS mean, sqrt(sq.v) AS se,
               gm.mu - rf.mu0 AS diff,
               CASE WHEN gm.g = '{ref}' THEN 0.0
                    ELSE sqrt(greatest(sq.v + rf.v0 - 2 * coalesce(cr.c, 0), 0)) END AS se_diff
        FROM gm JOIN sq ON gm.by_ = sq.by_ AND gm.g = sq.g
        LEFT JOIN rf ON gm.by_ = rf.by_
        LEFT JOIN cr ON gm.by_ = cr.by_ AND gm.g = cr.g
        ORDER BY "by", grp""")

    def within_session(self):
        """Effect of a lost point on the next game's residual, net of session-level form.

        For each session: T = sum over consecutive pairs of x_t*y_(t+1) minus its exact
        expectation if the session's games were in random order; V is the same quantity
        with y replaced by x (what T would be per unit of effect). Estimate = sum T / sum V.
        x = points lost in a game (1 loss, 0.5 draw, 0 win), y = residual of a game,
        both centred on the average for their position in the session.
        """
        src = self.source
        return self.rows(f"""
        WITH tr AS (SELECT least(gis, 10) AS g, avg(resid) AS my, avg(1 - s2 / 2.0) AS mx
                    FROM {src} WHERE elig AND sess_len >= 2 GROUP BY 1),
        s AS (SELECT q.pid, q.sess, q.speed, q.elig,
                     (1 - q.s2 / 2.0) - t.mx AS x, q.resid - t.my AS y,
                     CASE WHEN q.gis >= 2 THEN (1 - q.p_s2 / 2.0) - tp.mx END AS xp
              FROM {src} q JOIN tr t ON t.g = least(q.gis, 10)
              LEFT JOIN tr tp ON tp.g = least(q.gis - 1, 10)
              WHERE q.sess_len >= 2),
        ss AS (SELECT pid, sess, count(*) AS n, bool_and(elig) AS ok, min(speed) AS smin, max(speed) AS smax,
                      sum(x) AS sx, sum(y) AS sy, sum(x * y) AS sxy, sum(xp * y) AS pxy,
                      sum(xp * xp) AS pxx, sum(xp) AS sxp, sum(xp * x) AS pxc
               FROM s GROUP BY pid, sess),
        v AS (SELECT pid, smin, smax, n, pxy - (sx * sy - sxy) / n AS t, pxx - (sx * sxp - pxc) / n AS v
              FROM ss WHERE ok AND n >= 2),
        u AS (SELECT CASE WHEN smin = smax THEN smin::VARCHAR END AS by_, pid, n, t, v FROM v
              UNION ALL SELECT 'All', pid, n, t, v FROM v),
        e AS (SELECT by_, sum(t) / sum(v) AS est, sum(v) AS sv, count(*) AS sessions, sum(n - 1) AS n
              FROM u WHERE by_ IS NOT NULL GROUP BY by_),
        pp AS (SELECT by_, pid, sum(t) AS t, sum(v) AS v FROM u WHERE by_ IS NOT NULL GROUP BY by_, pid)
        SELECT e.by_ AS "by", e.est, e.sessions, e.n::BIGINT AS n,
               sqrt(sum(((pp.t - e.est * pp.v) / e.sv) * ((pp.t - e.est * pp.v) / e.sv))) AS se
        FROM e JOIN pp ON e.by_ = pp.by_ GROUP BY e.by_, e.est, e.sessions, e.n, e.sv ORDER BY 1""")

    def stratified(self, value, group, where, strata, a="L", b="W", by="'All'"):
        """Difference a - b compared within strata, weighted by a's counts (clustered SE)."""
        return self.rows(f"""
        WITH x AS (SELECT ({by})::VARCHAR AS by_, ({strata})::VARCHAR AS st, ({group})::VARCHAR AS g,
                          pid, ({value})::DOUBLE AS y
                   FROM {self.source} WHERE ({where})),
        ps AS (SELECT by_, st, g, pid, count(*)::DOUBLE AS n, sum(y) AS s FROM x
               WHERE y IS NOT NULL AND g IN ('{a}', '{b}') AND st IS NOT NULL GROUP BY by_, st, g, pid),
        sm AS (SELECT by_, st, g, sum(n) AS nn, sum(s) / sum(n) AS mu FROM ps GROUP BY by_, st, g),
        sw AS (SELECT by_, st,
                 max(nn) FILTER (WHERE g = '{a}') AS na, max(nn) FILTER (WHERE g = '{b}') AS nb,
                 max(mu) FILTER (WHERE g = '{a}') AS mua, max(mu) FILTER (WHERE g = '{b}') AS mub
               FROM sm GROUP BY by_, st),
        sw2 AS (SELECT * FROM sw WHERE na > 0 AND nb > 0),
        tot AS (SELECT by_, sum(na) AS t FROM sw2 GROUP BY by_),
        est AS (SELECT sw2.by_, sum(na * (mua - mub)) / any_value(tot.t) AS d
                FROM sw2 JOIN tot ON sw2.by_ = tot.by_ GROUP BY sw2.by_),
        inf AS (SELECT ps.by_, ps.pid,
                  sum((sw2.na / tot.t) * CASE WHEN ps.g = '{a}' THEN (ps.s - ps.n * sw2.mua) / sw2.na
                                              ELSE -(ps.s - ps.n * sw2.mub) / sw2.nb END) AS phi
                FROM ps JOIN sw2 ON ps.by_ = sw2.by_ AND ps.st = sw2.st JOIN tot ON ps.by_ = tot.by_
                GROUP BY ps.by_, ps.pid)
        SELECT est.by_ AS "by", est.d AS est, sqrt(sum(inf.phi * inf.phi)) AS se, any_value(tot.t)::BIGINT AS n
        FROM est JOIN inf ON est.by_ = inf.by_ JOIN tot ON est.by_ = tot.by_
        GROUP BY est.by_, est.d ORDER BY 1""")


def _speed_by(rows, key="by"):
    for r in rows:
        r[key] = SPEED_LABELS.get(r[key], r[key])
    return rows


def _pick(rows, grp, key="grp"):
    return [r for r in rows if r[key] == grp]


def run_analysis(out_dir: Path, cfg: dict, log=print) -> dict:
    out_dir = Path(out_dir)
    t0 = time.time()
    seq_glob = _sql_path(out_dir / "export" / "player_games") + "/*.parquet"
    con, _ = connect(out_dir / "work", cfg)
    con.execute(f"CREATE OR REPLACE VIEW seq AS SELECT * FROM read_parquet('{seq_glob}')")
    S = Stats(con)
    gap_s = int(float(cfg.get("session_gap_minutes", 30)) * 60)
    min_n = int(cfg.get("min_games_within_player", 10))
    PAIRS = "elig AND p_ok AND NOT new_sess"
    R: dict = {"config": {k: cfg.get(k) for k in ("session_gap_minutes", "max_rating_change",
                                                  "min_trigger_plies", "speeds", "min_games_within_player")}}

    def step(name):
        log(f"  {name} ({time.time() - t0:.0f} s)")

    # ---------------------------------------------------------------- overview & funnel
    step("overview")
    ov = S.rows(f"""
        SELECT count(*) AS rows, approx_count_distinct(pid) AS players, min(ts) AS t_min, max(ts) AS t_max,
          count(*) FILTER (WHERE bot OR obot) AS bot_rows,
          count(*) FILTER (WHERE NOT (bot OR obot) AND NOT elig) AS other_excluded,
          count(*) FILTER (WHERE elig) AS elig_rows,
          count(*) FILTER (WHERE elig AND p_s2 IS NOT NULL AND NOT new_sess) AS in_session,
          count(*) FILTER (WHERE {PAIRS}) AS pairs,
          count(*) FILTER (WHERE {PAIRS} AND has_eval) AS eval_pairs,
          count(*) FILTER (WHERE {PAIRS} AND p_has_eval) AS eval_trigger_pairs,
          count(*) FILTER (WHERE {PAIRS} AND has_clk) AS clock_pairs,
          count(*) FILTER (WHERE {PAIRS} AND gap < -30) AS negative_gaps,
          avg(slope) FILTER (WHERE {PAIRS}) AS mean_slope,
          avg(resid) FILTER (WHERE elig) AS mean_resid_all
        FROM seq""")[0]
    ov["games"] = ov["rows"] // 2
    sess = S.rows("""SELECT avg(sess_len) AS mean_len, quantile_cont(sess_len, 0.5) AS median_len,
                        count(*) AS sessions FROM seq WHERE gis = 1 AND elig""")[0]
    ov.update({"session_mean_len": sess["mean_len"], "session_median_len": sess["median_len"],
               "sessions": sess["sessions"]})
    R["overview"] = ov
    by_speed = S.rows(f"""SELECT speed::VARCHAR AS "by", count(*) FILTER (WHERE elig) AS rows,
                             count(*) FILTER (WHERE {PAIRS}) AS pairs FROM seq GROUP BY 1 ORDER BY 1""")
    R["speed_counts"] = _speed_by(by_speed)
    ex_file = out_dir / "extract_summary.json"
    if ex_file.exists():
        R["extract"] = json.loads(ex_file.read_text())

    # ---------------------------------------------------------------- headline
    step("headline: next game after a loss / draw / win")
    head = S.compare("resid", PREV, PAIRS, "W", by="speed") + S.compare("resid", PREV, PAIRS, "W")
    R["headline"] = _speed_by(head)
    R["outcomes"] = {
        "win": _speed_by(S.compare("(s2 = 2)::INT", PREV, PAIRS, "W", by="speed") +
                         S.compare("(s2 = 2)::INT", PREV, PAIRS, "W")),
        "loss": _speed_by(S.compare("(s2 = 0)::INT", PREV, PAIRS, "W", by="speed") +
                          S.compare("(s2 = 0)::INT", PREV, PAIRS, "W")),
        "expected": _speed_by(S.compare("exp_score", PREV, PAIRS, "W", by="speed") +
                              S.compare("exp_score", PREV, PAIRS, "W")),
        "actual": _speed_by(S.compare("s2 / 2.0", PREV, PAIRS, "W", by="speed") +
                            S.compare("s2 / 2.0", PREV, PAIRS, "W")),
    }

    # ---------------------------------------------------------------- robustness: 3 more estimators
    step("robustness checks")
    rob = {}
    # (a) Player-adjusted: compare each player with themselves. Their average residual is
    #     taken from their OTHER games (leaving out this game and the previous one), which
    #     avoids the small-sample "hot hand" bias of per-player before/after averages.
    con.execute(f"""CREATE OR REPLACE TEMP TABLE pmean AS
        SELECT pid, sum(resid) AS s, count(*) AS n FROM seq WHERE elig
        GROUP BY pid HAVING count(*) >= {max(min_n, 5)}""")
    con.execute("""CREATE OR REPLACE TEMP VIEW seq_adj AS
        SELECT seq.*, resid - (pmean.s - resid - p_resid) / (pmean.n - 2) AS resid_adj
        FROM seq JOIN pmean ON seq.pid = pmean.pid""")
    A = Stats(con, "seq_adj")
    rob["player_adjusted"] = _speed_by(
        [r for r in A.compare("resid_adj", PREV, PAIRS, "W", by="speed") +
         A.compare("resid_adj", PREV, PAIRS, "W") if r["grp"] == "L"])
    # (b) Like-for-like: compare only games with the same speed, rating band (before the
    #     previous game), game number in the session and event type. Groups are kept coarse
    #     so each holds many players (fine groups inherit the per-player "hot hand" bias).
    strata = ("speed || '|' || least(greatest(p_elo // 200, 3), 14) || '|' || "
              "CASE WHEN gis <= 2 THEN 2 WHEN gis <= 4 THEN 3 ELSE 5 END || '|' || least(kind, 1)")
    rob["controlled"] = _speed_by(S.stratified("resid", PREV, PAIRS, strata, by="speed") +
                                  S.stratified("resid", PREV, PAIRS, strata))
    # (c) Same sitting vs. random order: within each session, is a game's result linked to the
    #     NEXT game more than to a random other game of the same session? Anything that is
    #     constant over a sitting (a bad day, tiredness, mood) affects every game of the
    #     session equally, so it cancels. Compared with the exact random-order expectation,
    #     this has no small-sample bias. Values are trended by game number first.
    rob["same_sitting"] = _speed_by(S.within_session())
    R["robust"] = rob

    # ---------------------------------------------------------------- streaks
    step("losing and winning streaks")
    STRK = ("CASE WHEN p_s2 = 1 THEN 'D' WHEN strk_l >= 1 THEN 'L' || least(strk_l, 5) "
            "WHEN strk_w >= 1 THEN 'W' || least(strk_w, 5) END")
    R["streak"] = S.compare("resid", STRK, PAIRS, "W1")
    R["streak_loss_prob"] = S.compare("(s2 = 0)::INT", STRK, PAIRS, "W1")

    # ---------------------------------------------------------------- cool-down
    step("time between games")
    gap_rows = S.compare("resid", PREV, f"elig AND p_ok AND gap IS NOT NULL AND gap <= {3 * 86400}", "W", by=GAP_BIN)
    for r in gap_rows:
        r["order"], r["label"] = r["by"].split("|")
    R["gap"] = gap_rows

    # ---------------------------------------------------------------- rating bands
    step("rating bands")
    bands = S.compare("resid", PREV, PAIRS, "W", by=f"speed || '|' || {BAND200}") + \
        S.compare("resid", PREV, PAIRS, "W", by=f"'All|' || {BAND200}")
    for r in bands:
        sp, bd = r["by"].split("|")
        r["speed"] = SPEED_LABELS.get(sp, sp)
        r["band"] = band_label(bd)
        r["band_order"] = int(bd)
    R["bands"] = bands

    # ---------------------------------------------------------------- kinds of losses
    step("kinds of losses")
    types = [
        ("Lost on time", "p_term = 1"),
        ("Got checkmated", "p_mate"),
        ("Resigned", "p_term = 0 AND NOT p_mate"),
        ("Lost to someone 100+ lower", "p_oelo <= p_elo - 100"),
        ("Lost to a similar rating", "abs(p_oelo - p_elo) < 100"),
        ("Lost to someone 100+ higher", "p_oelo >= p_elo + 100"),
        ("Long game (60+ moves)", "p_plies >= 120"),
        ("Short game (under 25 moves)", "p_plies < 50"),
        ("Was winning (80%+ chance)", "p_has_eval AND p_maxwp >= 80"),
        ("Never stood better (under 55%)", "p_has_eval AND p_maxwp < 55"),
    ]
    tlist = ", ".join(f"CASE WHEN {cond} THEN '{name}' END" for name, cond in types)
    src = f"""(SELECT pid, resid, unnest(CASE WHEN p_s2 = 2 THEN ['After a win']
                    ELSE [{tlist}] END) AS lt
             FROM seq WHERE {PAIRS} AND p_s2 IN (0, 2))"""
    lt = S.compare("resid", "lt", "TRUE", "After a win", source=src)
    order = {name: i for i, (name, _) in enumerate(types)}
    R["loss_types"] = sorted([r for r in lt if r["grp"] != "After a win"], key=lambda r: order[r["grp"]])

    # ---------------------------------------------------------------- move quality
    step("move quality (games with engine evals)")
    QW = f"{PAIRS} AND has_eval AND nev >= 5"
    qual = {}
    for key, expr in (("accuracy", "acc"), ("acpl", "acpl"), ("blunders", "100.0 * bl / nev"),
                      ("mistakes", "100.0 * mi / nev"), ("inaccuracies", "100.0 * ina / nev")):
        qual[key] = _speed_by(S.compare(expr, PREV, QW, "W", by="speed") + S.compare(expr, PREV, QW, "W"))
    qstrata = "speed || '|' || (elo // 200) || '|' || greatest(-4, least(4, floor((oelo - elo) / 100.0)))::INTEGER"
    qual["accuracy_controlled"] = S.stratified("acc", PREV, QW, qstrata)
    qual["blunders_controlled"] = S.stratified("100.0 * bl / nev", PREV, QW, qstrata)
    R["quality"] = qual

    # ---------------------------------------------------------------- luck vs. form
    step("losses where the player played well")
    LW = f"{PAIRS} AND p_has_eval AND p_acc IS NOT NULL"
    R["luck"] = {
        "raw": [r for r in S.compare("resid", PREV, LW, "W") if r["grp"] == "L"],
        "controlled": S.stratified("resid", PREV, LW, "speed || '|' || least(floor(p_acc / 5), 19)::INTEGER"),
    }

    # ---------------------------------------------------------------- behaviour
    step("behaviour after a loss")
    beh = {}
    beh["gap_mean"] = S.compare("gap", PREV, PAIRS, "W")
    beh["gap_quantiles"] = S.rows(f"""
        SELECT {PREV} AS grp, quantile_cont(gap, 0.25) AS q25, quantile_cont(gap, 0.5) AS q50,
               quantile_cont(gap, 0.75) AS q75, avg((gap <= 15)::INT) AS instant
        FROM seq WHERE {PAIRS} AND gap >= 0 GROUP BY 1 ORDER BY 1""")
    beh["instant"] = S.compare("(gap <= 15)::INT", PREV, PAIRS, "W")
    stop = f"(n_gap IS NULL OR n_gap > {gap_s})::INT"
    beh["stop"] = S.compare(stop, THIS, "elig AND trig_ok", "W")
    QSTR = ("CASE WHEN s2 = 0 THEN 'L' || least(strk_l + 1, 5) WHEN s2 = 2 THEN 'W' || least(strk_w + 1, 5) END")
    beh["stop_streak"] = S.compare(stop, QSTR, "elig AND trig_ok", "W1")
    metrics = {
        "pace": ("pace_rel", f"{PAIRS} AND pace_rel IS NOT NULL"),
        "flag": ("(term = 1 AND s2 = 0)::INT", PAIRS),
        "resign_early": ("(term = 0 AND s2 = 0 AND NOT mate AND plies < 40)::INT", PAIRS),
        "abandon": ("(term = 2 AND s2 = 0)::INT", PAIRS),
        "berserk": ("berserk::INT", f"{PAIRS} AND kind = 1 AND has_clk"),
        "plies": ("plies", PAIRS),
        "rematch": ("rematch::INT", PAIRS),
        "faster_tc": ("(base + 40 * inc < p_est)::INT", PAIRS),
        "time_trouble": ("(clk1 < 0.1 * base)::INT", f"{PAIRS} AND has_clk AND base > 0"),
    }
    for key, (expr, where) in metrics.items():
        beh[key] = S.compare(expr, PREV, where, "W")
    R["behavior"] = beh

    # ---------------------------------------------------------------- player differences
    step("how much players differ")
    het = S.rows(f"""
        WITH pp AS (
          SELECT pid,
            count(*) FILTER (WHERE p_s2 = 0) AS nl, count(*) FILTER (WHERE p_s2 = 2) AS nw,
            avg(resid) FILTER (WHERE p_s2 = 0) AS ml, avg(resid) FILTER (WHERE p_s2 = 2) AS mw,
            var_samp(resid) FILTER (WHERE p_s2 = 0) AS vl, var_samp(resid) FILTER (WHERE p_s2 = 2) AS vw
          FROM seq WHERE {PAIRS} GROUP BY pid),
        q AS (SELECT ml - mw AS d, vl / nl + vw / nw AS v FROM pp WHERE nl >= 30 AND nw >= 30)
        SELECT count(*) AS players, avg(d) AS mean_d, var_samp(d) AS var_d, avg(v) AS noise_var,
               quantile_cont(d, [0.1, 0.25, 0.5, 0.75, 0.9]) AS qs
        FROM q""")[0]
    if het["players"] and het["players"] > 20 and het["var_d"] is not None:
        het["true_sd"] = math.sqrt(max(0.0, het["var_d"] - het["noise_var"]))
    R["heterogeneity"] = het

    # ---------------------------------------------------------------- calibration
    step("calibration check")
    R["calibration"] = S.rows("""
        SELECT greatest(-8, least(8, floor((elo - oelo) / 50.0)))::INTEGER * 50 AS gap, count(*) AS n,
               avg(s2 / 2.0) AS actual, avg(exp_score) AS expected
        FROM seq WHERE elig GROUP BY 1 ORDER BY 1""")
    con.close()
    R["seconds"] = round(time.time() - t0, 1)
    return R


def save_tables(R: dict, folder: Path) -> list:
    """Write every result table as CSV (easy to open in Excel)."""
    folder.mkdir(parents=True, exist_ok=True)
    written = []

    def dump(name, rows):
        if not rows or not isinstance(rows, list) or not isinstance(rows[0], dict):
            return
        cols = list(rows[0].keys())
        path = folder / f"{name}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()})
        written.append(path)

    for key, val in R.items():
        if isinstance(val, list):
            dump(key, val)
        elif isinstance(val, dict):
            for sub, rows in val.items():
                dump(f"{key}_{sub}", rows)
    return written

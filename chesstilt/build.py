"""Stage 2: turn games into per-player timelines with DuckDB.

Output: export/player_games/part-NNN.parquet, one row per player per game, with
the player's previous game, session, streak, expected score and residual.
Players are processed in buckets so memory use stays bounded.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import time
from pathlib import Path

import numpy as np

from .pgnparse import SPEED_NAMES

BUILD_VERSION = 3
BAND_MIN, BAND_MAX = 4, 29          # rating bands of 100 points: <500 ... 2900+


def total_memory_bytes() -> int:
    try:
        import ctypes
        if hasattr(ctypes, "windll"):
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return int(st.ullTotalPhys)
    except Exception:
        pass
    try:
        import os
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 8 << 30


def _sql_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")


def connect(work: Path, cfg: dict):
    import duckdb
    work.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(work / "tilt.duckdb"))
    mem = cfg.get("memory_limit") or f"{max(1, int(total_memory_bytes() * 0.6 / 2**30))}GB"
    tmp = work / "duckdb_tmp"
    tmp.mkdir(exist_ok=True)
    con.execute(f"SET memory_limit='{mem}'")
    con.execute(f"SET temp_directory='{_sql_path(tmp)}'")
    con.execute("SET preserve_insertion_order=false")
    if cfg.get("threads"):
        con.execute(f"SET threads={int(cfg['threads'])}")
    return con, mem


def _parse_mem(mem: str) -> int:
    s = mem.strip().upper().replace("IB", "B")
    for unit, mult in (("TB", 2**40), ("GB", 2**30), ("MB", 2**20), ("KB", 2**10)):
        if s.endswith(unit):
            return int(float(s[: -len(unit)]) * mult)
    return int(float(s))


def fingerprint(games_dir: Path, cfg: dict) -> str:
    h = hashlib.sha256()
    for f in sorted(games_dir.glob("*/_SUCCESS.json")):
        h.update(f.read_bytes())
    keys = ("session_gap_minutes", "max_rating_change", "min_trigger_plies", "speeds")
    h.update(json.dumps({k: cfg.get(k) for k in keys}, sort_keys=True).encode())
    h.update(str(BUILD_VERSION).encode())
    return h.hexdigest()[:16]


def fit_expected_model(con, log=print):
    """Expected score as a function of rating gap, per speed, colour and rating band.

    Fractional logistic regression on binned data: score ~ logit(a + b*x + c*x^2),
    x = rating gap / 400 (clamped to +-800 points). Sparse groups fall back to the
    speed+colour fit.
    """
    df = con.execute(f"""
        SELECT speed, color,
               least(greatest(elo // 100, {BAND_MIN}), {BAND_MAX}) AS band,
               greatest(-32, least(32, round((elo - oelo) / 25.0)))::INTEGER AS dbin,
               count(*) AS n, avg(s2 / 2.0) AS score
        FROM pg
        WHERE rated AND NOT bot AND NOT obot AND term IN (0, 1, 2)
        GROUP BY ALL
    """).fetchnumpy()
    speed, color, band = df["speed"], df["color"], df["band"]
    x = df["dbin"].astype(float) * 25.0 / 400.0
    n = df["n"].astype(float)
    y = df["score"].astype(float)

    def fit(mask):
        X = np.column_stack([np.ones(mask.sum()), x[mask], x[mask] ** 2])
        w, yy = n[mask], y[mask]
        beta = np.array([0.0, math.log(10.0), 0.0])
        for _ in range(50):
            eta = X @ beta
            p = 1.0 / (1.0 + np.exp(-eta))
            v = np.clip(p * (1 - p), 1e-6, None)
            z = eta + (yy - p) / v
            W = w * v
            A = X.T @ (W[:, None] * X) + np.diag([1e-6, 1e-6, 1e-3])
            nb = np.linalg.solve(A, X.T @ (W * z))
            if np.max(np.abs(nb - beta)) < 1e-9:
                beta = nb
                break
            beta = nb
        return beta

    rows = []
    for sp in range(len(SPEED_NAMES)):
        for co in (0, 1):
            m_sc = (speed == sp) & (color == co)
            if n[m_sc].sum() >= 500:
                pooled = fit(m_sc)
            else:
                pooled = np.array([0.04 if co == 0 else -0.04, math.log(10.0), 0.0])
            for bd in range(BAND_MIN, BAND_MAX + 1):
                m = m_sc & (band == bd)
                beta = fit(m) if n[m].sum() >= 3000 and m.sum() >= 8 else pooled
                rows.append((sp, co, bd, float(beta[0]), float(beta[1]), float(beta[2])))
    con.execute("CREATE OR REPLACE TABLE model (speed TINYINT, color TINYINT, band TINYINT, a DOUBLE, b DOUBLE, c DOUBLE)")
    con.executemany("INSERT INTO model VALUES (?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def build(out_dir: Path, cfg: dict, log=print, force: bool = False) -> dict:
    out_dir = Path(out_dir)
    games_dir = out_dir / "games"
    work = out_dir / "work"
    export = out_dir / "export"
    seq_dir = export / "player_games"
    parts = sorted(games_dir.glob("*/part-*.parquet"))
    if not parts:
        raise SystemExit(f"No extracted games found in {games_dir}. Run the extract step first.")
    fp = fingerprint(games_dir, cfg)
    meta_file = work / "build.json"
    if not force and meta_file.exists() and seq_dir.exists():
        try:
            meta = json.loads(meta_file.read_text())
            if meta.get("fingerprint") == fp:
                log("Timelines are up to date; skipping the build step (use --rebuild to force).")
                return meta
        except (OSError, ValueError):
            pass

    t0 = time.time()
    for d in (seq_dir, work / "pg"):
        if d.exists():
            shutil.rmtree(d)
    if (work / "tilt.duckdb").exists():
        (work / "tilt.duckdb").unlink()
    seq_dir.mkdir(parents=True, exist_ok=True)
    con, mem = connect(work, cfg)
    log(f"Building player timelines with DuckDB (memory limit {mem}).")

    glob = _sql_path(games_dir) + "/*/part-*.parquet"
    con.execute(f"CREATE VIEW games AS SELECT * FROM read_parquet('{glob}', union_by_name = true)")
    n_games = con.execute("SELECT count(*) FROM games").fetchone()[0]
    rows_total = 2 * n_games
    per_bucket = max(2_000_000, min(30_000_000, _parse_mem(mem) // 1200))
    buckets = max(1, math.ceil(rows_total / per_bucket))
    log(f"  {n_games:,} games -> {rows_total:,} player-game rows in {buckets} bucket(s)")

    side_cols = """
        {me}elo AS elo, {op}elo AS oelo, {me}diff AS rdiff,
        speed, kind, rated, base, inc, term, plies, mate,
        {me}bot AS bot, {op}bot AS obot, {me}titled AS titled,
        has_clk, {me}_clk1 AS clk1, {me}_berserk AS berserk, {me}_mt AS mt,
        has_eval, {me}_nev AS nev, {me}_acc AS acc, {op}_acc AS oacc, {me}_acpl AS acpl,
        {me}_blunders AS bl, {me}_mistakes AS mi, {me}_inacc AS ina, {me}_maxwp AS maxwp"""
    dur = "CAST(coalesce(duration, 0.6 * (2 * base + plies * inc)) AS BIGINT)"
    pg_dir = work / "pg"
    con.execute(f"""
        COPY (
          SELECT hash(white) AS pid, hash(black) AS opid, gid, 0::TINYINT AS color, ts, ts + {dur} AS te,
                 result AS s2, final_wp AS fwp,
                 {side_cols.format(me='w', op='b')},
                 (hash(white) % {buckets})::INTEGER AS bucket
          FROM games
          UNION ALL
          SELECT hash(black), hash(white), gid, 1::TINYINT, ts, ts + {dur},
                 2 - result, 100 - final_wp,
                 {side_cols.format(me='b', op='w')},
                 (hash(black) % {buckets})::INTEGER
          FROM games
        ) TO '{_sql_path(pg_dir)}' (FORMAT parquet, PARTITION_BY (bucket), COMPRESSION zstd)
    """)
    con.execute(f"CREATE VIEW pg AS SELECT * FROM read_parquet('{_sql_path(pg_dir)}/*/*.parquet', hive_partitioning = true)")
    log(f"  player-game table written ({time.time() - t0:.0f} s); fitting expected-score model")
    fit_expected_model(con, log)

    gap = int(float(cfg.get("session_gap_minutes", 30)) * 60)
    max_rd = int(cfg.get("max_rating_change", 30))
    min_tp = int(cfg.get("min_trigger_plies", 10))
    speeds = ",".join(str(SPEED_NAMES.index(s)) for s in cfg.get("speeds", SPEED_NAMES[:5]))
    for k in range(buckets):
        src = _sql_path(pg_dir / f"bucket={k}") + "/*.parquet"
        dst = _sql_path(seq_dir / f"part-{k:03d}.parquet")
        if not list((pg_dir / f"bucket={k}").glob("*.parquet")):
            continue
        con.execute(SEQ_SQL.format(src=src, dst=dst, gap=gap, max_rd=max_rd, min_tp=min_tp,
                                   speeds=speeds, bmin=BAND_MIN, bmax=BAND_MAX))
        shutil.rmtree(pg_dir / f"bucket={k}", ignore_errors=True)  # free disk as we go
        log(f"  bucket {k + 1}/{buckets} done ({time.time() - t0:.0f} s)")

    con.execute(f"""
        COPY (SELECT DISTINCT hash(name) AS pid, name FROM
              (SELECT white AS name FROM games UNION ALL SELECT black FROM games))
        TO '{_sql_path(export / 'players.parquet')}' (FORMAT parquet, COMPRESSION zstd)
    """)
    model = con.execute("SELECT * FROM model").fetchall()
    con.close()
    shutil.rmtree(pg_dir, ignore_errors=True)
    meta = {"fingerprint": fp, "games": n_games, "rows": rows_total, "buckets": buckets,
            "seconds": round(time.time() - t0, 1), "config": {k: cfg.get(k) for k in
            ("session_gap_minutes", "max_rating_change", "min_trigger_plies", "speeds")},
            "model_rows": len(model)}
    meta_file.write_text(json.dumps(meta, indent=2))
    (export / "README.txt").write_text(EXPORT_README)
    log(f"Timelines built in {meta['seconds']:.0f} s -> {seq_dir}")
    return meta


SEQ_SQL = """
COPY (
WITH b0 AS (
  SELECT *,
    greatest(-800, least(800, elo - oelo)) / 400.0 AS x,
    least(greatest(elo // 100, {bmin}), {bmax})::TINYINT AS band
  FROM read_parquet('{src}')
),
b1 AS (
  SELECT b0.*,
    1.0 / (1.0 + exp(-(m.a + m.b * b0.x + m.c * b0.x * b0.x))) AS xs,
    (m.b + 2.0 * m.c * b0.x) / 400.0 AS dlin
  FROM b0 JOIN model m ON m.speed = b0.speed AND m.color = b0.color AND m.band = b0.band
),
b2 AS (
  SELECT *,
    CASE WHEN abs(elo - oelo) < 800 THEN xs * (1 - xs) * dlin ELSE 0 END AS slope,
    s2 / 2.0 - xs AS resid,
    (rated AND NOT bot AND NOT obot AND term IN (0, 1, 2)
      AND (rdiff IS NULL OR abs(rdiff) <= {max_rd}) AND speed IN ({speeds})) AS elig,
    CASE WHEN mt > 0 AND base > 0
         THEN ln(mt / (CASE WHEN berserk THEN base / 80.0 ELSE base / 40.0 + inc END)) END AS lpace
  FROM b1
),
b3 AS (
  SELECT *,
    elig AND term <> 2 AND plies >= {min_tp} AS trig_ok,
    lpace - avg(lpace) OVER (PARTITION BY pid, speed) AS pace_rel
  FROM b2
),
w1 AS (
  SELECT *,
    lag(te) OVER w AS p_te,
    lead(ts) OVER w AS n_ts,
    lag(s2) OVER w AS p_s2,
    lag(trig_ok) OVER w AS p_ok,
    lag(term) OVER w AS p_term,
    lag(plies) OVER w AS p_plies,
    lag(mate) OVER w AS p_mate,
    lag(speed) OVER w AS p_speed,
    lag(kind) OVER w AS p_kind,
    lag(base + 40 * inc) OVER w AS p_est,
    lag(rdiff) OVER w AS p_rdiff,
    lag(elo) OVER w AS p_elo,
    lag(oelo) OVER w AS p_oelo,
    lag(opid) OVER w AS p_opid,
    lag(has_eval) OVER w AS p_has_eval,
    lag(acc) OVER w AS p_acc,
    lag(oacc) OVER w AS p_oacc,
    lag(maxwp) OVER w AS p_maxwp,
    lag(resid) OVER w AS p_resid
  FROM b3
  WINDOW w AS (PARTITION BY pid ORDER BY ts, gid)
),
w2 AS (
  SELECT *,
    ts - p_te AS gap,
    n_ts - te AS n_gap,
    (p_te IS NULL OR ts - p_te > {gap}) AS new_sess
  FROM w1
),
w3 AS (
  SELECT *,
    sum(CASE WHEN new_sess THEN 1 ELSE 0 END)
      OVER (PARTITION BY pid ORDER BY ts, gid ROWS UNBOUNDED PRECEDING) AS sess
  FROM w2
),
w4 AS (
  SELECT *,
    row_number() OVER s AS gis,
    count(*) OVER (PARTITION BY pid, sess) AS sess_len,
    sum(s2 - 1) OVER (PARTITION BY pid, sess ORDER BY ts, gid
                      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS sess_net0,
    lag(s2, 1) OVER s AS l1, lag(s2, 2) OVER s AS l2, lag(s2, 3) OVER s AS l3,
    lag(s2, 4) OVER s AS l4, lag(s2, 5) OVER s AS l5
  FROM w3
  WINDOW s AS (PARTITION BY pid, sess ORDER BY ts, gid)
)
SELECT
  gid, pid, opid, color, ts, te, speed, kind, band, base, inc, elo, oelo, rdiff, s2,
  xs::FLOAT AS exp_score, slope::FLOAT AS slope, resid::FLOAT AS resid,
  rated, bot, obot, titled, term, plies, mate, berserk, has_clk, mt, pace_rel::FLOAT AS pace_rel, clk1,
  has_eval, nev, acc, oacc, acpl, bl, mi, ina, maxwp, fwp,
  elig, trig_ok, gap::INTEGER AS gap, n_gap::INTEGER AS n_gap, new_sess, sess::INTEGER AS sess,
  gis::INTEGER AS gis, sess_len::INTEGER AS sess_len, coalesce(sess_net0, 0)::INTEGER AS sess_net,
  p_s2, p_ok, p_term, p_plies, p_mate, p_speed, p_kind, p_est, p_rdiff, p_elo, p_oelo,
  (p_opid = opid) AS rematch, p_has_eval, p_acc, p_oacc, p_maxwp, p_resid::FLOAT AS p_resid,
  CASE WHEN l1 IS NULL OR l1 <> 0 THEN 0 WHEN l2 IS NULL OR l2 <> 0 THEN 1
       WHEN l3 IS NULL OR l3 <> 0 THEN 2 WHEN l4 IS NULL OR l4 <> 0 THEN 3
       WHEN l5 IS NULL OR l5 <> 0 THEN 4 ELSE 5 END AS strk_l,
  CASE WHEN l1 IS NULL OR l1 <> 2 THEN 0 WHEN l2 IS NULL OR l2 <> 2 THEN 1
       WHEN l3 IS NULL OR l3 <> 2 THEN 2 WHEN l4 IS NULL OR l4 <> 2 THEN 3
       WHEN l5 IS NULL OR l5 <> 2 THEN 4 ELSE 5 END AS strk_w
FROM w4
ORDER BY pid, ts, gid
) TO '{dst}' (FORMAT parquet, COMPRESSION zstd)
"""

EXPORT_README = """Clean dataset exported by chess-tilt
=====================================

player_games/*.parquet  One row per player per game (each game appears twice:
                        once from White's side, once from Black's).
players.parquet         pid -> Lichess username.
../games/*/part-*.parquet  One row per game, straight from the PGN (see the
                        column list in chesstilt/pgnparse.py).

Open them with DuckDB, pandas (pd.read_parquet), Polars, R (arrow), or Excel via
Power Query. Example (DuckDB):
  SELECT * FROM 'player_games/*.parquet' WHERE p_s2 = 0 LIMIT 100;

Main columns of player_games
----------------------------
gid            Lichess game id -> https://lichess.org/<gid>
pid, opid      player / opponent id (join players.parquet for the name)
color          0 = White, 1 = Black
ts, te         game start / estimated end, Unix seconds UTC
speed          0 ultrabullet, 1 bullet, 2 blitz, 3 rapid, 4 classical
kind           0 normal pairing, 1 arena, 2 swiss, 3 other
band           rating band (elo // 100, clamped 4..29)
base, inc      initial clock and increment, seconds
elo, oelo      player / opponent rating before the game
rdiff          player's rating change from this game
s2             result x2: 2 win, 1 draw, 0 loss
exp_score      expected score from the ratings (fitted on this data)
slope          change in expected score per rating point
resid          score - exp_score   (negative = worse than the ratings predict)
rated          rated game;  bot / obot = player / opponent is a BOT account;  titled = player has a title
term           0 normal, 1 time forfeit, 2 abandoned
plies          half-moves played;  mate = ended in checkmate
has_clk        game has clock times;  berserk = player berserked (arena)
mt             average seconds per move; pace_rel = log pace vs. the player's own average
clk1           player's clock after their last move, s
has_eval, nev  game has engine evals;  moves of the player that have evals
acc, acpl      accuracy % and average centipawn loss (games with engine evals only)
oacc           opponent's accuracy %;  fwp = player's win chance % in the final position
bl, mi, ina    blunders, mistakes, inaccuracies;  maxwp = best win chance % in the game
elig           row passes the filters (rated, no bots, no rules infraction, stable rating)
trig_ok        game can count as the "previous game" (elig, not abandoned, >= N plies)
gap            seconds from the end of the player's previous game to the start of this one
n_gap          seconds from the end of this game to the start of the player's next game
new_sess       this game starts a new session (gap above the session threshold)
sess, gis      session number for the player; game number within the session
sess_len       games in this session;  sess_net = wins minus losses earlier in the session
p_*            the same fields for the player's previous game (p_s2 = previous result)
rematch        same opponent as the previous game
strk_l, strk_w losses / wins in a row right before this game, in this session (capped at 5)
"""

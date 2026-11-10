"""Fast parser for Lichess PGN dumps.

It reads only what the tilt analysis needs (headers, clocks and engine evals)
and never replays the moves. That keeps it fast enough for 100M+ games.

Each game becomes one tuple whose fields follow COLUMNS below.
"""
from __future__ import annotations

import math
import re
from datetime import date

SPEED_NAMES = ("ultrabullet", "bullet", "blitz", "rapid", "classical", "correspondence")
KIND_NAMES = ("pool", "arena", "swiss", "other")
TERM_NAMES = ("normal", "time_forfeit", "abandoned", "rules_infraction", "unterminated", "other")
RESULT_NAMES = {2: "white_win", 1: "draw", 0: "black_win"}

# Output columns, in tuple order: (name, type, description).
# Types: str, i64, i32, i16, i8, f32, bool.  "w_"/"b_" = White/Black side.
COLUMNS = [
    ("gid", "str", "Lichess game id (lichess.org/<gid>)"),
    ("ts", "i64", "Game start, Unix seconds UTC (from UTCDate + UTCTime)"),
    ("white", "str", "White username"),
    ("black", "str", "Black username"),
    ("welo", "i16", "White rating before the game"),
    ("belo", "i16", "Black rating before the game"),
    ("wdiff", "i16", "White rating change from this game"),
    ("bdiff", "i16", "Black rating change from this game"),
    ("result", "i8", "2 = White won, 1 = draw, 0 = Black won"),
    ("speed", "i8", "0 ultrabullet, 1 bullet, 2 blitz, 3 rapid, 4 classical, 5 correspondence"),
    ("kind", "i8", "0 normal pairing, 1 arena tournament, 2 swiss, 3 other event"),
    ("rated", "bool", "Rated game"),
    ("base", "i32", "Initial clock, seconds"),
    ("inc", "i16", "Increment, seconds"),
    ("term", "i8", "0 normal, 1 time forfeit, 2 abandoned, 3 rules infraction, 4 unterminated, 5 other"),
    ("plies", "i16", "Half-moves played"),
    ("mate", "bool", "Game ended in checkmate"),
    ("wbot", "bool", "White is a BOT account"),
    ("bbot", "bool", "Black is a BOT account"),
    ("wtitled", "bool", "White has a title (GM, IM, FM, ...)"),
    ("btitled", "bool", "Black has a title"),
    ("has_clk", "bool", "Game has clock times"),
    ("w_clk0", "f32", "White clock after first move, s"),
    ("w_clk1", "f32", "White clock after last move, s"),
    ("b_clk0", "f32", "Black clock after first move, s"),
    ("b_clk1", "f32", "Black clock after last move, s"),
    ("w_berserk", "bool", "White berserked (arena)"),
    ("b_berserk", "bool", "Black berserked (arena)"),
    ("w_mt", "f32", "White average seconds per move (first move excluded)"),
    ("b_mt", "f32", "Black average seconds per move (first move excluded)"),
    ("duration", "f32", "Estimated game length, s (from clocks)"),
    ("has_eval", "bool", "Game has Stockfish evals"),
    ("w_nev", "i16", "White moves with evals"),
    ("b_nev", "i16", "Black moves with evals"),
    ("w_acc", "f32", "White accuracy %, Lichess-style"),
    ("b_acc", "f32", "Black accuracy %, Lichess-style"),
    ("w_acpl", "f32", "White average centipawn loss"),
    ("b_acpl", "f32", "Black average centipawn loss"),
    ("w_blunders", "i16", "White blunders (win chance drop >= 15 points)"),
    ("b_blunders", "i16", "Black blunders"),
    ("w_mistakes", "i16", "White mistakes (drop 10-15 points)"),
    ("b_mistakes", "i16", "Black mistakes"),
    ("w_inacc", "i16", "White inaccuracies (drop 5-10 points)"),
    ("b_inacc", "i16", "Black inaccuracies"),
    ("w_maxwp", "i8", "White's best win chance % during the game"),
    ("b_maxwp", "i8", "Black's best win chance % during the game"),
    ("final_wp", "i8", "White's win chance % in the final position"),
]
COLUMN_NAMES = [c[0] for c in COLUMNS]

_SPEED_WORD = {b"UltraBullet": 0, b"Bullet": 1, b"Blitz": 2, b"Rapid": 3,
               b"Classical": 4, b"Correspondence": 5}
_KIND_WORD = {b"game": 0, b"tournament": 1, b"swiss": 2}
_TERM_CODE = {b"Normal": 0, b"Time forfeit": 1, b"Abandoned": 2,
              b"Rules infraction": 3, b"Unterminated": 4}

_HDR_RE = re.compile(rb'^\[(\w+) "(.*)"\]$', re.M)
_CLK_RE = re.compile(rb"%clk\s+(\d+):(\d+):(\d+(?:\.\d+)?)")
_EVAL_RE = re.compile(rb"%eval\s+(#?[+-]?\d+(?:\.\d+)?)")
_COMMENT_RE = re.compile(rb"\{[^}]*\}")
_SAN_START_RE = re.compile(rb"(?:(?<=\s)|^)[NBRQKOa-h]")
_MATE_RE = re.compile(rb"[a-h1-8NBRQKO]#")

_NO_EVAL = (None,) * 15
_START_CP = 15.0  # Lichess treats the start position as +0.15


def _win_pct(cp: float) -> float:
    """Lichess win chance (0-100) for the side the eval favours."""
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-0.00368208 * cp)) - 1.0)


_START_WP = _win_pct(_START_CP)

# ---------------------------------------------------------------- caches
_date_cache: dict = {}
_tc_cache: dict = {}
_event_cache: dict = {}


def _days(d: bytes) -> int:
    v = _date_cache.get(d)
    if v is None:
        v = date(int(d[0:4]), int(d[5:7]), int(d[8:10])).toordinal() - 719163
        if len(_date_cache) < 200_000:
            _date_cache[d] = v
    return v


def _secs(t: bytes) -> int:
    return int(t[0:2]) * 3600 + int(t[3:5]) * 60 + int(t[6:8])


def _time_control(tc: bytes):
    """Return (base, inc, speed) or None if unreadable."""
    v = _tc_cache.get(tc)
    if v is None and tc not in _tc_cache:
        if not tc or tc == b"-":
            v = (0, 0, 5)
        else:
            try:
                b, _, i = tc.partition(b"+")
                base = int(b)
                inc = int(i) if i else 0
                est = base + 40 * inc
                speed = 0 if est < 30 else 1 if est < 180 else 2 if est < 480 else 3 if est < 1500 else 4
                v = (base, inc, speed)
            except ValueError:
                v = None
        if len(_tc_cache) < 20_000:
            _tc_cache[tc] = v
    return v


def _event(ev: bytes):
    """Return (speed or None, kind, rated) from a Lichess Event header.

    Lichess writes e.g. "Rated Blitz game", "Rated Bullet tournament https://...",
    "Rated Rapid swiss https://...". Other names fall back to the time control.
    """
    v = _event_cache.get(ev)
    if v is not None:
        return v
    parts = ev.split(b" ", 3)
    rated = not ev.startswith(b"Casual")
    speed = _SPEED_WORD.get(parts[1]) if len(parts) > 1 else None
    if speed is not None and len(parts) > 2:
        kind = _KIND_WORD.get(parts[2], 3)
    else:
        low = ev.lower()
        kind = 1 if b"arena" in low or b"tournament" in low else 2 if b"swiss" in low else 3
        if ev.endswith(b" game"):
            kind = 0
    v = (speed, kind, rated)
    if b"http" not in ev and len(_event_cache) < 50_000:
        _event_cache[ev] = v
    return v


def _int_or_none(v):
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _clk_at(mt: bytes, pos: int) -> float:
    m = _CLK_RE.match(mt, pos)
    if m is None:
        return -1.0
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


def count_plies(mt: bytes) -> int:
    """Count half-moves in movetext that has no clock comments."""
    if b"{" in mt:
        mt = _COMMENT_RE.sub(b" ", mt)
    return len(_SAN_START_RE.findall(mt))


def eval_features(tokens: list) -> tuple:
    """Accuracy, centipawn loss and error counts from per-ply evals.

    tokens[i] is the eval after half-move i (White's point of view), e.g.
    b"0.17", b"-1.5", b"#3", b"#-2". Mirrors Lichess's accuracy formula.
    """
    if tokens and tokens[-1] in (b"#0", b"#-0", b"#+0"):
        tokens = tokens[:-1]  # "mate in 0" = the game is over; not a move to judge
    cps = []
    for t in tokens:
        if t[0] == 35:  # '#': forced mate
            cps.append(-1000.0 if b"-" in t else 1000.0)
        else:
            v = float(t) * 100.0
            cps.append(1000.0 if v > 1000.0 else (-1000.0 if v < -1000.0 else v))
    n = len(cps)
    if n == 0:
        return _NO_EVAL
    wps = [_START_WP]
    wps.extend(_win_pct(c) for c in cps)
    cpall = [_START_CP]
    cpall.extend(cps)

    # Volatility weights (one per move), as in Lichess.
    L = n + 1
    ws = n // 10
    ws = 2 if ws < 2 else (8 if ws > 8 else ws)
    if ws > L:
        ws = L
    first = wps[:ws]
    m = sum(first) / ws
    sd0 = math.sqrt(sum((x - m) ** 2 for x in first) / ws)
    sd0 = 0.5 if sd0 < 0.5 else (12.0 if sd0 > 12.0 else sd0)
    weights = [sd0] * (ws - 2)
    s = sum(first)
    ss = sum(x * x for x in first)
    for i in range(L - ws + 1):
        if i:
            out_v = wps[i - 1]
            in_v = wps[i + ws - 1]
            s += in_v - out_v
            ss += in_v * in_v - out_v * out_v
        var = ss / ws - (s / ws) ** 2
        sd = math.sqrt(var) if var > 0.0 else 0.0
        weights.append(0.5 if sd < 0.5 else (12.0 if sd > 12.0 else sd))

    acc_w = [0.0, 0.0]   # sum(accuracy * weight)
    wsum = [0.0, 0.0]    # sum(weight)
    harm = [0.0, 0.0]    # sum(1 / accuracy)
    cnt = [0, 0]
    cpl = [0.0, 0.0]
    bl = [0, 0]
    mi = [0, 0]
    ina = [0, 0]
    exp = math.exp
    for j in range(n):
        side = j & 1
        if side == 0:
            drop = wps[j] - wps[j + 1]
            loss = cpall[j] - cpall[j + 1]
        else:
            drop = wps[j + 1] - wps[j]
            loss = cpall[j + 1] - cpall[j]
        if drop <= 0.0:
            acc = 100.0
        else:
            acc = 103.1668100711649 * exp(-0.04354415386753951 * drop) - 3.166924740191411 + 1.0
            acc = 100.0 if acc > 100.0 else (0.0 if acc < 0.0 else acc)
            if drop >= 15.0:
                bl[side] += 1
            elif drop >= 10.0:
                mi[side] += 1
            elif drop >= 5.0:
                ina[side] += 1
        wt = weights[j]
        acc_w[side] += acc * wt
        wsum[side] += wt
        harm[side] += 1.0 / (acc if acc > 1.0 else 1.0)
        cnt[side] += 1
        if loss > 0.0:
            cpl[side] += loss

    out_acc = [None, None]
    out_acpl = [None, None]
    for side in (0, 1):
        if cnt[side]:
            weighted = acc_w[side] / wsum[side]
            harmonic = cnt[side] / harm[side]
            out_acc[side] = (weighted + harmonic) / 2.0
            out_acpl[side] = cpl[side] / cnt[side]
    after = wps[1:]
    w_max = int(round(max(after)))
    b_max = int(round(100.0 - min(after)))
    return (cnt[0], cnt[1], out_acc[0], out_acc[1], out_acpl[0], out_acpl[1],
            bl[0], bl[1], mi[0], mi[1], ina[0], ina[1], w_max, b_max, int(round(wps[-1])))


def parse_game(hdr: bytes, mt: bytes, opts: dict, stats: dict):
    """Turn one game (header block + movetext) into a row tuple, or None if skipped."""
    h = dict(_HDR_RE.findall(hdr))

    variant = h.get(b"Variant")
    if variant is not None and variant not in (b"Standard", b"standard"):
        stats["skip_variant"] = stats.get("skip_variant", 0) + 1
        return None
    res = h.get(b"Result")
    if res == b"1-0":
        result = 2
    elif res == b"0-1":
        result = 0
    elif res == b"1/2-1/2":
        result = 1
    else:
        stats["skip_unfinished"] = stats.get("skip_unfinished", 0) + 1
        return None
    white = h.get(b"White")
    black = h.get(b"Black")
    if not white or not black or white == b"?" or black == b"?":
        stats["skip_no_player"] = stats.get("skip_no_player", 0) + 1
        return None
    try:
        welo = int(h[b"WhiteElo"])
        belo = int(h[b"BlackElo"])
    except (KeyError, ValueError):
        stats["skip_no_rating"] = stats.get("skip_no_rating", 0) + 1
        return None
    try:
        d = h.get(b"UTCDate") or h[b"Date"]
        t = h.get(b"UTCTime") or h[b"Time"]
        ts = _days(d) * 86400 + _secs(t)
    except (KeyError, ValueError, IndexError):
        stats["skip_no_time"] = stats.get("skip_no_time", 0) + 1
        return None
    tcv = _time_control(h.get(b"TimeControl", b"-"))
    if tcv is None:
        stats["skip_bad_time_control"] = stats.get("skip_bad_time_control", 0) + 1
        return None
    base, inc, tc_speed = tcv
    ev_speed, kind, rated = _event(h.get(b"Event", b""))
    speed = ev_speed if ev_speed is not None else tc_speed
    if speed == 5 and not opts.get("keep_correspondence", False):
        stats["skip_correspondence"] = stats.get("skip_correspondence", 0) + 1
        return None
    term_h = h.get(b"Termination")
    term = 0 if term_h is None else _TERM_CODE.get(term_h, 5)
    wdiff = _int_or_none(h.get(b"WhiteRatingDiff"))
    bdiff = _int_or_none(h.get(b"BlackRatingDiff"))
    wt = h.get(b"WhiteTitle")
    bt = h.get(b"BlackTitle")
    wbot = wt == b"BOT"
    bbot = bt == b"BOT"
    wtitled = wt is not None and not wbot and wt not in (b"", b"-", b"?")
    btitled = bt is not None and not bbot and bt not in (b"", b"-", b"?")
    site = h.get(b"Site", b"")
    gid = site.rsplit(b"/", 1)[-1] if site else b""

    # ---- movetext
    n_clk = mt.count(b"%clk") if mt else 0
    plies = n_clk if n_clk else (count_plies(mt) if mt else 0)
    mate = b"#" in mt and _MATE_RE.search(mt) is not None
    wm = (plies + 1) // 2
    bm = plies // 2

    has_clk = n_clk > 0
    w0 = w1 = b0 = b1 = wmt = bmt = dur = None
    wbz = bbz = False
    if has_clk:
        p0 = mt.find(b"%clk")
        w0 = _clk_at(mt, p0)
        q = mt.rfind(b"%clk")
        if n_clk >= 2:
            p1 = mt.find(b"%clk", p0 + 4)
            b0 = _clk_at(mt, p1)
            q2 = mt.rfind(b"%clk", 0, q)
            last = _clk_at(mt, q)
            prev = _clk_at(mt, q2)
            if (n_clk - 1) % 2 == 0:
                w1, b1 = last, prev
            else:
                w1, b1 = prev, last
        else:
            w1 = w0
        if kind == 1 and base > 0:
            wbz = 0.0 <= w0 < 0.75 * base
            bbz = b0 is not None and 0.0 <= b0 < 0.75 * base
        w_inc = 0 if wbz else inc
        b_inc = 0 if bbz else inc
        el_w = w0 - w1 + w_inc * (wm - 1) if wm >= 1 else 0.0
        el_b = (b0 - b1 + b_inc * (bm - 1)) if (bm >= 1 and b0 is not None) else 0.0
        if wm >= 2:
            wmt = max(0.0, el_w / (wm - 1))
        if bm >= 2:
            bmt = max(0.0, el_b / (bm - 1))
        if term == 1:  # flagged: the loser's remaining time ran out too
            if result == 0 and w1 is not None:
                el_w += w1
            elif result == 2 and b1 is not None:
                el_b += b1
        dur = max(0.0, el_w) + max(0.0, el_b)

    evf = _NO_EVAL
    has_eval = False
    if mt and b"%eval" in mt:
        toks = _EVAL_RE.findall(mt)
        # Evals must line up with the moves from the start; the mating move may lack one.
        if toks and len(toks) >= plies - 1 and len(toks) <= plies:
            evf = eval_features(toks)
            has_eval = evf[0] is not None
        else:
            stats["eval_misaligned"] = stats.get("eval_misaligned", 0) + 1

    return (gid, ts, white, black, welo, belo, wdiff, bdiff, result, speed, kind, rated,
            base, inc, term, plies, mate, wbot, bbot, wtitled, btitled,
            has_clk, w0, w1, b0, b1, wbz, bbz, wmt, bmt, dur, has_eval) + evf


def parse_chunk(data: bytes, rows: list, opts: dict, stats: dict) -> None:
    """Parse a chunk of whole games, appending row tuples to `rows`."""
    if b"\r" in data:
        data = data.replace(b"\r\n", b"\n")
    hdr = None
    n_read = 0
    for blk in data.split(b"\n\n"):
        if not blk:
            continue
        c = blk[0]
        if c in (10, 13, 32, 9):
            blk = blk.strip()
            if not blk:
                continue
            c = blk[0]
        if c == 91:  # '[' -> header block
            if hdr is not None:  # previous game had no movetext
                n_read += 1
                _safe_parse(hdr, b"", rows, opts, stats)
            if not blk.endswith(b"]"):
                # Headers and moves without a blank line between them.
                cut = blk.rfind(b"]\n")
                if cut > 0:
                    n_read += 1
                    _safe_parse(blk[:cut + 1], blk[cut + 2:], rows, opts, stats)
                    hdr = None
                    continue
            hdr = blk
        else:
            if hdr is None:
                stats["orphan_text_blocks"] = stats.get("orphan_text_blocks", 0) + 1
                continue
            n_read += 1
            _safe_parse(hdr, blk, rows, opts, stats)
            hdr = None
    if hdr is not None:
        n_read += 1
        _safe_parse(hdr, b"", rows, opts, stats)
    stats["games_read"] = stats.get("games_read", 0) + n_read


def _safe_parse(hdr, mt, rows, opts, stats):
    try:
        row = parse_game(hdr, mt, opts, stats)
    except Exception:  # one broken game must not stop a 30 GB run
        stats["skip_parse_error"] = stats.get("skip_parse_error", 0) + 1
        return
    if row is not None:
        rows.append(row)

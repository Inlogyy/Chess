"""Synthetic games in Lichess PGN format with a known, built-in amount of tilt.

Used by `tilt.py selftest` and to check that the statistics recover the truth.
Each simulated player has a true strength. On top of that:
  * tilt_elo  - after a loss in the same sitting, the player plays this many
                rating points weaker in the next game (more after streaks);
  * form_sd   - a random "good day / bad day" offset per player per day
                (this is NOT tilt, but it makes losses cluster);
  * pace_tilt - after a loss the player spends this fraction of normal time per move;
  * quit_after_loss - extra chance of stopping right after a loss.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone

_SAN = ("e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O h3 Nb8 d4 Nbd7 "
        "c4 e6 Nc3 d5 cxd5 exd5 Bg5 c6 Qc2 Be7 e3 Nbd7 Bd3 O-O Nge2 Re8 O-O Nf8 f3 "
        "Ne6 Bh4 g6 Rad1 Nh5 Bxe7 Qxe7 Kh1 Ng7 g4 h5 Qd2 hxg4 fxg4 Bd7 Rf2 Rad8 "
        "Rdf1 f6 Ng3 Qd6 Nce2 Kh7 Nf4 Nxf4 exf4 Rf8 Qe3 Rde8 Qd2 b6 a3 c5 dxc5 bxc5 "
        "Rxf6 Rxf6 Rxf6 Qxf6 Bxg6+ Kg8 Bxh5 Qh4 Kg2 Bc6+ Kh2 Qf2+ Kh1 Re1+ Ng1").split()
_ADJ = ("quiet brave lucky sharp fast blue red tiny grand dark wild cool mad iron "
        "silent happy rapid lazy royal gold").split()
_NOUN = ("knight bishop rook pawn queen king tiger fox wolf eagle shark lion bear "
         "owl panda dragon mage ninja pirate gambit").split()
_MATES = ("Qh7#", "Qxf7#", "Rd8#", "Qg7#", "Nf7#", "Rh8#", "Qxg7#", "Ra8#")

_TCS = {  # speed -> [(base, inc, weight)]
    "bullet": [(60, 0, 6), (120, 1, 2), (30, 0, 1)],
    "blitz": [(180, 0, 5), (180, 2, 3), (300, 0, 4), (300, 3, 2)],
    "rapid": [(600, 0, 5), (600, 5, 3), (900, 10, 2)],
    "classical": [(1800, 0, 2), (1800, 20, 1)],
}
_SPEED_WORD = {"bullet": "Bullet", "blitz": "Blitz", "rapid": "Rapid", "classical": "Classical"}
_FLAG_P = {"bullet": 0.28, "blitz": 0.15, "rapid": 0.07, "classical": 0.05}


@dataclass
class SynthParams:
    players: int = 3000
    days: int = 14
    seed: int = 7
    tilt_elo: float = 0.0
    streak_extra: float = 0.5      # extra tilt per additional loss in a row (fraction of tilt_elo)
    form_sd: float = 0.0
    pace_tilt: float = 1.0
    quit_after_loss: float = 0.0
    eval_frac: float = 0.08
    k_factor: float = 16.0
    start: str = "2024-03-01"


def _fmt_clk(sec: float) -> str:
    s = int(sec) if sec > 0 else 0
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _fmt_eval(cp: float) -> str:
    if cp >= 1500:
        return "#" + str(max(1, int((3000 - cp) / 150)))
    if cp <= -1500:
        return "#-" + str(max(1, int((3000 + cp) / 150)))
    return f"{cp / 100:.2f}"


def _gid(rng: random.Random) -> str:
    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(rng.choice(chars) for _ in range(8))


def _name(rng: random.Random, i: int) -> str:
    return f"{rng.choice(_ADJ).capitalize()}{rng.choice(_NOUN).capitalize()}{i}"


def _acpl_for(skill: float) -> float:
    return 22.0 + 110.0 * math.exp(-(skill - 700.0) / 650.0)


def _simulate_clocks(rng, plies, base, inc, berserk, flag_side, pace):
    """Return per-ply clock readings and total elapsed seconds."""
    clocks = []
    cur = [base / 2 if berserk[0] else base, base / 2 if berserk[1] else base]
    incs = [0 if berserk[0] else inc, 0 if berserk[1] else inc]
    moves_left = [(plies + 1) // 2, plies // 2]
    used_frac = [rng.uniform(0.35, 0.95), rng.uniform(0.35, 0.95)]
    if flag_side is not None:
        used_frac[flag_side] = 1.0
    elapsed = 0.0
    for p in range(plies):
        side = p & 1
        if p >= 2:
            remaining = moves_left[side]
            share = used_frac[side] * cur[side] / (remaining + 3)
            spend = share * rng.uniform(0.2, 1.8) * pace[side] + incs[side] * rng.uniform(0.3, 1.0)
            spend = min(spend, cur[side] + incs[side] - 0.2)
            spend = max(spend, 0.1)
            cur[side] = cur[side] - spend + incs[side]
            elapsed += spend
        moves_left[side] -= 1
        clocks.append(cur[side])
    if flag_side is not None:
        elapsed += clocks[-1] if clocks else 0.0
    return clocks, elapsed


DRAW_CP = 50.0


def _play(rng, plies, skills):
    """Simulate a game move by move as an engine evaluation (White's view, centipawns).

    Every move gives away a little value; weaker, tired or tilted players give away more
    and blunder more often. The result then follows from the final evaluation, as in real
    chess, so a player's accuracy and the game's result are linked the realistic way.
    """
    ev = []
    e = 15.0
    mean_loss = (_acpl_for(skills[0]), _acpl_for(skills[1]))
    for p in range(plies):
        side = p & 1
        loss = rng.expovariate(1.0 / mean_loss[side]) * 0.6
        if rng.random() < mean_loss[side] / 1400.0:
            loss += rng.uniform(150, 500)
        e = e - loss if side == 0 else e + loss
        e = max(-2200.0, min(2200.0, e))
        ev.append(e)
    return ev


def generate(path: str, params: SynthParams | None = None, quiet: bool = False) -> dict:
    """Write a synthetic PGN file. Returns a summary dict."""
    P = params or SynthParams()
    rng = random.Random(P.seed)
    t0 = int(datetime.strptime(P.start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())

    players = []
    for i in range(P.players):
        skill = min(2850.0, max(450.0, rng.gauss(1500, 380)))
        prefs = [("bullet", rng.random() * 3), ("blitz", rng.random() * 5),
                 ("rapid", rng.random() * 2), ("classical", rng.random() * 0.3)]
        players.append({
            "name": _name(rng, i), "skill": skill,
            "rating": {s: skill + rng.gauss(0, 60) for s in _TCS},
            "activity": rng.uniform(0.15, 1.2), "prefs": prefs,
        })
    n_bg = max(20000, P.players * 25)
    bg_names = [f"{rng.choice(_ADJ)}_{rng.choice(_NOUN)}{rng.randrange(10**6)}" for _ in range(n_bg)]
    bots = ["maia_like_BOT", "stockfish_jr_BOT", "toaster_BOT"]

    games = []
    for d in range(P.days):
        day_start = t0 + d * 86400
        for pl in players:
            form = rng.gauss(0, P.form_sd) if P.form_sd > 0 else 0.0
            n_sess = sum(1 for _ in range(4) if rng.random() < pl["activity"] / 4)
            for _ in range(n_sess):
                total = sum(w for _, w in pl["prefs"])
                r = rng.random() * total
                for speed, w in pl["prefs"]:
                    r -= w
                    if r <= 0:
                        break
                tcs = _TCS[speed]
                r = rng.random() * sum(w for _, _, w in tcs)
                for base, inc, w in tcs:
                    r -= w
                    if r <= 0:
                        break
                arena = rng.random() < 0.15
                tour_id = _gid(rng)
                t = day_start + rng.randrange(0, 86400 - 3600)
                loss_streak = 0
                prev_loss = False
                while True:
                    rating = pl["rating"][speed]
                    is_bot = rng.random() < 0.01
                    if is_bot:
                        opp = rng.choice(bots)
                        opp_rating = int(rating + rng.gauss(0, 120))
                        opp_skill = opp_rating
                    else:
                        opp = rng.choice(bg_names)
                        opp_rating = int(rating + rng.gauss(0, 75))
                        opp_skill = opp_rating + rng.gauss(0, 35)
                    eff = pl["skill"] + form
                    if loss_streak > 0:
                        eff -= P.tilt_elo * (1 + P.streak_extra * (min(loss_streak, 4) - 1))
                    white = rng.random() < 0.5
                    sk = [eff + 20, opp_skill] if white else [opp_skill + 20, eff]  # small edge for White
                    abandoned = rng.random() < 0.004
                    plies = rng.randint(3, 30) if abandoned else int(min(250, max(8, rng.gauss(75, 28))))
                    evs = _play(rng, plies + 1, sk)
                    final = evs[plies - 1]
                    res_code = 1 if abs(final) < DRAW_CP else (2 if final > 0 else 0)
                    if abandoned and res_code == 1:
                        res_code = rng.choice((0, 2))
                    if res_code == 1:
                        score = 0.5
                    else:
                        score = 1.0 if (res_code == 2) == white else 0.0
                    Er = 1 / (1 + 10 ** (-(rating - opp_rating) / 400))
                    delta = int(round(P.k_factor * (score - Er)))

                    # How the game ended.
                    term = "Abandoned" if abandoned else "Normal"
                    mate = False
                    flag_side = None  # 0 = White flagged, 1 = Black flagged
                    if score != 0.5 and not abandoned:
                        x = rng.random()
                        if x < _FLAG_P[speed]:
                            term = "Time forfeit"
                        elif x < _FLAG_P[speed] + 0.002:
                            term = "Rules infraction"
                        elif rng.random() < 0.3:
                            mate = True
                    me_white = white
                    white_won = None if score == 0.5 else (res_code == 2)
                    loser_side = None if white_won is None else (1 if white_won else 0)
                    if term in ("Time forfeit", "Normal") and loser_side is not None:
                        # The losing side is the one to move: White to move after an even ply count.
                        if (plies & 1) != loser_side:
                            plies += 1
                    if term == "Time forfeit":
                        flag_side = loser_side
                    berserk = (arena and rng.random() < 0.15, arena and rng.random() < 0.12)
                    my_side = 0 if me_white else 1
                    pace = [1.0, 1.0]
                    if prev_loss:
                        pace[my_side] = P.pace_tilt
                    clocks, elapsed = _simulate_clocks(rng, plies, base, inc, berserk, flag_side, pace)
                    if rng.random() >= P.eval_frac:
                        evs = None

                    # Movetext.
                    parts = []
                    for p in range(plies):
                        if p == plies - 1 and mate:
                            san = rng.choice(_MATES)
                        else:
                            san = _SAN[p % len(_SAN)]
                            if san.endswith("#"):
                                san = san[:-1]
                        if evs is not None and p > 0:
                            dropv = (evs[p - 1] - evs[p]) if (p & 1) == 0 else (evs[p] - evs[p - 1])
                            if dropv > 300:
                                san += "??"
                            elif dropv > 150:
                                san += "?"
                        num = f"{p // 2 + 1}." if (p & 1) == 0 else f"{p // 2 + 1}..."
                        com = []
                        if evs is not None and not (p == plies - 1 and mate):
                            com.append(f"[%eval {_fmt_eval(evs[p])}]")
                        com.append(f"[%clk {_fmt_clk(clocks[p])}]")
                        parts.append(f"{num} {san} {{ {' '.join(com)} }}")
                    res_str = "1/2-1/2" if score == 0.5 else ("1-0" if white_won else "0-1")
                    movetext = " ".join(parts) + " " + res_str

                    me, opp_name = pl["name"], opp
                    w_name, b_name = (me, opp_name) if me_white else (opp_name, me)
                    w_elo, b_elo = (int(rating), opp_rating) if me_white else (opp_rating, int(rating))
                    w_d, b_d = (delta, -delta) if me_white else (-delta, delta)
                    sw = _SPEED_WORD[speed]
                    event = (f"Rated {sw} tournament https://lichess.org/tournament/{tour_id}"
                             if arena else f"Rated {sw} game")
                    dt = datetime.fromtimestamp(t, tz=timezone.utc)
                    hdr = [
                        f'[Event "{event}"]',
                        f'[Site "https://lichess.org/{_gid(rng)}"]',
                        f'[Date "{dt:%Y.%m.%d}"]',
                        '[Round "-"]',
                        f'[White "{w_name}"]',
                        f'[Black "{b_name}"]',
                        f'[Result "{res_str}"]',
                        f'[UTCDate "{dt:%Y.%m.%d}"]',
                        f'[UTCTime "{dt:%H:%M:%S}"]',
                        f'[WhiteElo "{w_elo}"]',
                        f'[BlackElo "{b_elo}"]',
                        f'[WhiteRatingDiff "{w_d:+d}"]',
                        f'[BlackRatingDiff "{b_d:+d}"]',
                    ]
                    if is_bot:
                        hdr.append(f'[{"Black" if me_white else "White"}Title "BOT"]')
                    hdr += ['[ECO "C60"]', '[Opening "Ruy Lopez"]',
                            f'[TimeControl "{base}+{inc}"]', f'[Termination "{term}"]']
                    games.append((t, "\n".join(hdr) + "\n\n" + movetext + "\n\n"))

                    pl["rating"][speed] = rating + delta
                    lost = score == 0.0
                    loss_streak = loss_streak + 1 if lost else 0
                    prev_loss = lost
                    # Keep playing?
                    stay = 0.82 * (1 - P.quit_after_loss) if lost else 0.82
                    if rng.random() > stay:
                        break
                    gap = rng.expovariate(1 / (12.0 if lost else 20.0))
                    if rng.random() < 0.08:
                        gap += rng.uniform(120, 1500)
                    t = int(t + elapsed + 4 + gap)
    games.sort(key=lambda g: g[0])
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for _, txt in games:
            f.write(txt)
    if not quiet:
        print(f"wrote {len(games):,} synthetic games to {path}")
    return {"games": len(games), "players": P.players}

"""Check the tilt estimators on simulated games where the truth is known.

Run after changing anything in chesstilt/analyze.py or chesstilt/build.py:

    python tests/validate_methods.py            (about 5 minutes, 4 scenarios x ~300k games)

Expected pattern (points of score, loss minus win):
  A_none   every estimator ~0
  B_tilt   every estimator clearly negative (same-sitting a bit smaller: it partly
           cancels tilt that lasts several games)
  C_form   simple / player-adjusted / like-for-like slightly negative (bad days look
           like tilt); same-sitting and same-accuracy ~0
  D_mixed  simple more negative than B; same-sitting close to B
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chesstilt.analyze import run_analysis  # noqa: E402
from chesstilt.build import build  # noqa: E402
from chesstilt.extract import extract  # noqa: E402
from chesstilt.synth import SynthParams, generate  # noqa: E402

SCENARIOS = {
    "A_none": dict(),
    "B_tilt": dict(tilt_elo=40.0),
    "C_form": dict(form_sd=60.0),
    "D_mixed": dict(tilt_elo=40.0, form_sd=60.0, pace_tilt=0.85, quit_after_loss=0.15),
}
CFG = {"session_gap_minutes": 30, "max_rating_change": 30, "min_trigger_plies": 10,
       "min_games_within_player": 10, "speeds": ["ultrabullet", "bullet", "blitz", "rapid", "classical"],
       "memory_limit": "", "threads": 0}


def _all(rows, grp=None):
    return next(r for r in rows if r["by"] == "All" and (grp is None or r.get("grp") == grp))


def main():
    base = Path(tempfile.mkdtemp(prefix="tilt_validate_"))
    print(f"{'scenario':9s} | {'simple':>13s} {'player-adj':>13s} {'like-for-like':>13s} "
          f"{'same-sitting':>13s} {'same-acc':>13s}   (points, +-95%)")
    try:
        for name, extra in SCENARIOS.items():
            t = time.time()
            d = base / name
            d.mkdir()
            pgn = d / f"{name}.pgn"
            generate(str(pgn), SynthParams(players=6000, days=14, seed=11, **extra), quiet=True)
            extract([pgn], d / "out", workers=2, log=lambda *a: None)
            pgn.unlink()
            build(d / "out", CFG, log=lambda *a: None, force=True)
            R = run_analysis(d / "out", CFG, log=lambda *a: None)
            rob = R["robust"]
            cells = [
                (_all(R["headline"], "L")["diff"], _all(R["headline"], "L")["se_diff"]),
                (_all(rob["player_adjusted"], "L")["diff"], _all(rob["player_adjusted"], "L")["se_diff"]),
                (_all(rob["controlled"])["est"], _all(rob["controlled"])["se"]),
                (_all(rob["same_sitting"])["est"], _all(rob["same_sitting"])["se"]),
                (_all(R["luck"]["controlled"])["est"], _all(R["luck"]["controlled"])["se"]),
            ]
            txt = " ".join(f"{100 * e:+6.2f}+-{196 * s:4.2f}" for e, s in cells)
            print(f"{name:9s} | {txt}   [{time.time() - t:.0f} s]", flush=True)
            shutil.rmtree(d, ignore_errors=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()

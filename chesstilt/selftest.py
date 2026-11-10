"""End-to-end self test on made-up games with a known amount of tilt built in."""
from __future__ import annotations

import shutil
import time
import webbrowser
from pathlib import Path


def run_selftest(folder: Path, cfg: dict, log=print, open_report=True) -> bool:
    from .analyze import run_analysis, save_tables
    from .build import build
    from .extract import extract
    from .report import build_report, write_results
    from .synth import SynthParams, generate

    t0 = time.time()
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    pgn = folder / "selftest_games.pgn"
    log("Self test: making about 90,000 practice games with tilt built in ...")
    generate(str(pgn), SynthParams(players=2500, days=10, seed=5, tilt_elo=80.0), quiet=True)
    log("Reading them with 2 worker processes ...")
    extract([pgn], folder, workers=2, log=lambda *a: None)
    log("Building timelines ...")
    build(folder, cfg, log=lambda *a: None, force=True)
    log("Running the statistics ...")
    R = run_analysis(folder, cfg, log=lambda *a: None)
    write_results(R, folder / "report")
    save_tables(R, folder / "report" / "tables")
    rep = build_report(R, folder / "report" / "report.html", title_note=" · SELF TEST (made-up games)")
    pgn.unlink()

    head = next((r for r in R["headline"] if r["by"] == "All" and r["grp"] == "L"), None)
    same = next((r for r in R["robust"]["same_sitting"] if r["by"] == "All"), None)
    checks = [
        ("games were read and kept", R["overview"]["games"] > 20000),
        ("the built-in tilt is detected", head is not None and head["diff"] < -0.02 and head["diff"] < -3 * head["se_diff"]),
        ("the bad-day check also finds it", same is not None and same["est"] < -0.02),
        ("the report was written", rep.exists() and rep.stat().st_size > 20000),
    ]
    log("")
    for name, ok in checks:
        log(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    ok = all(c for _, c in checks)
    log(f"\nSelf test {'PASSED' if ok else 'FAILED'} in {time.time() - t0:.0f} s. Report: {rep}")
    if open_report and rep.exists():
        try:
            webbrowser.open(rep.resolve().as_uri())
        except Exception:
            pass
    return ok

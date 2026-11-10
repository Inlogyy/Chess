#!/usr/bin/env python3
"""chess-tilt: how much worse do people play after they lose?

Reads Lichess PGN dumps (.pgn.zst, .pgn, .bz2, .gz), keeps the games that fit,
and measures post-loss tilt. On Windows type `py` instead of `python`.

  python tilt.py run FILE [FILE ...]   do everything: extract, build, report
  python tilt.py selftest              2-minute check that everything works
  python tilt.py inspect FILE          show how the program reads your file
  python tilt.py extract FILE [...]    step 1 only (read and filter the games)
  python tilt.py build                 step 2 only (player timelines)
  python tilt.py report                step 3 only (statistics and report)

Common options:
  --out FOLDER     where results go (default: a "tilt_output" folder next to the first file)
  --limit N        only read the first N games of each file (quick trial run)
  --workers N      worker processes for reading (default: CPU cores minus one)
  --config FILE    settings file (default: config.toml next to this program)
  --rebuild        rebuild the player timelines even if they look up to date
  --no-open        do not open the report in the browser at the end
"""
from __future__ import annotations

import argparse
import sys
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAST = HERE / ".last_output"


def _log(msg=""):
    print(msg, flush=True)


def _need_packages():
    missing = []
    for mod, pip in (("duckdb", "duckdb"), ("pyarrow", "pyarrow"), ("numpy", "numpy")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip)
    if missing:
        raise SystemExit("Missing Python packages: " + ", ".join(missing) +
                         "\nInstall them with:  py -m pip install -r requirements.txt")


def _out_dir(args, inputs) -> Path:
    if args.out:
        return Path(args.out).expanduser().resolve()
    if inputs:
        return (Path(inputs[0]).resolve().parent / "tilt_output")
    if LAST.exists():
        p = Path(LAST.read_text(encoding="utf-8").strip())
        if p.exists():
            return p
    return (Path.cwd() / "tilt_output").resolve()


def _inputs(paths) -> list:
    files = []
    for p in paths:
        pp = Path(p).expanduser()
        if any(ch in str(p) for ch in "*?"):
            files.extend(sorted(pp.parent.glob(pp.name)))
        elif pp.is_dir():
            files.extend(sorted(f for f in pp.iterdir() if f.name.lower().endswith((".pgn", ".pgn.zst", ".pgn.bz2", ".pgn.gz", ".zst"))))
        elif pp.exists():
            files.append(pp)
        else:
            raise SystemExit(f"File not found: {p}")
    if not files:
        raise SystemExit("No PGN files given.")
    return files


def step_extract(args, cfg, inputs, out):
    from chesstilt.extract import extract
    t = time.time()
    _log(f"\n[1/3] Reading games -> {out / 'games'}")
    extract(inputs, out, workers=args.workers or int(cfg["workers"]), chunk_mb=int(cfg["chunk_mb"]),
            limit=args.limit or 0, opts={"keep_correspondence": bool(cfg["keep_correspondence"])}, log=_log)
    _log(f"      done in {time.time() - t:.0f} s")


def step_build(args, cfg, out):
    from chesstilt.build import build
    t = time.time()
    _log(f"\n[2/3] Building player timelines -> {out / 'export'}")
    build(out, cfg, log=_log, force=args.rebuild)
    _log(f"      done in {time.time() - t:.0f} s")


def step_report(args, cfg, out) -> Path:
    from chesstilt.analyze import run_analysis, save_tables
    from chesstilt.build import build
    from chesstilt.report import build_report, write_results
    build(out, cfg, log=_log)  # quick no-op when up to date; rebuilds if settings changed
    t = time.time()
    _log(f"\n[3/3] Statistics and report -> {out / 'report'}")
    R = run_analysis(out, cfg, log=_log)
    rep = out / "report"
    write_results(R, rep)
    save_tables(R, rep / "tables")
    note = f" · first {args.limit:,} games per file" if getattr(args, "limit", 0) else ""
    path = build_report(R, rep / "report.html", title_note=note)
    _log(f"      done in {time.time() - t:.0f} s")
    _log(f"\nReport: {path}")
    _log(f"Tables: {rep / 'tables'}")
    _log(f"Clean dataset: {out / 'export'}")
    if not args.no_open:
        try:
            webbrowser.open(path.resolve().as_uri())
        except Exception:
            pass
    return path


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(prog="tilt.py", description="Measure post-loss tilt in Lichess games.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("command", choices=["run", "extract", "build", "report", "inspect", "selftest"])
    ap.add_argument("files", nargs="*")
    ap.add_argument("--out")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)
    sys.path.insert(0, str(HERE))

    if args.command == "inspect":
        from chesstilt.extract import inspect_file
        for f in _inputs(args.files):
            inspect_file(f, log=_log)
        return
    _need_packages()
    from chesstilt import settings
    cfg = settings.load(Path(args.config))

    if args.command == "selftest":
        from chesstilt.selftest import run_selftest
        ok = run_selftest(HERE / "selftest_output", cfg, log=_log, open_report=not args.no_open)
        sys.exit(0 if ok else 1)

    inputs = _inputs(args.files) if args.command in ("run", "extract") else []
    out = _out_dir(args, inputs)
    out.mkdir(parents=True, exist_ok=True)
    try:
        LAST.write_text(str(out), encoding="utf-8")
    except OSError:
        pass
    t0 = time.time()
    _log(f"Output folder: {out}")
    if args.command in ("run", "extract"):
        step_extract(args, cfg, inputs, out)
    if args.command in ("run", "build"):
        step_build(args, cfg, out)
    if args.command in ("run", "report"):
        step_report(args, cfg, out)
    _log(f"\nAll done in {(time.time() - t0) / 60:.1f} min.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to continue; finished files are not redone.")
        sys.exit(130)
    except MemoryError:
        print("\nRan out of memory. Try:  --workers 2  and/or set memory_limit = \"4GB\" in config.toml.")
        sys.exit(1)
    except OSError as e:
        if getattr(e, "errno", None) == 28 or getattr(e, "winerror", None) == 112:
            print("\nThe disk is full. Free some space or pick a folder on a bigger drive with --out.")
            sys.exit(1)
        raise

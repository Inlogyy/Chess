# Notes for working on this repository

chess-tilt: measures post-loss tilt in Lichess PGN dumps (30 GB+ / 100M+ games).
The owner runs it on **Windows**, on their own Lichess dump; keep that path working.

## Pipeline

| Stage | File | Output |
|---|---|---|
| 1. extract | `chesstilt/pgnparse.py`, `chesstilt/extract.py` | `<out>/games/<file>/part-NN.parquet` (one row per game) |
| 2. build | `chesstilt/build.py` (DuckDB) | `<out>/export/player_games/part-NNN.parquet` (one row per player per game, with previous game, session, streaks, expected score, residual) |
| 3. report | `chesstilt/analyze.py`, `chesstilt/report.py`, `chesstilt/svgchart.py` | `<out>/report/report.html`, `tables/*.csv`, `results.json` |

Entry point: `tilt.py` (`run`, `extract`, `build`, `report`, `inspect`, `selftest`).
Settings: `config.toml` -> `chesstilt/settings.py`. Windows launcher: `RUN_TILT.bat`.

## Testing

- `python tests/test_parser.py`: parser unit tests (fast).
- `python tilt.py selftest --no-open`: end-to-end on ~90k synthetic games (~20 s).
- `python tests/validate_methods.py`: re-validates every estimator against simulated
  games with known tilt / bad-day effects (~5 min). **Run it after any change to
  `analyze.py` or `build.py`.** `chesstilt/synth.py` generates the games; its results come
  from simulated move quality, so "same accuracy" comparisons are meaningful.

## Method decisions (validated; don't undo without re-running the validation)

- Main quantity: residual = actual score minus expected score, where expected score is a
  fractional-logit fit on the data (per speed, colour, 100-pt rating band).
- All standard errors are clustered by player (influence-function SQL in `analyze.Stats`).
- **Do not** use per-player "mean after loss minus mean after win": finite-sequence
  (Miller-Sanjurjo / hot-hand) bias. Player adjustment uses a leave-two-out player mean.
- **Do not** use a "game before vs. game after the loss" difference-in-differences: tilt
  itself makes losses cluster, so it cancels real tilt (it read ~0 with 40 Elo of tilt).
- The bad-day control is `Stats.within_session`: exact within-session permutation
  expectation, so no small-sample bias; it slightly understates multi-game tilt.
- Stratified ("like-for-like") estimators must use coarse strata: fine strata end up
  within single players and inherit the hot-hand bias.

## Compatibility gotchas

- Multiprocessing uses the `spawn` context everywhere (Windows behaviour); worker
  functions must stay importable at module level.
- Console output must stay ASCII (Windows code pages).
- `RUN_TILT.bat` is stored with CRLF line endings and marked `-text` in `.gitattributes`.
- DuckDB: no `x -> expr` lambdas (removed in 2.0, `lambda x:` is missing in old 1.x);
  `//` on floats is true division (use `floor(a / b)`); `by` is a reserved word.
- Python 3.11-3.13 (needs `tomllib`; wheels for duckdb/pyarrow/zstandard).

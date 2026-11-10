# chess-tilt

Measures **post-loss tilt** in Lichess games: how much worse people play in the
game right after a loss, and whether that is the loss itself or just a bad day.

It reads Lichess database dumps directly (`lichess_db_standard_rated_YYYY-MM.pgn.zst`,
also plain `.pgn`, `.bz2`, `.gz`), streams through 30 GB+ / 100M+ games using all
CPU cores with flat memory use, removes the games that don't fit, and writes:

- `report\report.html`: the big-picture report (opens in your browser)
- `report\tables\*.csv`: every number in the report, for Excel
- `export\player_games\*.parquet`: the clean dataset, one row per player per game,
  with previous result, streaks, sessions, expected score and more
  (column guide in `export\README.txt`)
- `games\...\*.parquet`: one row per game, straight from the PGN

## Windows: step by step

1. **Install Python 3.13** from <https://www.python.org/downloads/windows/>.
   On the first installer screen tick **"Add python.exe to PATH"**.
   (3.11 and 3.12 also work. Avoid brand-new versions such as 3.15: some packages lag behind.)
2. **Download this repository**: on its GitHub page click **Code → Download ZIP**, then
   unzip it somewhere, e.g. `Documents\Chess`. (Or `git clone` it.)
3. **Double-click `RUN_TILT.bat`** and type `selftest`.
   The first time it installs its packages (about a minute, needs internet). The self
   test makes about 90,000 practice games with tilt built in, analyses them, checks that the
   tilt is found, and opens a sample report. Takes 1–2 minutes.
4. **Drag your `.pgn.zst` file onto `RUN_TILT.bat`.** That's it. Results go to a
   `tilt_output` folder next to your file, and the report opens when it's done.

### Quick trial before the full run

Open a terminal in the program folder (in Explorer: click the address bar, type `cmd`, press Enter):

```
.venv\Scripts\python tilt.py run "D:\lichess\lichess_db_standard_rated_2024-01.pgn.zst" --limit 2000000
```

This reads only the first 2 million games (a few minutes). Running the full command
later redoes that file automatically.

### Time, disk and memory for one 30 GB month (~90–100M games)

| Step | 4-core laptop | 8-core desktop |
|---|---|---|
| 1. Reading and filtering | ~25 min | ~12 min |
| 2. Player timelines | ~15 min | ~8 min |
| 3. Statistics and report | ~8 min | ~4 min |

Scaled up from test runs (reading: ~69,000 games/s with 3 worker processes; timelines
and statistics for 4.2M games: 24 s and 12 s on 4 cores). The program prints its real
progress and time left as it goes.

- **Disk:** about 20–25 GB free on the drive holding `tilt_output` (about 4 GB of games
  plus 12 GB of clean dataset per month, plus temporary files while building). Use
  `--out E:\somewhere` to pick another drive. It never needs the unzipped PGN (~210 GB):
  it reads the `.zst` directly.
- **Memory:** 8 GB works; 16 GB is comfortable. If you run out, set
  `memory_limit = "4GB"` in `config.toml` and/or add `--workers 2`.
- **Stopping:** Ctrl+C is safe. Run the same command again; finished files are
  skipped and the rest is redone.

### All commands

```
.venv\Scripts\python tilt.py run FILE [FILE ...]   everything (several months at once is fine)
.venv\Scripts\python tilt.py inspect FILE          show how the first games of a file are read
.venv\Scripts\python tilt.py report                redo only the statistics (e.g. after editing config.toml)
.venv\Scripts\python tilt.py selftest              the self test
```

Options: `--out FOLDER`, `--limit N`, `--workers N`, `--config FILE`, `--rebuild`, `--no-open`.

## Which games are removed

Removed when reading:
- correspondence games (days per move; set `keep_correspondence = true` to keep)
- chess variants, unfinished/aborted games, games missing ratings, time or players

Kept in each player's timeline but not counted in the analysis:
- games involving a **BOT** account
- casual games, games ended for **rules infractions** (cheating), unterminated games
- games where the player's rating moved more than 30 points (a new, still-settling
  "provisional" rating; set `max_rating_change`)
- as the *previous* game: abandoned games and games under 10 half-moves (`min_trigger_plies`)

They stay in the timeline so that "the next game" is always the real next game.

## How tilt is measured

Every game gets a **predicted score** from the two ratings (fitted on your data, by
speed, colour and rating band). The program compares the actual score with the
prediction in the **next game in the same sitting** (started within 30 minutes of the
previous one ending; set `session_gap_minutes`), after a loss vs. after a win.

1. **Results vs. rating:** next-game score vs. prediction, overall and by speed,
   rating, losing/winning streak length, break length, and kind of loss (on time,
   checkmated, upset, blew a winning position, ...).
2. **Move quality:** accuracy (Lichess formula), average centipawn loss, blunders,
   mistakes and inaccuracies, from the Stockfish evaluations in the dump (about 6% of
   games, still millions).
3. **Behaviour:** time before re-queueing ("rage queue"), time per move, losing on
   time, early resignations, abandoning, berserking, rematches, switching to a faster
   time control, and stopping after a loss.

### Telling tilt apart from a bad day

A player who is tired or off form loses more *and* plays worse afterwards, even if
the loss itself didn't upset them. The report shows these estimates side by side:

| Check | What it removes |
|---|---|
| Simple comparison | nothing (the headline number) |
| Each player vs. their own average | differences between players (e.g. under-rated improving players) |
| Like-for-like games | differences in speed, rating, game number in the sitting, event type |
| **Same sitting vs. random order** | anything constant over a sitting: bad days, tiredness, mood |
| Same accuracy in the lost game | form that shows up in the moves (engine-analysed games) |

"Same sitting vs. random order" asks whether a loss is followed by a worse *next*
game than by a random other game from the same sitting, computed exactly rather
than by simulation. It is the best single estimate of tilt itself; it can slightly
understate tilt that lasts several games, so the truth most likely lies between it
and the simple comparison.

All 95% ranges are **clustered by player**, so people with thousands of games cannot
make results look more certain than they are.

### Checked on simulated data

The methods were tested on made-up games in exact Lichess format where the truth is
known: no tilt, 40 rating points of tilt, "bad days" without tilt, and both mixed.
Two tempting methods failed those tests and were left out:
- per-player "average after losses minus average after wins" (biased in short
  records, the known hot-hand statistics trap);
- comparing the game *before* a loss with the game *after* it, which cancels real
  tilt, because tilt itself makes losses cluster.

### Caveats

- Players who stop after a loss can't be measured after it (the report shows how often that happens).
- The dumps hold rated standard games only, so casual and variant games in between are invisible.
- Engine evaluations exist only for games someone asked Lichess to analyse, which is not a random sample.
- It is observational data: the checks remove the obvious alternative explanations,
  but they are not a controlled experiment.

## Using the clean dataset yourself

```python
import duckdb
duckdb.sql("""
    SELECT p_s2 AS previous_result, avg(resid) AS score_vs_prediction, count(*) AS games
    FROM 'tilt_output/export/player_games/*.parquet'
    WHERE elig AND p_ok AND NOT new_sess
    GROUP BY 1 ORDER BY 1
""").show()
```

`pandas.read_parquet`, Polars, R's `arrow` package and Excel Power Query read the files too.

## Tests

```
.venv\Scripts\python tests\test_parser.py         parser unit tests (seconds)
.venv\Scripts\python tilt.py selftest --no-open    whole pipeline on practice games (~20 s)
.venv\Scripts\python tests\validate_methods.py    every estimator vs. known simulated effects (~5 min)
```

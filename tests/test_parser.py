"""Parser tests. Run with:  python -m pytest tests   (or)   python tests/test_parser.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chesstilt.pgnparse import COLUMN_NAMES, eval_features, parse_chunk  # noqa: E402

LICHESS_EVAL_GAME = b"""[Event "Rated Blitz game"]
[Site "https://lichess.org/j1dkb5dw"]
[Date "2024.01.01"]
[Round "-"]
[White "BFG9k"]
[Black "mamalak"]
[Result "0-1"]
[UTCDate "2024.01.01"]
[UTCTime "23:01:03"]
[WhiteElo "1639"]
[BlackElo "1403"]
[WhiteRatingDiff "-12"]
[BlackRatingDiff "+12"]
[ECO "C00"]
[Opening "French Defense: Normal Variation"]
[TimeControl "180+2"]
[Termination "Normal"]

1. e4 { [%eval 0.17] [%clk 0:03:00] } 1... e6 { [%eval 0.19] [%clk 0:03:00] } 2. d4 { [%eval 0.25] [%clk 0:02:58] } 2... b6 { [%eval 0.62] [%clk 0:02:59] } 3. Bd3?? { [%eval -4.14] [%clk 0:02:50] } 3... Bb7 { [%eval -4.10] [%clk 0:02:55] } 4. Qh5 { [%eval #-3] [%clk 0:02:40] } 4... Qg5 { [%eval #-2] [%clk 0:02:50] } 5. f3 { [%eval #-1] [%clk 0:02:30] } 5... Qe3# { [%clk 0:02:45] } 0-1

"""

BERSERK_GAME = b"""[Event "Rated Bullet tournament https://lichess.org/tournament/abcdEFGH"]
[Site "https://lichess.org/zzzzzzzz"]
[White "a"]
[Black "b"]
[Result "1-0"]
[UTCDate "2024.01.02"]
[UTCTime "10:00:00"]
[WhiteElo "1500"]
[BlackElo "1500"]
[WhiteRatingDiff "+6"]
[BlackRatingDiff "-6"]
[WhiteTitle "BOT"]
[BlackTitle "FM"]
[TimeControl "60+0"]
[Termination "Time forfeit"]

1. e4 { [%clk 0:00:30] } 1... e5 { [%clk 0:01:00] } 2. Nf3 { [%clk 0:00:28] } 2... Nc6 { [%clk 0:00:50] } 3. Bc4 { [%clk 0:00:20] } 3... Bc5 { [%clk 0:00:02] } 1-0

"""

NO_CLOCK_GAME = b"""[Event "Rated Classical game"]
[Site "https://lichess.org/aaaaaaaa"]
[White "x"]
[Black "y"]
[Result "1/2-1/2"]
[UTCDate "2013.01.01"]
[UTCTime "00:00:01"]
[WhiteElo "1500"]
[BlackElo "1600"]
[TimeControl "1800+0"]
[Termination "Normal"]

1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. O-O Nf6 5. Re1 1/2-1/2

"""

SKIPPED_GAMES = b"""[Event "Rated Correspondence game"]
[Site "https://lichess.org/cccccccc"]
[White "x"]
[Black "y"]
[Result "1-0"]
[UTCDate "2013.01.01"]
[UTCTime "00:00:01"]
[WhiteElo "1500"]
[BlackElo "1600"]
[TimeControl "-"]
[Termination "Normal"]

1. e4 e5 1-0

[Event "Rated Blitz game"]
[Site "https://lichess.org/dddddddd"]
[White "x"]
[Black "y"]
[Result "*"]
[UTCDate "2013.01.01"]
[UTCTime "00:00:01"]
[WhiteElo "1500"]
[BlackElo "1600"]
[TimeControl "300+0"]
[Termination "Unterminated"]

1. e4 *

[Event "Rated Blitz game"]
[Site "https://lichess.org/eeeeeeee"]
[White "x"]
[Black "y"]
[Result "1-0"]
[UTCDate "2013.01.01"]
[UTCTime "00:00:01"]
[WhiteElo "?"]
[BlackElo "1600"]
[TimeControl "300+0"]
[Termination "Normal"]

1. e4 1-0

"""


def _rows(data, **opts):
    rows, stats = [], {}
    parse_chunk(data, rows, opts, stats)
    return [dict(zip(COLUMN_NAMES, r)) for r in rows], stats


def test_lichess_eval_game():
    rows, stats = _rows(LICHESS_EVAL_GAME)
    assert len(rows) == 1 and stats["games_read"] == 1
    g = rows[0]
    assert g["gid"] == b"j1dkb5dw" and g["white"] == b"BFG9k" and g["black"] == b"mamalak"
    assert g["result"] == 0 and g["welo"] == 1639 and g["wdiff"] == -12 and g["bdiff"] == 12
    assert g["speed"] == 2 and g["kind"] == 0 and g["base"] == 180 and g["inc"] == 2
    assert g["plies"] == 10 and g["mate"] is True and g["term"] == 0
    assert g["has_clk"] and g["w_clk0"] == 180 and g["w_clk1"] == 150 and g["b_clk1"] == 165
    # White: 180 -> 150 over moves 2..5 with +2 increments: (180-150+2*4)/4 = 9.5 s per move
    assert abs(g["w_mt"] - 9.5) < 1e-6
    assert g["has_eval"] and g["w_nev"] == 5 and g["b_nev"] == 4
    assert g["w_blunders"] >= 1 and g["w_acc"] < g["b_acc"]
    assert g["final_wp"] < 5 and g["b_maxwp"] > 95
    # 23:01:03 on 2024-01-01 UTC
    assert g["ts"] == 1704150063


def test_berserk_bot_time_forfeit():
    rows, _ = _rows(BERSERK_GAME)
    g = rows[0]
    assert g["kind"] == 1 and g["speed"] == 1
    assert g["w_berserk"] is True and g["b_berserk"] is False
    assert g["wbot"] is True and g["btitled"] is True and g["term"] == 1
    # Berserk removes the increment; white used 30 -> 20 over 2 timed moves.
    assert abs(g["w_mt"] - 5.0) < 1e-6
    # Black flagged with 2 s left, so those 2 s count as used.
    assert abs(g["duration"] - ((30 - 20) + (60 - 2) + 2)) < 1e-6


def test_no_clock_game():
    rows, _ = _rows(NO_CLOCK_GAME)
    g = rows[0]
    assert g["plies"] == 9 and g["has_clk"] is False and g["w_mt"] is None
    assert g["result"] == 1 and g["speed"] == 4 and g["has_eval"] is False


def test_skips():
    rows, stats = _rows(SKIPPED_GAMES)
    assert rows == []
    assert stats["skip_correspondence"] == 1 and stats["skip_unfinished"] == 1
    assert stats["skip_no_rating"] == 1 and stats["games_read"] == 3
    rows, stats = _rows(SKIPPED_GAMES, keep_correspondence=True)
    assert len(rows) == 1 and rows[0]["speed"] == 5


def test_windows_line_endings_and_chunks():
    data = (LICHESS_EVAL_GAME + BERSERK_GAME + NO_CLOCK_GAME).replace(b"\n", b"\r\n")
    rows, stats = _rows(data)
    assert len(rows) == 3 and stats["games_read"] == 3


def test_eval_features_simple():
    # A clean game with no errors gives ~100% accuracy and 0 errors.
    f = eval_features([b"0.15", b"0.15", b"0.20", b"0.15"])
    assert f[0] == 2 and f[1] == 2 and f[2] > 99 and f[3] > 99 and f[6] == 0 and f[7] == 0


def test_zst_with_skippable_frame(tmp_path=None):
    """Lichess-style .zst files can start with a skippable frame; they must still be decoded."""
    import struct
    import tempfile
    from pathlib import Path

    import zstandard

    from chesstilt.extract import open_pgn
    data = (LICHESS_EVAL_GAME + BERSERK_GAME) * 50
    comp = zstandard.ZstdCompressor()
    blob = b""
    for i in range(0, len(data), 20000):
        f = comp.compress(data[i:i + 20000])
        blob += struct.pack("<III", 0x184D2A50, 4, len(f)) + f
    d = Path(tmp_path or tempfile.mkdtemp())
    p = d / "x.pgn.zst"
    p.write_bytes(blob)
    raw, stream = open_pgn(p)
    out = b""
    while True:
        chunk = stream.read(1 << 16)
        if not chunk:
            break
        out += chunk
    raw.close()
    assert out == data


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

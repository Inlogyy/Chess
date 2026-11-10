"""Stage 1: stream PGN files, parse them on all CPU cores, write Parquet.

Memory use stays flat no matter how big the input is: the file is read in
chunks, handed to worker processes, and each worker writes its own Parquet file.
"""
from __future__ import annotations

import bz2
import gzip
import json
import multiprocessing as mp
import os
import queue
import shutil
import time
import traceback
from pathlib import Path

from .pgnparse import COLUMNS, SPEED_NAMES, parse_chunk

FORMAT_VERSION = 1
FLUSH_ROWS = 50_000


def _pa():
    import pyarrow as pa
    import pyarrow.parquet as pq
    return pa, pq


def schema():
    pa, _ = _pa()
    types = {"str": pa.string(), "i64": pa.int64(), "i32": pa.int32(), "i16": pa.int16(),
             "i8": pa.int8(), "f32": pa.float32(), "bool": pa.bool_()}
    return pa.schema([(n, types[t]) for n, t, _ in COLUMNS])


def open_pgn(path: Path):
    """Return (raw_file, decompressed_stream) for .pgn, .pgn.zst, .pgn.bz2 or .pgn.gz."""
    raw = open(path, "rb")
    magic = raw.read(4)
    raw.seek(0)
    if magic == b"\x28\xb5\x2f\xfd":  # zstd
        try:
            import zstandard
        except ImportError:
            zstandard = None
        if zstandard is not None:
            dctx = zstandard.ZstdDecompressor(max_window_size=2 ** 31)
            return raw, dctx.stream_reader(raw, read_size=1 << 20, read_across_frames=True)
        try:
            from compression import zstd  # Python 3.14+
            return raw, zstd.ZstdFile(raw)
        except ImportError:
            raw.close()
            raise SystemExit("This file is zstd-compressed (.zst). Install support with:  pip install zstandard")
    if magic[:3] == b"BZh":
        return raw, bz2.BZ2File(raw)
    if magic[:2] == b"\x1f\x8b":
        return raw, gzip.GzipFile(fileobj=raw)
    return raw, raw


def stem_of(path: Path) -> str:
    name = path.name
    for ext in (".zst", ".bz2", ".gz", ".pgn"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    return name or "games"


# ------------------------------------------------------------------ workers
def _rows_to_table(rows, sch):
    pa, _ = _pa()
    arrays = []
    for (name, typ, _), col in zip(COLUMNS, zip(*rows)):
        if typ == "str":
            try:
                arr = pa.array(col, type=pa.string())
            except (pa.ArrowInvalid, UnicodeDecodeError):
                arr = pa.array([c.decode("utf-8", "replace") for c in col], type=pa.string())
        else:
            arr = pa.array(col, type=sch.field(name).type)
        arrays.append(arr)
    return pa.Table.from_arrays(arrays, schema=sch)


def _worker(wid, task_q, result_q, out_file, opts):
    try:
        _, pq = _pa()
        sch = schema()
        rows, stats, writer, kept = [], {}, None, 0
        while True:
            item = task_q.get()
            if item is None:
                break
            parse_chunk(item, rows, opts, stats)
            if len(rows) >= FLUSH_ROWS:
                if writer is None:
                    writer = pq.ParquetWriter(out_file, sch, compression="zstd")
                writer.write_table(_rows_to_table(rows, sch))
                kept += len(rows)
                rows = []
        if rows:
            if writer is None:
                writer = pq.ParquetWriter(out_file, sch, compression="zstd")
            writer.write_table(_rows_to_table(rows, sch))
            kept += len(rows)
        if writer is not None:
            writer.close()
        stats["games_kept"] = kept
        result_q.put(("ok", wid, stats))
    except KeyboardInterrupt:
        result_q.put(("interrupted", wid, None))
    except BaseException:
        result_q.put(("error", wid, traceback.format_exc()))


# ------------------------------------------------------------------ main side
def _put(q, item, procs, result_q):
    while True:
        try:
            q.put(item, timeout=1.0)
            return
        except queue.Full:
            if not all(p.is_alive() for p in procs):
                _raise_worker_error(result_q)


def _raise_worker_error(result_q):
    try:
        while True:
            r = result_q.get(timeout=2.0)
            if r[0] == "error":
                raise RuntimeError(f"A worker process failed:\n{r[2]}")
    except queue.Empty:
        pass
    raise RuntimeError("A worker process stopped unexpectedly (out of memory?). "
                       "Try fewer workers, e.g. --workers 2. (If you call chesstilt from your own "
                       "script, put the call under  if __name__ == '__main__':)")


def _trim(piece: bytes, keep: int) -> bytes:
    """Keep only the first `keep` games of a chunk."""
    pos = -1
    for _ in range(keep):
        pos = piece.find(b"\n\n[", pos + 1)
        if pos < 0:
            return piece
    return piece[: pos + 1]


def _fmt_dur(sec: float) -> str:
    sec = int(sec)
    if sec < 90:
        return f"{sec} s"
    if sec < 5400:
        return f"{sec // 60} min"
    return f"{sec // 3600} h {(sec % 3600) // 60:02d} min"


def _feed(path, task_q, result_q, procs, chunk_mb, limit, log):
    raw, stream = open_pgn(path)
    total = max(1, os.path.getsize(path))
    size = chunk_mb << 20
    pending = b""
    games = 0
    truncated = False
    t0 = last = time.time()
    try:
        while True:
            try:
                data = stream.read(size)
            except Exception as e:  # damaged or truncated download
                log(f"  WARNING: {path.name} ends early or is damaged ({type(e).__name__}: {e}).")
                log("           Keeping every complete game read up to that point.")
                truncated = True
                data = b""
            if not data:
                break
            buf = pending + data if pending else data
            cut = buf.rfind(b"\n\n[")
            if cut < 0:
                pending = buf
                continue
            piece, pending = buf[: cut + 1], buf[cut + 2:]
            n = piece.count(b'\n[Result ')
            if limit and games + n >= limit:
                piece = _trim(piece, limit - games)
                _put(task_q, piece, procs, result_q)
                games = limit
                pending = b""
                break
            _put(task_q, piece, procs, result_q)
            games += n
            now = time.time()
            if now - last >= 10:
                last = now
                done = raw.tell() / total
                rate = games / (now - t0)
                eta = (now - t0) * (1 - done) / done if done > 0.001 else 0
                log(f"  {done:6.1%} of file | {games:,} games | {rate:,.0f} games/s | about {_fmt_dur(eta)} left")
        if pending.strip():
            _put(task_q, pending, procs, result_q)
            games += pending.count(b"\n[Result ")
    finally:
        raw.close()
    return games, truncated, time.time() - t0


def extract_file(path: Path, games_dir: Path, workers: int, chunk_mb: int, limit: int,
                 opts: dict, log=print) -> dict:
    """Extract one PGN file into games_dir/<name>/part-NN.parquet. Skips finished files."""
    path = Path(path)
    st = path.stat()
    meta = {"input": str(path.resolve()), "size": st.st_size, "mtime": int(st.st_mtime),
            "limit": int(limit or 0), "format": FORMAT_VERSION,
            "keep_correspondence": bool(opts.get("keep_correspondence", False))}
    stem = stem_of(path)
    final = games_dir / stem
    tmp = games_dir / (stem + ".partial")
    done_file = final / "_SUCCESS.json"
    if done_file.exists():
        try:
            old = json.loads(done_file.read_text())
            if old.get("meta") == meta:
                log(f"{path.name}: already extracted, skipping (delete {final} to redo).")
                return old
        except (OSError, ValueError):
            pass
    for d in (tmp, final):
        if d.exists():
            shutil.rmtree(d)
    tmp.mkdir(parents=True)

    log(f"{path.name}: reading with {workers} worker process(es)" + (f", first {limit:,} games only" if limit else ""))
    ctx = mp.get_context("spawn")  # identical behaviour on Windows, macOS and Linux
    task_q = ctx.Queue(maxsize=workers * 2)
    result_q = ctx.Queue()
    procs = [ctx.Process(target=_worker, daemon=True,
                         args=(i, task_q, result_q, str(tmp / f"part-{i:02d}.parquet"), opts))
             for i in range(workers)]
    for p in procs:
        p.start()
    try:
        fed, truncated, elapsed = _feed(path, task_q, result_q, procs, chunk_mb, limit, log)
        for _ in procs:
            _put(task_q, None, procs, result_q)
        stats: dict = {}
        got = 0
        while got < workers:
            try:
                r = result_q.get(timeout=1.0)
            except queue.Empty:
                if not any(p.is_alive() for p in procs):
                    _raise_worker_error(result_q)
                continue
            if r[0] != "ok":
                raise RuntimeError(f"Worker {r[1]} failed:\n{r[2]}")
            for k, v in r[2].items():
                stats[k] = stats.get(k, 0) + v
            got += 1
        for p in procs:
            p.join(timeout=30)
    except BaseException:
        for p in procs:
            if p.is_alive():
                p.terminate()
        # Chunks still queued for dead workers would otherwise block Python from exiting.
        for q in (task_q, result_q):
            q.cancel_join_thread()
            q.close()
        raise
    elapsed = max(elapsed, 1e-9)
    stats["truncated_input"] = bool(truncated)
    info = {"meta": meta, "stats": stats, "seconds": round(elapsed, 1)}
    (tmp / "_SUCCESS.json").write_text(json.dumps(info, indent=2))
    tmp.rename(final)
    read = stats.get("games_read", 0)
    log(f"{path.name}: {read:,} games read, {stats.get('games_kept', 0):,} kept, "
        f"in {_fmt_dur(elapsed)} ({read / elapsed:,.0f} games/s)")
    return info


def extract(inputs, out_dir: Path, workers: int = 0, chunk_mb: int = 8, limit: int = 0,
            opts: dict | None = None, log=print) -> dict:
    games_dir = Path(out_dir) / "games"
    games_dir.mkdir(parents=True, exist_ok=True)
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    opts = opts or {}
    infos = [extract_file(Path(p), games_dir, workers, chunk_mb, limit, opts, log) for p in inputs]
    total: dict = {}
    for info in infos:
        for k, v in info["stats"].items():
            if isinstance(v, bool):
                total[k] = total.get(k, False) or v
            else:
                total[k] = total.get(k, 0) + v
    summary = {"files": [i["meta"]["input"] for i in infos], "stats": total}
    (Path(out_dir) / "extract_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def inspect_file(path: Path, n_games: int = 3000, log=print) -> None:
    """Parse the first games of a file and print what the program sees."""
    from collections import Counter

    from .pgnparse import COLUMN_NAMES, KIND_NAMES, TERM_NAMES
    raw, stream = open_pgn(Path(path))
    buf = b""
    while buf.count(b"\n[Result ") < n_games:
        data = stream.read(4 << 20)
        if not data:
            break
        buf += data
    raw.close()
    cut = buf.rfind(b"\n\n[")
    if cut > 0:
        buf = buf[: cut + 1]
    rows, stats = [], {}
    parse_chunk(buf, rows, {"keep_correspondence": True}, stats)
    log(f"Parsed the first {stats.get('games_read', 0):,} games of {Path(path).name}; {len(rows):,} usable.")
    skipped = {k: v for k, v in stats.items() if k.startswith("skip")}
    if skipped:
        log(f"Skipped: {skipped}")
    if not rows:
        log("No usable games found. Is this a Lichess PGN file?")
        return
    col = {n: i for i, n in enumerate(COLUMN_NAMES)}
    n = len(rows)

    def share(name):
        return sum(1 for r in rows if r[col[name]]) / n

    log(f"  with clock times : {share('has_clk'):.0%}")
    log(f"  with engine evals: {share('has_eval'):.0%}")
    log(f"  speeds   : {dict(Counter(SPEED_NAMES[r[col['speed']]] for r in rows))}")
    log(f"  events   : {dict(Counter(KIND_NAMES[r[col['kind']]] for r in rows))}")
    log(f"  endings  : {dict(Counter(TERM_NAMES[r[col['term']]] for r in rows))}")
    log(f"  bot games: {sum(1 for r in rows if r[col['wbot']] or r[col['bbot']]):,}")
    from datetime import datetime, timezone

    from .pgnparse import RESULT_NAMES
    first = dict(zip(COLUMN_NAMES, rows[0]))
    names = {"speed": SPEED_NAMES, "kind": KIND_NAMES, "term": TERM_NAMES}
    log("First game as the program reads it:")
    for k, v in first.items():
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        if isinstance(v, float):
            v = round(v, 2)
        if k in names and v is not None:
            v = f"{v} ({names[k][v]})"
        elif k == "result":
            v = f"{v} ({RESULT_NAMES.get(v, '?')})"
        elif k == "ts":
            v = f"{v} ({datetime.fromtimestamp(v, tz=timezone.utc):%Y-%m-%d %H:%M:%S} UTC)"
        log(f"  {k:12s} {v}")

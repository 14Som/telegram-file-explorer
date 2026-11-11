"""Disk cache for media streams.

Telegram has no public file URL, so every player seek goes back through the
local server. Caching fixed-size blocks on disk makes replays, scrubbing and
seeking instant after the first read, and keeps Telegram requests down.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Tuple

from . import appconfig

logger = logging.getLogger("tfe.streamcache")

BLOCK = 2 * 1024 * 1024
# Frame grabs read a 512 KiB window at a time. Eighteen grabs of one file all
# read the same header and the same index at the tail, so those windows are
# cached at the size they are asked for: filled once, served from disk after.
SLICE = 512 * 1024
# Streams, PDFs and book pages all land here; a large disk budget means a
# second open of the same file comes from the phone instead of Telegram.
MAX_BYTES = 24 * 1024 ** 3

# A player only drains its socket while it has picture to show, so the serve
# loop spends most of a slow link idle. The builder keeps pulling blocks past
# the current window on its own task, which is what gives playback its runway.
PREFETCH_AHEAD = 768 * 1024 * 1024
PREFETCH_RUN = 32  # blocks per pass, so seeks are picked up within ~64 MiB
PRUNE_INTERVAL = 120.0

DIR = appconfig.HOME / "cache" / "stream"

_locks: Dict[str, asyncio.Lock] = {}
_slice_locks: Dict[Tuple[str, int], asyncio.Lock] = {}
# A frame grab walks a whole GOP, so several preview windows are fetched at
# once; the cap keeps a picker's burst inside what Telegram serves without
# backing off.
_fetch_sem = asyncio.Semaphore(6)
_builders: Dict[str, Dict[str, Any]] = {}
_active_key: "str | None" = None
_fills = 0
_prune_task: "asyncio.Task[None] | None" = None
_last_prune = 0.0


def _dir_for(key: str) -> Path:
    safe = "".join(ch for ch in key if ch.isalnum() or ch in "-_")
    return DIR / safe


def _path(key: str, index: int) -> Path:
    return _dir_for(key) / f"{index:06d}.blk"


def _slice_path(key: str, index: int) -> Path:
    return _dir_for(key) / f"s{index:05d}.slc"


def _block_len(index: int, size: int | None) -> int:
    """Bytes a block holds; the final block of a file can be short."""
    if size is None:
        return BLOCK
    return max(0, min(BLOCK, size - index * BLOCK))


async def _stream_run(
    service: Any,
    message: Any,
    key: str,
    first: int,
    last: int,
    size: int | None,
    start: int,
    end: int,
) -> AsyncIterator[bytes]:
    """Stream blocks first..last (inclusive), handing bytes on as they arrive.

    Players starve when a read only ends after a whole 2 MiB block has been
    downloaded: on a slow phone link that is several seconds of silence between
    bursts. Every chunk is passed through immediately instead, while complete
    blocks are still written to disk for later seeks and replays.
    """
    base = first * BLOCK
    directory = _dir_for(key)
    directory.mkdir(parents=True, exist_ok=True)
    if size is None:
        run_end = (last + 1) * BLOCK
    else:
        run_end = min((last + 1) * BLOCK, size)
    # Pull from the requested byte, not the block boundary. Waiting for a whole
    # block's worth of lead-in is what starved players on slow links.
    initial = max(start, base)
    length = run_end - initial
    if length <= 0:
        return
    buffer = bytearray()
    buffer_start = initial
    index = first + 1 if initial > base else first
    position = initial
    try:
        async for chunk in service.stream_chunks(message, initial, length):
            chunk_start = position
            position += len(chunk)
            buffer.extend(chunk)
            while index <= last:
                need = _block_len(index, size)
                offset = index * BLOCK - buffer_start
                if offset + need > len(buffer):
                    break
                _write(directory, index, bytes(buffer[offset : offset + need]))
                index += 1
            trim = min(index * BLOCK - buffer_start, len(buffer)) if index <= last else len(buffer)
            if trim > 0:
                del buffer[:trim]
                buffer_start += trim
            low = max(start, chunk_start)
            high = min(end, position)
            if low < high:
                yield chunk[low - chunk_start : high - chunk_start]
            if position >= end:
                return
        # Only a complete block (or the exact tail of the file) may be cached,
        # otherwise a short read would poison every later request for this block.
        if buffer and index <= last and len(buffer) == _block_len(index, size):
            _write(directory, index, bytes(buffer))
    except asyncio.CancelledError:
        raise
    except OSError:
        raise
    except Exception as exc:
        raise OSError("stream cache could not fetch blocks") from exc


def _write(directory: Path, index: int, data: bytes) -> None:
    _write_path(directory / f"{index:06d}.blk", data)


def _write_path(target: Path, data: bytes) -> None:
    temp = target.with_suffix(".part")
    try:
        temp.write_bytes(data)
        os.replace(temp, target)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass


def _touch(path: Path) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass


def _prune_sync() -> None:
    if not DIR.exists():
        return
    total = 0
    entries = []
    for pattern in ("*.blk", "*.slc"):
        for file in DIR.rglob(pattern):
            try:
                stat = file.stat()
            except OSError:
                continue
            total += stat.st_size
            entries.append((stat.st_mtime, stat.st_size, file))
    if total <= MAX_BYTES:
        return
    entries.sort()
    for _, size, file in entries:
        if total <= MAX_BYTES:
            break
        try:
            file.unlink()
            total -= size
        except OSError:
            continue
    for folder in DIR.iterdir():
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass


async def _maybe_prune() -> None:
    global _prune_task, _last_prune
    if _prune_task is not None and not _prune_task.done():
        return
    if not DIR.exists():
        return
    now = time.time()
    if now - _last_prune < PRUNE_INTERVAL:
        return
    _last_prune = now
    _prune_task = asyncio.create_task(asyncio.to_thread(_prune_sync))


def warm(
    service: Any,
    message: Any,
    key: str,
    start: int,
    length: int,
    size: int | None = None,
) -> None:
    """Keep filling blocks onto disk past the window just served.

    Called for every real playback range request: the block builder runs on
    its own task, so the download continues while the player is still chewing
    through the bytes it has. A seek moves the anchor and the builder
    re-targets. Only one item may build at a time; a new playback target stops
    the previous builder so browsing cannot fan out into a Telegram flood.
    """
    global _active_key
    if not size or size <= 0:
        return
    end = min(start + length, size)
    state = _builders.get(key)
    if state is None:
        state = {"end": end, "size": size, "task": None}
        _builders[key] = state
    else:
        state["end"] = end
        state["size"] = size
    if _active_key != key:
        previous = _builders.get(_active_key or "")
        task = previous.get("task") if previous else None
        if task is not None and not task.done():
            task.cancel()
        _active_key = key
    task = state.get("task")
    if task is None or task.done():
        state["task"] = asyncio.create_task(_build(service, message, key))


async def _build(service: Any, message: Any, key: str) -> None:
    state = _builders.get(key)
    try:
        while state is not None and _builders.get(key) is state:
            anchor = state["end"]
            size = state["size"]
            if anchor >= size:
                return
            index = anchor // BLOCK
            last = (min(size, anchor + PREFETCH_AHEAD) - 1) // BLOCK
            while index <= last and _path(key, index).exists():
                index += 1
            if index > last:
                return
            pass_last = min(last, index + PREFETCH_RUN - 1)
            run_end = min(size, (pass_last + 1) * BLOCK)
            async for _chunk in _stream_run(
                service, message, key, index, pass_last, size,
                index * BLOCK, run_end,
            ):
                if _builders.get(key) is not state:
                    return
                # The player jumped somewhere else; drop this pass quickly.
                if abs(state["end"] - anchor) > 2 * BLOCK:
                    break
            await _maybe_prune()
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        raise
    except Exception:
        # The next range request starts a fresh builder.
        return
    finally:
        if state is not None and state.get("task") is asyncio.current_task():
            state["task"] = None


async def shutdown() -> None:
    """Stop every builder and keep the complete blocks on disk.

    Blocks are written atomically, so whatever was finished can serve the next
    launch; only partial files are dropped. The size cap prunes the oldest
    blocks on its own.
    """
    global _active_key
    _active_key = None
    tasks = []
    for state in _builders.values():
        task = state.get("task")
        if task is not None and not task.done():
            task.cancel()
            tasks.append(task)
    _builders.clear()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.to_thread(_drop_partials)


def _drop_partials() -> None:
    if not DIR.exists():
        return
    for path in DIR.rglob("*.part"):
        try:
            path.unlink()
        except OSError:
            pass


async def purge() -> None:
    """Drop half-written blocks left behind by a previous run.

    Android can kill the app from the recents screen before the lifespan
    shutdown runs, which can leave *.part files behind. Complete blocks are
    kept so the next launch starts with a warm cache.
    """
    global _active_key
    _active_key = None
    _builders.clear()
    await asyncio.to_thread(_drop_partials)


async def _slice(
    service: Any, message: Any, key: str, index: int, want: int
) -> bytes:
    """One 512 KiB preview window, fetched once and kept on disk.

    A picker starts its grabs together and every one of them reads the header
    and the index at the tail, window by window. Without sharing those windows
    each grab pulled the same megabytes from Telegram again, which is what made
    a build take a minute. A slice is only cached when it arrived complete.
    """
    path = _slice_path(key, index)
    if want <= 0:
        return b""
    if path.is_file():
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        if len(data) == want:
            return data
    if len(_slice_locks) > 4096:
        for slot in [k for k, held in _slice_locks.items() if not held.locked()]:
            _slice_locks.pop(slot, None)
    lock = _slice_locks.setdefault((key, index), asyncio.Lock())
    async with lock:
        if path.is_file():
            try:
                data = path.read_bytes()
            except OSError:
                data = b""
            if len(data) == want:
                return data
        data = bytearray()
        async with _fetch_sem:
            async for chunk in service.stream_chunks(message, index * SLICE, want):
                data.extend(chunk)
                if len(data) >= want:
                    break
        if len(data) == want:
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_path(path, bytes(data))
        return bytes(data)


async def _slice_read(
    service: Any, message: Any, key: str, start: int, end: int, size: "int | None"
) -> AsyncIterator[bytes]:
    spans = []
    pos = start
    while pos < end:
        index = pos // SLICE
        base = index * SLICE
        limit = base + SLICE if size is None else min(base + SLICE, size)
        stop = min(end, limit)
        if stop <= pos:
            break
        spans.append((index, base, limit, pos, stop))
        pos = stop
    if not spans:
        return

    async def load(index: int, want: int) -> bytes:
        try:
            return await _slice(service, message, key, index, want)
        except asyncio.CancelledError:
            raise
        except Exception:
            return b""

    tasks = {
        index: asyncio.create_task(load(index, limit - base))
        for index, base, limit, _from, _stop in spans
    }
    try:
        for index, base, limit, from_off, to_off in spans:
            data = await tasks[index]
            if len(data) < limit - base:
                # The window did not arrive whole; finish from the wire
                # rather than retrying the same slices.
                async for chunk in service.stream_chunks(
                    message, from_off, end - from_off
                ):
                    yield chunk
                return
            yield data[from_off - base : to_off - base]
    finally:
        for task in tasks.values():
            task.cancel()


async def read(
    service: Any,
    message: Any,
    key: str,
    start: int,
    length: int,
    size: int | None = None,
    direct: bool = False,
) -> AsyncIterator[bytes]:
    """Yield length bytes from start, caching blocks as the data streams past.

    The per-file lock is only taken when it is free. A player that walked away
    from a range read can sit on its connection for minutes, and waiting on its
    lock would freeze every later seek; contended reads stream straight through
    without caching instead.

    ``direct`` never fills 2 MiB blocks: a frame grab stops after one keyframe,
    and the windows it does read are shared with the other grabs of the same
    file, so they are cached as 512 KiB slices instead.
    """
    global _fills
    if length <= 0:
        return
    end = start + length
    pos = start
    sent = 0
    while pos < end:
        index = pos // BLOCK
        path = _path(key, index)
        if path.exists():
            _touch(path)
            offset = pos - index * BLOCK
            remaining = min(BLOCK - offset, end - pos)
            moved = 0
            try:
                with open(path, "rb") as handle:
                    handle.seek(offset)
                    while remaining > 0:
                        data = handle.read(min(262144, remaining))
                        if not data:
                            break
                        remaining -= len(data)
                        moved += len(data)
                        sent += len(data)
                        pos += len(data)
                        yield data
            except OSError:
                raise OSError("stream cache block unreadable")
            if moved == 0:
                raise OSError("stream cache block unreadable")
            continue

        if direct:
            got = 0
            async for chunk in _slice_read(service, message, key, pos, end, size):
                sent += len(chunk)
                got += len(chunk)
                pos += len(chunk)
                yield chunk
            if got == 0:
                raise OSError("stream cache could not fetch blocks")
            continue

        lock = _locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            remaining = end - pos
            got = 0
            async for chunk in service.stream_chunks(message, pos, remaining):
                sent += len(chunk)
                got += len(chunk)
                pos += len(chunk)
                yield chunk
            if got == 0:
                raise OSError("stream cache could not fetch blocks")
            # A stalled fetch ends the iterator early; resume from where it
            # stopped instead of ending the response short and truncating.
            continue

        await lock.acquire()
        try:
            # A concurrent request may have filled the leading blocks meanwhile.
            if _path(key, index).exists():
                continue
            last = (end - 1) // BLOCK
            run_last = index
            while run_last < last and not _path(key, run_last + 1).exists():
                run_last += 1
            before = pos
            async for chunk in _stream_run(
                service, message, key, index, run_last, size, pos, end
            ):
                sent += len(chunk)
                pos += len(chunk)
                yield chunk
            if pos == before:
                raise OSError("stream cache could not fetch blocks")
        finally:
            lock.release()
    if sent < length:
        raise OSError("stream cache ended early")
    _fills += 1
    if _fills % 256 == 0:
        await _maybe_prune()


def stats() -> Dict[str, Any]:
    total = 0
    count = 0
    if DIR.exists():
        for pattern in ("*.blk", "*.slc"):
            for file in DIR.rglob(pattern):
                try:
                    total += file.stat().st_size
                    count += 1
                except OSError:
                    continue
    return {"blocks": count, "bytes": total, "limit": MAX_BYTES}

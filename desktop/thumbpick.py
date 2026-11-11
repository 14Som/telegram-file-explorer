"""Thumbnail picker: grab a few frames with ffmpeg and keep the chosen one.

Frame placement follows thumbup.py (MIT, bl4ck5un/thumbup.py): a fixed number
of snapshots spaced evenly across the runtime, taken in parallel and kept as
separate images. The picker UI (Thumby-style: show frames, tap one) sends the
winning index back here; that frame becomes the item's thumbnail.

Candidates live in the cache folder so vault backups only carry the chosen
frame, which is stored next to the regular thumbnails as ``<id>.custom.jpg``.
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import appconfig, mediatools, storyboard, thumbcrypt

log = logging.getLogger("tfe.thumbpick")

COUNT = 18
REFINE_COUNT = 9
WORKERS = 4
SIZE = 640
AUTO_SIZE = 640
MAX_BUILD_SECONDS = 10 * 60
RETRY_AFTER = 10 * 60
CAND_DIR = appconfig.CACHE_DIR / "thumbpicks"

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_lock = threading.Lock()
# A picker build is the one thing the user is waiting on. Background artwork
# (tile thumbnails, the warm pass) checks this and steps aside so its GetFile
# requests do not sit in front of the frames being shown.
_activity = 0


def busy() -> bool:
    return _activity > 0

_jobs: Dict[str, threading.Thread] = {}
# A refine starts by clearing its frame folder. Two taps in a row used to run
# back to back, and the second wiped the frames the first was still copying.
_refine_locks: Dict[str, threading.Lock] = {}
_errors: Dict[str, Tuple[str, float]] = {}
_progress: Dict[str, Dict[str, Any]] = {}
# Lengths learned from a probe or the storyboard sheet. Probing costs a slow
# first read, and without this every shuffle and refine repeated it.
_durations: Dict[str, float] = {}
# Used only when no length can be read at all, so the picker still shows
# frames instead of an error; frames past the end fall back to a neighbour.
FALLBACK_SPAN = 90.0


def _key(item: Dict[str, Any]) -> str:
    safe = f"{item.get('chat_id')}-{item['message_id']}-{item.get('size') or 0}"
    return "".join(ch for ch in safe if ch.isalnum() or ch in "-_")


def candidate_dir(item: Dict[str, Any]) -> Path:
    return CAND_DIR / _key(item)


def custom_path(item_id: int) -> Path:
    return appconfig.THUMB_DIR / f"{item_id}.custom.jpg"


def frame_path(item: Dict[str, Any], index: int) -> Path:
    return candidate_dir(item) / f"frame-{index}.jpg"


def frame_source(item: Dict[str, Any], index: int) -> Optional[Path]:
    """Ready frame path, including ones a running build has already finished."""
    path = frame_path(item, index)
    if path.is_file():
        return path
    if 1 <= index <= COUNT:
        partial = CAND_DIR / f"{_key(item)}.work" / f"frame-{index}.jpg"
        if partial.is_file():
            return partial
    return None


def refine_path(item: Dict[str, Any], index: int) -> Path:
    return CAND_DIR / f"{_key(item)}.refine" / f"frame-{index}.jpg"


def _headers(cookie: str) -> str:
    return f"Cookie: tfe_session={cookie}\r\n"


def _remember(key: str, duration: float) -> None:
    with _lock:
        _durations[key] = duration


def _meta_duration(path: Path) -> float:
    if not path.is_file():
        return 0.0
    try:
        return float(json.loads(path.read_text(encoding="utf-8")).get("duration") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def _duration_for(
    item: Dict[str, Any], key: str, url: str, cookie: str, ffmpeg: Path
) -> float:
    """Video length from the catalogue, the storyboard, or ffmpeg.

    Many videos arrive without a duration attribute, so the length has to be
    probed. A probe is one slow read and it used to fail while frame grabs
    filled the link; anything learned once is reused for the session.
    """
    duration = float(item.get("duration") or 0)
    if duration > 0:
        _remember(key, duration)
        return duration
    with _lock:
        known = _durations.get(key, 0.0)
    if known > 0:
        return known
    # A previous picker build and the seek-preview sheet both record it.
    duration = _meta_duration(CAND_DIR / key / "meta.json")
    if duration <= 0:
        duration = _meta_duration(storyboard.SPRITE_DIR / f"{key}.json")
    if duration <= 0:
        duration = storyboard.probe_duration(ffmpeg, url, cookie)
    if duration <= 0:
        # A flood wait from the frame grabs may just have expired; one retry
        # on a longer leash usually wins.
        time.sleep(1.0)
        duration = storyboard.probe_duration(ffmpeg, url, cookie, timeout=90.0)
    if duration > 0:
        _remember(key, duration)
    return duration


def _times(duration: float) -> List[float]:
    return [min(duration - 0.3, duration * (index + 0.5) / COUNT) for index in range(COUNT)]


def _random_times(duration: float, previous: Optional[List[float]] = None) -> List[float]:
    """Shuffle: one random point inside each slice of the runtime.

    A random point per slice keeps the new set spread across the video instead
    of clustering, and each point is re-rolled a few times to stay clear of the
    frames the user just rejected.
    """
    margin = min(0.5, max(0.1, duration * 0.005))
    low, high = margin, max(margin, duration - margin)
    span = max(0.0, high - low)
    old = previous or []
    gap = min(1.0, max(0.3, duration * 0.05))
    times: List[float] = []
    for index in range(COUNT):
        start = low + span * index / COUNT
        end = low + span * (index + 1) / COUNT
        pick = (start + end) / 2
        for _ in range(20):
            pick = random.uniform(start, end)
            if not old or min(abs(pick - value) for value in old) >= gap:
                break
        times.append(pick)
    return times


def _grab(
    ffmpeg: Path, url: str, cookie: str, index: int, second: float, work: Path
) -> Optional[Path]:
    target = work / f"frame-{index + 1}.jpg"
    try:
        result = subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner", "-loglevel", "error",
                "-headers", _headers(cookie),
                # Decoding from the previous keyframe means reading its whole
                # GOP from Telegram (minutes per frame on a long film). The
                # nearest keyframe is at most one GOP away, which is invisible
                # in a grid spread over the whole runtime, and reads one packet.
                "-noaccurate_seek",
                "-ss", f"{second:.3f}",
                "-i", url,
                "-frames:v", "1",
                "-vf", f"scale={SIZE}:{SIZE}:force_original_aspect_ratio=decrease",
                "-q:v", "2",
                "-y", str(target),
            ],
            capture_output=True,
            timeout=120,
            creationflags=_NO_WINDOW,
        )
        if result.returncode == 0 and target.is_file() and target.stat().st_size > 0:
            return target
        target.unlink(missing_ok=True)
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def _grab_strip(
    ffmpeg: Path, url: str, cookie: str, start: float, width: float, folder: Path
) -> List[Path]:
    """Nine frames from one pass over the window; reads the range once."""
    try:
        result = subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner", "-loglevel", "error",
                "-headers", _headers(cookie),
                "-ss", f"{start:.3f}",
                "-i", url,
                "-t", f"{width + 0.05:.3f}",
                "-vf",
                f"fps={REFINE_COUNT}/{width:.4f},"
                f"scale={SIZE}:{SIZE}:force_original_aspect_ratio=decrease",
                "-frames:v", str(REFINE_COUNT),
                "-q:v", "2",
                "-y", str(folder / "frame-%d.jpg"),
            ],
            capture_output=True,
            timeout=180,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    found = [
        folder / f"frame-{index}.jpg"
        for index in range(1, REFINE_COUNT + 1)
        if (folder / f"frame-{index}.jpg").is_file()
        and (folder / f"frame-{index}.jpg").stat().st_size > 0
    ]
    return found


def _build(
    item: Dict[str, Any],
    key: str,
    url: str,
    cookie: str,
    randomize: bool = False,
    previous: Optional[List[float]] = None,
) -> None:
    global _activity
    with _lock:
        _activity += 1
    work = CAND_DIR / f"{key}.work"
    target_dir = CAND_DIR / key
    ffmpeg = mediatools.tool_path("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is not installed.")
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(CAND_DIR / f"{key}.refine", ignore_errors=True)
    version = int(time.time() * 1000)
    try:
        duration = _duration_for(item, key, url, cookie, ffmpeg)
        # With no length anywhere, spread the frames over a short opening
        # stretch. Seeks past the end fall back to a neighbouring frame, which
        # is a usable strip instead of an error dialog.
        span = duration if duration > 0 else FALLBACK_SPAN
        times = _random_times(span, previous) if randomize else _times(span)
        plan = {
            "state": "building",
            "item_id": item.get("id"),
            "count": COUNT,
            "times": [round(value, 2) for value in times],
            "duration": round(duration, 2),
            "version": version,
            "ready": [],
        }
        # The UI draws all tiles at once and fills each one the moment its
        # frame lands, so a slow stream still shows progress within seconds.
        with _lock:
            _progress[key] = plan
        frames: List[Optional[Path]] = [None] * COUNT
        deadline = time.time() + MAX_BUILD_SECONDS
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {
                pool.submit(_grab, ffmpeg, url, cookie, index, second, work): index
                for index, second in enumerate(times)
            }
            for future in as_completed(futures):
                index = futures[future]
                if time.time() > deadline:
                    raise RuntimeError("Building thumbnails took too long.")
                try:
                    path = future.result()
                except Exception:
                    path = None
                if path is not None:
                    thumbcrypt.seal_file(path)
                frames[index] = path
                if path is not None:
                    with _lock:
                        _progress.get(key, {}).get("ready", []).append(index + 1)
        if all(path is None for path in frames):
            raise RuntimeError("Telegram did not return any frames.")
        for index, path in enumerate(frames):
            if path is not None:
                continue
            fallback = next(
                (frames[j] for j in range(index - 1, -1, -1) if frames[j]),
                next((frames[j] for j in range(index + 1, COUNT) if frames[j]), None),
            )
            target = work / f"frame-{index + 1}.jpg"
            shutil.copyfile(fallback, target)
            frames[index] = target
        payload = {
            "state": "ready",
            "item_id": item.get("id"),
            "count": COUNT,
            "times": [round(value, 2) for value in times],
            "duration": round(duration, 2),
            "version": version,
        }
        (work / "meta.json").write_text(json.dumps(payload), encoding="utf-8")
        CAND_DIR.mkdir(parents=True, exist_ok=True)
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        work.rename(target_dir)
        with _lock:
            _errors.pop(key, None)
        log.info("Thumbnail candidates ready for item %s", item.get("id"))
    finally:
        with _lock:
            _activity -= 1
            _progress.pop(key, None)
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)


def _read_meta(key: str) -> Optional[Dict[str, Any]]:
    folder = CAND_DIR / key
    meta = folder / "meta.json"
    if not meta.is_file():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("state") != "ready":
        return None
    if any(not (folder / f"frame-{index + 1}.jpg").is_file() for index in range(COUNT)):
        return None
    return data


def describe(
    item: Dict[str, Any], url: str, cookie: str, shuffle: bool = False
) -> Dict[str, Any]:
    """Report or start candidate generation for one item; never blocks for long.

    ``shuffle`` throws the current set away and rebuilds it from random points,
    keeping the frame times it replaces so the new set does not repeat them.
    """
    kind = (item.get("kind") or "").lower()
    if kind != "video":
        return {"state": "unavailable", "reason": "Thumbnails can only be picked for videos."}
    key = _key(item)
    previous: List[float] = []
    if shuffle:
        previous = list((_read_meta(key) or {}).get("times") or [])
        reset(item)
        meta = None
    else:
        meta = _read_meta(key)
    if meta is not None:
        meta["custom"] = custom_path(int(item["id"])).is_file()
        return meta
    if mediatools.tool_path("ffmpeg") is None:
        return {
            "state": "missing-tool",
            "tool": "ffmpeg",
            "reason": "Choosing a thumbnail needs ffmpeg, which is not installed yet.",
        }
    with _lock:
        error, stamp = _errors.get(key, (0.0, ""))
        job = _jobs.get(key)
        running = job is not None and job.is_alive()
        plan = _progress.get(key) if running else None
        snapshot = None
        if plan is not None:
            snapshot = dict(plan)
            snapshot["ready"] = sorted(set(plan.get("ready") or []))
        if not running and not (error and time.time() - stamp < RETRY_AFTER):
            error = ""
            job = threading.Thread(
                target=_run,
                args=(item, key, url, cookie, shuffle, previous),
                name="tfe-thumbpick",
                daemon=True,
            )
            _jobs[key] = job
            job.start()
    if error:
        return {"state": "error", "reason": error}
    if snapshot is not None:
        return snapshot
    return {"state": "pending"}


def _run(
    item: Dict[str, Any],
    key: str,
    url: str,
    cookie: str,
    randomize: bool = False,
    previous: Optional[List[float]] = None,
) -> None:
    try:
        _build(item, key, url, cookie, randomize, previous)
    except Exception as exc:
        log.warning("Thumbnail candidates for item %s failed: %s", item.get("id"), exc)
        with _lock:
            _errors[key] = (str(exc) or "Could not build thumbnails.", time.time())


def _grab_auto(
    ffmpeg: Path, url: str, cookie: str, second: float, target: Path
) -> Optional[Path]:
    try:
        result = subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner", "-loglevel", "error",
                "-headers", _headers(cookie),
                "-noaccurate_seek",
                "-ss", f"{second:.3f}",
                "-i", url,
                "-frames:v", "1",
                "-vf", f"scale={AUTO_SIZE}:-2",
                "-q:v", "3",
                "-y", str(target),
            ],
            capture_output=True,
            timeout=120,
            creationflags=_NO_WINDOW,
        )
        if result.returncode == 0 and target.is_file() and target.stat().st_size > 0:
            return target
    except (OSError, subprocess.SubprocessError):
        pass
    target.unlink(missing_ok=True)
    return None


def auto_thumb(item: Dict[str, Any], url: str, cookie: str) -> bool:
    """Keep one frame as the thumbnail for a video without a Telegram preview.

    A keyframe seek reads only the start of the stream, so this is cheap even
    for multi-gigabyte files. The frame is stored as the picked cover, which
    vault snapshots already carry to other PCs.
    """
    target = custom_path(int(item["id"]))
    if target.is_file():
        return True
    ffmpeg = mediatools.tool_path("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is not installed yet.")
    duration = _duration_for(item, _key(item), url, cookie, ffmpeg)
    second = 5.0
    if duration > 0:
        second = min(max(2.0, duration * 0.12), max(0.2, duration - 0.3))
    work = CAND_DIR / f"auto-{int(item['id'])}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        frame = _grab_auto(ffmpeg, url, cookie, second, work / "frame.jpg")
        if frame is None and second > 0.2:
            frame = _grab_auto(ffmpeg, url, cookie, 0.0, work / "frame.jpg")
        if frame is None:
            return False
        thumbcrypt.write_bytes(target, frame.read_bytes())
        return True
    finally:
        shutil.rmtree(work, ignore_errors=True)


def reset(item: Dict[str, Any]) -> None:
    """Drop generated candidates and past errors so the picker can try again."""
    key = _key(item)
    with _lock:
        _errors.pop(key, None)
        _jobs.pop(key, None)
        _progress.pop(key, None)
    shutil.rmtree(CAND_DIR / key, ignore_errors=True)
    shutil.rmtree(CAND_DIR / f"{key}.refine", ignore_errors=True)


def _refine_window(center: float, duration: float) -> Tuple[float, float]:
    if duration <= 0:
        # Length unknown: nine frames across six seconds around the chosen
        # moment still work, and the strip never blocks on the duration.
        return max(0.0, center - 3.0), 6.0
    gap = duration / COUNT
    width = max(0.4, min(gap, 12.0, max(0.2, duration - 0.2)))
    start = min(max(0.0, center - width / 2), max(0.0, duration - 0.2 - width))
    return max(0.0, start), width


def refine_times(center: float, duration: float) -> List[float]:
    """Nine moments inside twelve seconds around ``center``.

    The picker spreads ``COUNT`` frames across the whole runtime, so two
    neighbours can be minutes apart; this zooms into a short window around the
    chosen one, where nine frames land about a second apart.
    """
    start, width = _refine_window(center, duration)
    step = width / (REFINE_COUNT - 1)
    return [start + step * index for index in range(REFINE_COUNT)]


def refine(
    item: Dict[str, Any], url: str, cookie: str, center: float
) -> Dict[str, Any]:
    global _activity
    key = _key(item)
    with _lock:
        slot = _refine_locks.setdefault(key, threading.Lock())
    with slot:
        with _lock:
            _activity += 1
        try:
            return _refine(item, url, cookie, center)
        finally:
            with _lock:
                _activity -= 1


def _refine(
    item: Dict[str, Any], url: str, cookie: str, center: float
) -> Dict[str, Any]:
    """Grab nine frames around ``center`` in one batch for fine tuning."""
    ffmpeg = mediatools.tool_path("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is not installed.")
    key = _key(item)
    folder = CAND_DIR / f"{key}.refine"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    duration = _duration_for(item, key, url, cookie, ffmpeg)
    version = int(time.time() * 1000)
    center = max(0.0, float(center))
    start, width = _refine_window(center, duration)
    times = refine_times(center, duration)
    frames: List[Optional[Path]] = [None] * REFINE_COUNT
    for path in _grab_strip(ffmpeg, url, cookie, start, width, folder):
        index = int(path.stem.split("-")[1]) - 1
        if 0 <= index < REFINE_COUNT:
            thumbcrypt.seal_file(path)
            frames[index] = path
    missing = [index for index, path in enumerate(frames) if path is None]
    if missing:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {
                pool.submit(_grab, ffmpeg, url, cookie, index, times[index], folder): index
                for index in missing
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    path = future.result()
                except Exception:
                    path = None
                if path is not None:
                    thumbcrypt.seal_file(path)
                frames[index] = path
    if all(path is None for path in frames):
        raise RuntimeError("Telegram did not return any frames.")
    for index, path in enumerate(frames):
        if path is not None:
            continue
        fallback = next(
            (frames[j] for j in range(index - 1, -1, -1) if frames[j]),
            next((frames[j] for j in range(index + 1, REFINE_COUNT) if frames[j]), None),
        )
        if fallback is not None and fallback.is_file():
            shutil.copyfile(fallback, folder / f"frame-{index + 1}.jpg")
    payload = {
        "count": REFINE_COUNT,
        "times": [round(value, 2) for value in times],
        "version": version,
    }
    (folder / "meta.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


def select_refine(item: Dict[str, Any], index: int) -> Path:
    """Keep one of the fine-tuning frames as the item's thumbnail."""
    if index < 1 or index > REFINE_COUNT:
        raise ValueError("Choose one of the fine frames.")
    source = refine_path(item, index)
    if not source.is_file():
        raise FileNotFoundError("Generate the fine frames first.")
    data = thumbcrypt.read_bytes(source)
    if data is None:
        raise FileNotFoundError("Generate the fine frames first.")
    target = custom_path(int(item["id"]))
    thumbcrypt.write_bytes(target, data)
    return target


def select(item: Dict[str, Any], index: int) -> Path:
    """Copy a generated frame into place as the item's thumbnail."""
    if index < 1 or index > COUNT:
        raise ValueError("Choose one of the generated frames.")
    # Frames are published one at a time while the build runs, so read from the
    # working folder too; it is renamed when the build ends, hence the retry.
    data = None
    for source in (frame_source(item, index), frame_path(item, index)):
        if source is None or not source.is_file():
            continue
        data = thumbcrypt.read_bytes(source)
        if data is not None:
            break
    if data is None:
        raise FileNotFoundError("Generate the frames first.")
    target = custom_path(int(item["id"]))
    thumbcrypt.write_bytes(target, data)
    return target

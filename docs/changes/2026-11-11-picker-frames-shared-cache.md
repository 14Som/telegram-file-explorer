# Picker frames share a slice cache (2.6.36 / 3.13.40)

A picker build used to take about a minute. Every grab downloaded its own copy
of the file header and the index at the tail, and a warm-up pass read another
6 MiB before the first frame could start. Both are gone: preview reads now fill
one shared 512 KiB cache, so the 18 grabs pay for the header and the index once.
Frames are unchanged — same video, same moments, same quality.

- `streamcache.read(direct=True)` serves preview reads from 512 KiB slices
  (`sNNNNN.slc`) cached next to the 2 MiB playback blocks. A slice is fetched
  once, single-flight per `(key, index)`, written atomically and served from
  disk to every later grab of the same file. Slice fetches are capped at 6 in
  flight so a picker burst stays under Telegram's GetFile rate limit.
- The picker URL returns to a 512 KiB window (`PREVIEW_STREAM_WINDOW`), and the
  old `prime_head`/`prime` warm-up was removed from `/api/stream`. ffmpeg stops
  reading after its keyframe, so anything past the window was cancelled mid-fetch
  and re-requested by the next grab; the slice cache serves those repeats instead.
- Coarse frames (18 across the runtime) seek with `-noaccurate_seek`, which reads
  one GOP (about five 512 KiB windows) instead of decoding from the start of the
  file. The refine strip keeps accurate seeks because it reads one GOP anyway.
- `thumbpick.busy()` makes the background thumbnail warmer step aside while a
  picker runs; automatic video covers are held to two at a time.
- Refine calls for one item are serialised (`_refine_locks`): two quick taps used
  to wipe each other's `.refine` folder and end in a 500. A missing fallback
  frame is skipped instead of failing the whole strip.
- `WORKERS` stays at 4. Six concurrent grabs plus a vault upload put the account
  into a GetFile flood penalty and one build sat at 300 s with no frames.
- GetFile requests follow Telegram's rules learned on the live account: the only
  accepted limit is 512 KiB, a read may not cross a 1 MiB block boundary, and
  offsets are 4 KiB-aligned. `tg.stream_chunks` uses a single request for window
  grabs that fit, and 512 KiB-aligned chunks otherwise.
- Rejected after measuring: `-skip_frame nokey` (10–11 requests per frame vs 5)
  and a 2 MiB preview window (126 requests, 19.2 s: cancelled slices were fetched
  again).

Verified on the live vault, cold cache (video 34:22, 1.34 GB):

- Build: 59.5 s before, 13.1 s and 22.3 s after in two runs, 91 GetFile requests,
  no flood waits. Frames started landing at 15 s and all 18 were in by 22 s.
- Refine: 17.9 s before, 6.6 s after for nine frames around the tapped moment.
- Second open of the same item comes from the candidate cache: `ready` with 18
  frames in 10 ms. Two refine calls fired at once both returned 9 frames (1.0 s
  and 2.1 s) with no error in the log.
- In the real app: the modal showed 8 frames within a few seconds and the rest
  filled in while it stayed usable.
- Desktop 2.6.36 app EXE md5 `bf30de1b6f360c0a1c495e774f387d62`; Setup EXE
  43,319,481 bytes, payload md5 matches the app EXE.
- APK 3.13.40 (versionCode 70, 76,667,154 bytes, md5
  `8c1d313b3915b6f0d038d86f0a5715e6`): `app.imy` carries the slice cache,
  `-noaccurate_seek`, the refine lock and no temporary diagnostics. Not
  installed on a phone yet.

Settings note: the app is left on Main Storage 1 (its default vault) after testing.

# Thumbnails fill in as they are grabbed, with a fine-tune strip (2.6.32 / 3.13.37)

18 frames still take a while on a long video, and the old picker showed
nothing until all of them were done. Now each frame appears the moment it is
saved, four grabs run at once, and clicking a frame opens nine finer moments
around it — about a second of scrolling instead of shuffling for minutes.

- `thumbpick._build` publishes a plan (`building`, times, version, `ready`
  indices) before the first grab; every finished frame is sealed, copied into
  the cache and added to `ready`. The modal polls every 700 ms and fills the
  matching tile, so the grid is usable at 4 frames as well as 18.
- `WORKERS` is 4. Shuffle only unlocks once all 18 are in, so a shuffle never
  mixes halves of two sets.
- Clicking a tile no longer commits it. It POSTs `/api/thumb-picker/{id}/refine`
  with the tapped time; the backend grabs nine frames in one ffmpeg pass
  (`fps`, `frame-%d.jpg`) inside a window capped at 12 s, falling back to
  parallel seeks for any frame the pass misses.
- New routes: `POST .../refine` (returns candidate URLs), `GET .../refine/{index}`
  (serves the sealed frame), `POST .../select-refine` (makes it the cover).
  Frames are served from `{key}.work/` while a build is still running.
- Phone picker: tapping a frame also opens a `.thumb-refine` strip; nine
  frames come from `AndroidBridge.frameAt` with the same 12 s window, and a
  check button commits the coarse frame if the fine ones are not better.
- README updated; desktop 2.6.32, APK 3.13.37 (versionCode 67).

Verified:

- Local harness against a Range-capable server: partial `ready` lists arrive
  before the build ends, all 18 exist after it, refine returns 9 times around
  the tapped second (midpoint exact), frames decrypt, and reset clears both
  sets. A refine pass takes 0.3 s locally.
- Live build on a 95-minute movie (5740 s): `pending` at 1 s, `building` with
  0 ready at 3 s, 4 ready at 7 s, 8 at 25 s, 12 at 41 s, 18 at 64.7 s. The old
  picker showed an empty dialog for the whole of that minute.
- Live refine on the same video first took 60 s because the window spanned the
  whole 319 s gap; capping it at 12 s brought that to ~1 s.
- Installed EXE (md5 `e91afce2b06da384071db388d995a8f5`, 2.6.32): the picker
  opened from cache with 18 tiles in 102 ms and 18 images by 204 ms; clicking
  a frame showed 9/9 fine frames spanning 12 s in 1 s, and the strip scrolls
  into view.
- Setup EXE (md5 `ef3f08bd5e23194ffe7f2c7ce8f15cf0`, 43,317,892 bytes) rebuilt
  from the new app EXE; its payload md5 matches the app EXE.
- APK 3.13.37 (versionCode 67, 76,661,270 bytes): `app.imy` holds REFINE_COUNT
  9 / COUNT 18 / WORKERS 4, the strip pass and all three refine routes;
  `mobile.js` and `mobile.css` carry the fine strip. Not installed yet — no
  phone was reachable over Wi-Fi debugging.

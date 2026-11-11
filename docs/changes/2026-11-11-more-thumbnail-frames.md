# Thumbnail picker grabs 18 frames instead of 5 (2.6.31 / 3.13.36)

Five frames were too few to pick a cover. The picker now pulls 18 evenly
spaced frames from the stream, and Shuffle replaces them with 18 new random
points that avoid the times it just discarded.

- `thumbpick.COUNT` is 18; the build runs three ffmpeg grabs at a time (same as
  preview sheets) and allows up to 10 minutes before giving up.
- Cached sets from the 5-frame version fail the completeness check and rebuild
  automatically the next time the picker opens.
- The desktop modal text no longer hardcodes a number, and its 3-column grid
  scrolls through six rows.
- The phone picker reads 18 frames too, spaced evenly from ~5% to ~95% of the
  runtime.
- README updated; version bumped to desktop 2.6.31 and APK 3.13.36
  (versionCode 66).

Verified:

- Frame times: 18 evenly spaced values, 18 shuffled values at least 1 s away
  from the previous set, still 18 for a 3-second clip.
- Local ffmpeg grab of 18 frames completed in 1.1 s against a Range-capable
  HTTP server (430 KB total).
- Rebuilt `TelegramFileExplorer.exe` (md5
  `a3cf4cb0e5ff6c50a264a6ae32ac6183`); the embedded `thumbpick` bytecode shows
  COUNT 18 and the 600 s ceiling, and the packaged `app.js` holds the new text.
- Installed the EXE (`/api/ping` reports 2.6.31) and opened the picker on a
  33:50 video: 18 tiles appeared with all 18 images loaded in 40 s on the
  first build, and reopened from cache in 1 s.
- Setup EXE (43,309,932 bytes) rebuilt from the new app EXE; its payload md5
  matches.
- APK 3.13.36 (76,654,538 bytes): `thumbpick.pyc` inside `app.imy` has COUNT 18
  and `mobile.js` reads 18 frames.

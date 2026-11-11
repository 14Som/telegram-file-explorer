# Keep a picker frame before the set finishes (2.6.37 / 3.13.41)

Frames show up one by one, but "Use thumbnail" only worked after the whole set
was published. Picking a frame that was already on screen returned "Generate
the frames first." A frame that is visible can now be kept right away; the rest
of the set keeps building in the background.

- `thumbpick.select` resolves the frame through `frame_source`, which also looks
  in the `{key}.work` folder a running build writes to, and retries the final
  `{key}` path in case the build renames the folder mid-read. No frame data,
  sizes or quality changed.
- The picker status line now says "Any that are ready can be picked and kept
  right away." instead of "wait for the rest".
- Verified in the installed EXE: with 10 of 18 frames loaded, a clicked frame
  saved with "Thumbnail updated." and the new cover appeared on the tile and in
  the details panel. No 409. Phone picker already saved tapped frames directly,
  so the APK only needed the rebuild.

Builds: desktop 2.6.37 (`TelegramFileExplorer.exe` md5
`907847463412db75695e7cf78de3da9c`), Setup EXE 43,323,628 bytes md5
`0a042c11590ea5cf3edb78e293f68ded`, APK 3.13.41 (71) 76,667,298 bytes md5
`fa514189689934f32b41672d1bb9064e`.

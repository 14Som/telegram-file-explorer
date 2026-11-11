# Sidebar folders open collapsed (2.6.33)

The folder tree always came back fully expanded. Expansion was saved in
`localStorage`, but the UI is served from `127.0.0.1` on a new random port each
launch, so the browser origin changed and the saved set was wiped with it.

- Expansion now lives in a session-only `expandedFolders` set; nothing is read
  from storage. Every parent folder starts collapsed, and folders discovered
  later (new subfolders, a sync) also start collapsed.
- Opening a folder with the chevron, the *Expand all* / *Collapse all* button
  and the expanded state on re-renders all work per session as before.
- On the live vault (161 folders, 8 top-level): 8 rows and 0 open chevrons at
  start; one chevron opened 1 row; *Expand all* showed all 161 with 70 open;
  collapsing returned to 8. A full quit and relaunch again opened with 8 rows,
  0 open.
- Desktop 2.6.33; `app.js` cache key bumped to 60.

Verified:

- Packaged `app.js` has no `tfe_fold_collapsed` reference, and `server.pyc`
  reports 2.6.33.
- Rebuilt `TelegramFileExplorer.exe` (md5
  `9ead06fedb2d3314d8e3eaf7cb84f33a`), installed to LOCALAPPDATA and clicked
  through with the real app.
- Setup EXE (md5 `2b617f27a9622934ae97b6d00436e397`) rebuilt from the new app
  EXE; its payload md5 matches.
- The phone has no sidebar folder tree, so the APK is unchanged.

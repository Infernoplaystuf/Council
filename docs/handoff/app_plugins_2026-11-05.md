# Council plugins: the foundation (2026-11-05)

Andy's decisions (2026-11-05, project thread "Add a life finance tracker to
Council"):

- The life finance tracker ships as Council's **first plugin**, so the plugin
  foundation comes first.
- Copy Typhon's `plugin.json` reader into qt-migration now rather than wait
  for PR #65 (typhon-plugins).
- **Every plugin starts off** and is switched on in the app, in a Settings tab.

## What is built

| Piece | Where |
|---|---|
| The manifest reader, shared with Typhon (typhon-plugins is merged into qt-migration) | `council_core/plugin_manifest.py` (Typhon's; Council does not change it) |
| Council's manifest check, discovery, on/off, context, loader, fault log | `council_core/app_plugins.py` |
| The built-in plugins' folder (empty list for now) | `council_plugins/` + `app_plugins.BUILTIN_PLUGINS` |
| A switched-on plugin's tab, guarded | `council_qt/plugin_tabs.py`, called from `council_qt.py` after the built-in tabs |
| The Settings tab, Plugins section | `council_qt/tabs/settings.py` (registered as "⚙ Settings") |
| Bundle | `council.spec` ships `council_plugins/` as files (they are imported from their folders) |
| Tests | `tests/test_app_plugins.py` |

## The rules

- A Council manifest is schema 1 with: `kind` "tab" (only kind so far),
  `council.` ids for built-ins (third-party ids may not use it), `network`
  "none" only, `needs` naming services (none offered yet, so a plugin that
  names one is listed with that reason), `python` requirements checked
  without importing. Unknown fields are refused, as in Typhon.
- Built-in = a folder of `council_plugins/` on `BUILTIN_PLUGINS`; folder name
  is the id without `council.`, dots to underscores. Any other folder with a
  manifest there is refused.
- Third-party plugins live in `<app dir>/plugins/<id with _>`; they are listed
  but can't be switched on until there is an approval step.
- On/off is `<app dir>/plugins.json` `{"enabled": [ids]}`; a missing or
  broken file means all off. Changes apply at the next start.
- Listing reads manifests only. A plugin's code is imported when its tab is
  first opened, from its folder as package `council_plugin_<id>` (no
  `sys.path` change).
- The entry is called as `entry(ctx)` and must return a QWidget. `ctx` is
  `app_plugins.PluginContext`: `id`, `name`, `window`, `data_folder()`
  (`<app dir>/plugin_data/<id>/`), `save_json`/`load_json` (atomic, plain
  file names only), `log`.
- Anything a plugin raises while loading or building (SystemExit included,
  not KeyboardInterrupt) goes to `<app dir>/plugin_errors.log`; its tab shows
  "<name> could not start: <reason>" and Council carries on.

## Next

1. Finance plugin (`council_plugins/finance/`, id `council.finance`) step 1:
   tax engine with 2025/2026 tables, paycheck breakdown, plan save/load. See
   the Finance proposal (project thread). Waiting on Andy's state, filing
   status and number of earners for the state tables.
2. Services for `needs` (local model, Connections search), each opt-in.
3. An approval step for third-party plugins (hash, then load), as in
   `docs/typhon_plugins_design.md` 4.5-4.6.
4. Moving existing tabs into plugins, one at a time where it helps.

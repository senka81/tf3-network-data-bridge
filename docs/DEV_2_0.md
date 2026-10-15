# 2.0.0-dev.1 package

This is a draft integration for the senka81 fork. It preserves the upstream MIT
license and bsaurusrex creator attribution. No live-game acceptance has been completed.

## Versions and layout

- DEV package/display label: 2.0.0-dev.1.
- Proposed Python server: 2.0.0-dev.1, standard library only, 38 tools.
- Lua bridgeVersion: 78; unchanged between the v8.6 and v8.6.1 development folders.
- DEV mod revision: 7; unchanged because the runtime exporter is unchanged.
- `mod/mcp_bridge_dev_1` is the DEV exporter. Keep its resource ID stable for saves.
- `mod/mcp_bridge_1` is the retained upstream exporter, not the intended companion
  for new tools requiring protocol 78.

The drive's v8.6.1 server still declared `1.4.0-dev76`. Its logic matches the uploaded
8.6.1 server; this proposal uses the explicit review version above. Metadata now
identifies the 2.0.0-dev.1 package and protocol 78 without changing game script identity.

The fork now uses semantic versioning: 2.0.0 reflects the expanded public tools and
required history keys; dev.1 marks the first development prerelease. Historical
versioned tool names remain stable for client compatibility.

## Changes

The 12 original upstream tools and their schemas remain. Twenty-six tools add
catchment aggregation, coverage, freight opportunity/reachability investigations,
industry cargo-access membership, town-building delivery membership, passenger
history, and passenger service/capacity diagnostics. These observations do not
prove connected routes, cargo acceptance, passenger destinations, persistent
crowding, construction feasibility or ROI.

v8.6.1 fixes the combined advisor's passenger-line handoff: it now retrieves the
full station/line diagnostic rather than passing `servingLines` where `lines` was
expected. It raises an explicit error if historical capacity and service-pattern
classifications disagree. Clients using `tf3_network_investment_advisor_v86` must
switch to `tf3_network_investment_advisor_v861`; the standalone v86 diagnosis remains.

Review safeguards require explicit history keys, reject malformed protocol message
shapes without terminating the process, disclose local history writes, and add the
standard macOS Steam location to discovery.

## Isolated setup and acceptance

Use a separate test save and a separate server process/configuration. Back up that
save first. Install only the DEV mod into your chosen test installation using the
game's local-mod procedure. Do not overwrite the working production installation.
Enable only the intended exporter: DEV and production share `mcp_bridge__` filenames,
so running both can select the wrong hello file and cleanup can remove the other's
exchange files. This branch does not deploy or enable either mod automatically.

Run `python3 -I server/tf3_mcp_server.py` through an MCP client for stdio transport.
The server also supports `--print TOOL JSON_ARGUMENTS` for manual checks. Set
`TF3_MCP_BRIDGE_DIR` to the test save's actual exchange folder if discovery is
ambiguous; set `TF3_HISTORY_DIR` to a separate private test history directory.
Do not use `--setup` unless you intend to modify Claude configuration.

Confirm `tf3_status`, the active save and protocol 78 before using new tools. Check
`tf3_station_catchment`, `tf3_cargo_access_audit` and `tf3_town_delivery_audit` for
supported exports, plausible rows and warnings. Record two or more advancing game
times using a unique `save_key`, then inspect history and the v861 advisor. Check
paused/duplicate reads, save reloads and stale fallback warnings. Restart the server
after switching saves to avoid short-lived cache reuse from the previous session.
Native API behavior and large-save frame costs need live verification.

## Local history and security

History recording writes names, IDs, station queues and utilization under
`TF3_HISTORY_DIR` or `~/.tf3-network-planner/dev-history`. Reporting may create the
directory too. Use different keys for independent playthroughs; reusing a key can
mix histories on the same map. Town renames can split a fingerprint. Automatic-key
histories are neither migrated nor deleted. Retention is capped at 500 samples per
recording, but files are not byte-limited. Use one history writer per directory.

Exchange files and history must be trusted local data in user-owned directories.
The server has no authentication and no comprehensive input-size limits or symlink
hardening. Lua userdata is read through the game's loader. Keep the established
trusted stdio/tunnel boundary; do not expose the bridge directly to a public network.
Do not commit save files, histories, snapshots, tunnel settings, credentials, caches
or backups. `.gitignore` excludes common local artifacts but cannot replace review.

## Reproducible validation

Use Python 3.14 (tested) or another compatible Python environment. Runtime needs no
third-party packages; only the simulated Lua test requires `lupa` (tested with 2.8).
Install test dependencies in a separate virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r tests/requirements-dev.txt
.venv/bin/python tests/test_review_safeguards.py
.venv/bin/python tests/test_v861_integration.py
.venv/bin/python tests/mod_server_integration_test.py
```

The safeguards suite checks original schemas, unique tool names, history separation,
duplicate/rollback/stale recording, old-mod gates, and malformed stdio messages.
The advisor fixture checks populated service lines, capacity versus service-pattern
classification, cargo preservation and conflict detection.

The adapted upstream simulator runs the actual DEV Lua and proposed server against
fake game APIs and its own temporary exchange. It checks all nine request types,
50 assertions, multipart/Unicode responses, cleanup, original outputs, catchment
deduplication, cargo industry membership and town-building membership. Its fake
catchment API deliberately returns repeated IDs. It does not touch a live game.

The original `tests/bridge_test.py` remains an upstream production-mod regression.
Run it independently; it creates its own temporary exchange. To test stdio fallback,
pass that run's printed `<out>/mod_presets` path to `tests/server_test.py`. The latter
now accepts additive tools while requiring the 12 original names in their relative order.

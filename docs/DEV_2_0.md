# 2.0.0-dev.1 package

This is a draft integration for the senka81 fork. It preserves the upstream MIT
license and bsaurusrex creator attribution. The controlled macOS live test passed on
October 15, 2026; broader production acceptance remains pending. See
[validation history](VALIDATION_HISTORY.md) for the earlier work and today’s checks.

## Versions and layout

- DEV package/display label: 2.0.0-dev.1.
- Proposed Python server: 2.0.0-dev.1, standard library only, 38 tools.
- Lua bridgeVersion: 78; unchanged between the v8.6 and v8.6.1 development folders.
- DEV mod revision: 8; adds snapshot timing fields needed for history spacing.
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

## Installation from this branch (DEV only)

There is no published 2.0.0-dev.1 release asset yet. Review the source on the PR
branch; do not use the upstream release download commands for this version.
The checked candidate archive contains only the DEV mod, server, license and DEV
documentation. It does not include the legacy mod, deploy script or test telemetry.

1. Use a copied save and a separate test environment. Locate the TF3 user-data
   folder from the game's Settings > Advanced > Open User Data Folder.
2. Copy the **leaf folder** `mod/mcp_bridge_dev_1` into `<userdata>/mods/`, giving
   `<userdata>/mods/mcp_bridge_dev_1/mod.json`. Do not nest the repository or the
   outer `mod` folder there. If that destination already exists, do not overwrite
   it: use another test environment or the separately namespaced test method. The
   controlled live test used a private mod ID/prefix to avoid replacing an installed
   DEV mod; those substitutions are not included in the candidate archive.
3. Save `server/tf3_mcp_server.py` in a separate test-server folder. Use the Python
   interpreter that has been validated for your platform (Python 3.14 on macOS
   was tested). The server runtime itself requires no pip packages.
4. Enable the DEV bridge only on the copied save. Disable other bridge exporters
   for that copy, leaving unrelated required mods enabled. Keep the original save
   and working MCP client/tunnel configuration unchanged.
5. Set `TF3_MCP_BRIDGE_DIR` to that test environment's actual exchange folder and
   `TF3_HISTORY_DIR` to a new private directory outside the checkout. Launch
   `python3 -I /path/to/test-server/tf3_mcp_server.py` through a separate stdio
   client or use `--print` for manual checks. Supply a unique `save_key` to history
   tools. Never point two active servers at the same exchange/history.
6. Confirm status, protocol 78 and the active copied save. A history-constrained
   recording requires valid timing fields; missing conversion now declines the
   recording instead of silently ignoring spacing.

**Do not run `deploy.ps1` for this DEV package.** It is retained upstream tooling:
it selects `mod/mcp_bridge_1`, writes to the original staging destination, and can
carry the original mod.io publication linkage. It is not a DEV installer or safe
publication path for this fork. Do not upload to the original author's listing.

The mod, server, root LICENSE and relevant docs must travel together in a reviewed
runtime package. Preserve the MIT notice when redistributing. Review every archive
member before publishing; ignore rules alone do not prevent disclosure.

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
supported exports, plausible rows and warnings. Record at least three adequately spaced advancing game
times using a unique `save_key`, then inspect history and the v861 advisor. Check
paused/duplicate reads, save reloads and stale fallback warnings. Restart the server
after switching saves to avoid short-lived cache reuse from the previous session.
Native APIs and responsiveness passed the controlled macOS test. Broader workloads
and long-session performance remain unverified.

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

Run these checks from the full PR source checkout. The reviewed runtime candidate
archive intentionally excludes tests, the legacy mod and deployment tooling.

Use Python 3.14 (tested) or another compatible Python environment. Runtime needs no
third-party packages; only the simulated Lua test requires `lupa` (tested with 2.8).
Install test dependencies in a separate virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r tests/requirements-dev.txt
.venv/bin/python tests/test_review_safeguards.py
.venv/bin/python tests/test_v861_integration.py
.venv/bin/python tests/test_live_readiness.py
.venv/bin/python tests/mod_server_integration_test.py
```

The safeguards suite checks original schemas, unique tool names, history separation,
duplicate/rollback/stale recording, old-mod gates, and malformed stdio messages.
The advisor fixture checks populated service lines, capacity versus service-pattern
classification, cargo preservation and conflict detection.

The adapted upstream simulator runs the actual DEV Lua and proposed server against
fake game APIs and its own temporary exchange. It checks all nine request types,
53 assertions, multipart/Unicode responses, cleanup, original outputs, catchment
deduplication, cargo industry membership and town-building membership. Its fake
catchment API deliberately returns repeated IDs. It does not touch a live game.

The original `tests/bridge_test.py` remains an upstream production-mod regression.
Run it independently; it creates its own temporary exchange. To test stdio fallback,
pass that run's printed `<out>/mod_presets` path to `tests/server_test.py`. The latter
now accepts additive tools while requiring the 12 original names in their relative order.

## Live-readiness review fixes

Both simulated bridges bind their exchange path directly to a temporary directory,
ignoring inherited production environment settings. The readiness suite runs both
with a decoy live path and verifies its files remain unchanged.

Passenger classification uses all current town stations and serving lines before
applying public display limits. Missing or stale current observations yield
`insufficient_current_evidence`; standalone and combined diagnostics retain current
warnings and ages. Public v82 display limits remain unchanged.

The protocol-78 snapshot now adds `yearTicks` and `dayTicks`. With `min_game_days`
greater than zero, recording declines if timing conversion is unavailable and rejects
non-finite or negative intervals. Existing history files remain compatible. The DEV
simulator verifies actual Lua timing fields through history recording.

The six readiness regressions cover >100 stations, >12 serving lines, missing current
stations, mixed freshness in standalone/combined output, interval enforcement and
invalid conversions, and both simulators with inherited production paths.

The controlled test passed all nine exports, history spacing/duplicates, stale-data
handling, advisor output and reload rollback protection on one copied macOS save.
The namespace-isolated test variant is not a shipped protocol change. Broader
platforms, workloads and longer sessions remain unverified. Keep the PR draft.

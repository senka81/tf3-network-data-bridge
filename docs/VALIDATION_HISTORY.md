# Validation history and release-review scope

## Earlier development — October 14 evening (user's “last night”)

The v8.6/v8.6.1 development packages already documented live bridge/advisor checks,
manual history reuse, passenger service diagnosis, and the combined advisor's
serving-line handoff fix. Existing live exchange artifacts and prior history were
present before this PR work. The user confirms substantial testing was already done.
This was not an untested bridge starting from zero.

The retained development README described how to test the combined and standalone
advisors, and the supplied v8.6.1 regression fixture checked populated serving lines,
capacity versus service-pattern cases, cargo preservation and conflict detection.
A README command or expected result is not a complete execution log: this review
cannot independently certify every earlier live assertion, exact revision or test
count. No private old logs or history are published here.

## PR preparation and review — October 15

The server/mod comparison established that the two development folders had identical
Lua exporters; v8.6.1 changed Python advisor logic. The package was renamed
2.0.0-dev.1, retaining protocol 78 and stable public tool identities. Review exposed
four gaps in the existing test coverage:

- inherited production exchange paths could defeat simulator isolation;
- internal display limits could hide passenger station/line evidence;
- service diagnosis could drop current stale-data warnings;
- snapshot timing metadata was missing for requested history spacing.

These were fixed and covered by six new readiness regressions. Five safeguard
tests, advisor fixtures, 53 DEV simulator checks, 44 upstream-mod simulator checks
and the stdio suite passed locally. GitHub reported no CI checks for the reviewed
revision; local validation must not be described as CI approval.

## Targeted controlled live validation — October 15

Head tested: `8a5624782073b33f20020687cd0af2f8bde09b24`.
One macOS save was copied, with separate private history and a test-only mod ID and
exchange prefix. The only test-variant substitutions were identity, prefix and
display metadata; the isolated variant also passed the 53 simulator checks.

The full nine-export smoke check intentionally repeated some earlier coverage to
catch integration regressions. It was not the first time live exports or advisor
behavior had been tested. The targeted additional evidence was:

- new timing metadata and enforced spacing between three accepted observations;
- duplicate/paused recording rejection;
- preserved stale warnings and insufficient-current-evidence classification;
- stale recording rejection without changing history;
- fresh reload responses and rejection of earlier-time history overwrites;
- complete passenger serving-line data and successful combined advisor output.

The inspected live exports reported no API errors or unsupported fields. The user
reported a responsive game. Protected original save, mod, server and prior-history
files matched pre-test checksums. Normal application logs/profile/autosaves could
still change during game use. Only the outcome summary is published; no private
save names, history contents, save keys, paths, raw exports or per-town findings
are needed in the release record.

## Final release review and limitations

Packaging and installation review does not require another broad live test cycle
unless code changes or new findings justify it. It checks the exact tracked file
allowlist, candidate archive layout, license inclusion, stable IDs, version labels,
relative documentation links and absence of private runtime artifacts.

The controlled live test supports a development prerelease on the tested platform;
it is not unrestricted production acceptance. Other platforms, broader save/mod
combinations, long sessions, heavier workloads and concurrent writers remain
unverified. The >100-stations-per-town and >12-serving-lines-per-station extremes
passed regression fixtures; this live save did not independently establish both.
Shared release exchange filenames and trusted local-directory assumptions remain
operational limits. Three observations provide preliminary advisory evidence only.
Keep the PR draft until the owner explicitly chooses the next release step.

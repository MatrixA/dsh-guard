# Acceptance scope

This document records prior validation and the methods needed to repeat it. Historical results apply to a local macOS installation of **DSH 0.2.2-rc.1**. They are not a certification of the generic installer, another machine, another DSH version, or native Windows.

Private deployment reports and screenshots are not distributed in this repository. No current live deployment status is asserted here. Inspect an installed system with `dsh-guard status` and its independent console.

## Recorded results

| Check | Prior result and scope |
|---|---|
| Python regression suite, 2026-11-09 | 77 tests run: 70 passed, 7 native Windows tests skipped on macOS. This is the pre-publication baseline, not a current release test count. |
| Host bridge | Five checks passed when the optional native DSH fixture was available. Default dependency-free runs skip that native check. |
| Native macOS development acceptance | All 11 checks passed using Python 3.11.12 and the same DSH version. Model repair was disabled. |
| Registered macOS service | Authenticated readiness held for a continuous 60-second window after handover and recovery. |
| Automatic checkpoint | A real development change was independently validated and accepted. Rejected candidates and concurrent edits did not replace the accepted checkpoint. |
| Guard upgrade | Guard identity changed while DSH identity and incident accounting remained intact; the supervisor maintained its heartbeat. |
| Stop and resume | A real control request was correlated with its final result. Stop persisted across Guard restart; resumed DSH passed the stability window. |
| Application entry | A fresh temporary browser completed the local authentication redirect and reached an editable DSH page. |
| Console | Desktop and 360px layouts, light/dark themes, cancellation, native Escape, and focus restoration were checked in a real macOS browser. |
| Persistent data and other instances | Existing session identifiers remained present; the registered scope did not take over independent listeners. This does not establish every storage backend's business behavior. |

## Publication checks, 2026-11-09

The current source was checked on macOS with Python 3.11.12 and Node 26.5.0:

| Check | Result and scope |
|---|---|
| Python suite with explicitly selected YAML 2.9.1 test dependency | 81 tests run: 74 passed, 7 native Windows tests skipped |
| Dependency-free bridge fixtures | 4 passed, 1 optional installed-DSH contract skipped |
| Generic installation fixture | Generated command and exact startup arguments ran after removal of the fixture source; changed terminal variables could not switch the registered state/home; existing installs and stale development captures were refused |
| Native generic macOS enrollment | A separate disposable DSH 0.2.2-rc.1 home passed prepare, sandbox validation, initial development activation, authenticated readiness, idle Host bridge, source-link preservation and graceful stop; no model calls or production writes |

This checks the new enrollment path separately from the historical deployment. It does not test registering this new job with the user's actual launchd session or every first-machine dependency installation. The GitHub Actions definitions have been added but have not run on GitHub; their Node 24 and Python 3.13 jobs are not local acceptance results. See [macOS setup](docs/macos.md) for the installation workflow.

## Repeatable checks

From the repository root, the ordinary regression checks are:

```sh
python3 -m unittest discover -s . -v
node --test bridge/test-bridge.mjs
```

These use temporary fixtures. They do not require production credentials or a live DSH service. Native checks may skip when their platform or optional dependency is unavailable; record each skip and its reason.

The optional bridge contract check uses an existing same-version DSH installation in a temporary profile:

```sh
DSH_GUARD_TEST_INSTALL_ANCHOR=/absolute/dsh-runtime/node_modules/@deepseek-ai/dsh/package.json \
  node --expose-internals --test bridge/test-bridge.mjs
```

For native development acceptance, first prepare a disposable same-version baseline with only the intended test dependencies, then choose a new private working directory:

```sh
python3 acceptance_development.py \
  --work /absolute/new-private-acceptance-directory \
  --baseline /absolute/prepared-release
```

This is an integration exercise with native processes and sandboxing. It is not part of ordinary CI. Preserve failures alongside successful reports and inspect the final report's scope before making an acceptance claim.

## Native development acceptance method

The 11-check exercise covers:

1. Scratch composition and authenticated startup.
2. Host bridge capability reporting.
3. An initial independently validated checkpoint.
4. Real source HMR and automatic checkpoint creation while preserving local package links.
5. Native plugin disable without a whole-process restart.
6. Planned restart without incident restart or model-budget consumption.
7. Rejection of broken optional source as a recovery point.
8. Malformed YAML leaving the active runtime tree available.
9. A real plugin fatal callback entering rescue while preserving the development workspace and persistent data.
10. Validation and return after source repair.
11. Persistent manual stop and explicit resume.

For an installed application, separately verify listener ownership, stable readiness, graceful stop, supervisor restart, data preservation, independent instances, and browser initialization. A ready core API does not prove the page has finished initialization. Console submission is not operation success; correlate the request with its final result.

## Findings retained from prior acceptance

Python 3.11 could stop forwarding a large stdin payload during repeated `communicate(None)` polling. Bounded private temporary files now supply validator input; the regression suite includes a delayed-reader case with 512KiB of input.

A transient optional MCP connection failure produced degraded status without a whole-process restart or incident/model-budget consumption. An operator retried it through native HMR while preserving the DSH process. This was a manual retry, not accepted automatic connector repair.

A prior repair exercise used explicitly synthetic diagnostics to enter restricted repair. The CLI invocation, candidate isolation, and publication were real; an unattended plugin fault triggering model repair was not established. The later 11-check native exercise disabled model repair. An earlier compound resume assertion failed without enough evidence to establish a cause; a later pass does not supply that missing diagnosis.

## Windows and remaining limits

Native Windows has **not been tested**. The seven native tests skipped on macOS; a Windows-shaped console fixture rendered on macOS only establishes rendering under that browser. PowerShell installation and scheduled-task scripts have not been parsed or executed on native Windows. The acceptance driver returns an incomplete result on other platforms and keeps real DSH and candidate isolation marked unaccepted. See [WINDOWS.md](WINDOWS.md).

Windows candidate containment, checkpoints, publication, and rescue are not implemented. Claude repair is disabled at execution boundaries, even if a configuration mistakenly enables it. Linux and WSL are unsupported.

Candidate checks cover a declared Host scope. Explicitly enrolled external MCP rows may be disabled in a scratch-only validation overlay and are recorded in the release manifest; their external behavior remains unverified. The live development bridge has no such exemption: every reported capability must be ready or disabled before checkpoint acceptance.

No acceptance claim covers arbitrary source repair, all external models or integrations, replay of external agent actions, uninterrupted in-flight work during process switches, cross-version data migration, bootstrap/interpreter failure, or availability while the machine is asleep or the user is logged out. Same-account filesystem access is not a strong isolation boundary.

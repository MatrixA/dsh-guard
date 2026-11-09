# Implementation and operating boundaries

The full recovery implementation targets **macOS and DSH 0.2.2-rc.1**. Native Windows has a separate supervision preview; Linux and WSL are unsupported. This is an independent community project. See [ACCEPTANCE.md](ACCEPTANCE.md) for the distinction between prior acceptance and current verification.

## Components

| Component | Responsibility |
|---|---|
| `dsh_guard.py` | Managed-process identity, authenticated readiness, persistent manual stop, incident accounting, checkpoints, rescue, bounded repair, and command execution |
| `launcher.py` | Install and verify versioned Guard artifacts; monitor the main-loop heartbeat; retry or select a verified previous version within persistent limits |
| `command.py` | Installed macOS CLI selecting a verified Guard application outside the development source tree |
| `console.py` and `ui/index.html` | Independent loopback status and operation controls, including local authenticated entry to DSH |
| `bridge/` | Optional read-only Host observer for native plugin operations, loader state, patch syntax, and capabilities |
| `validate.mjs` | Restricted edits to explicitly enrolled YAML scalars while preserving unrelated syntax and existing tags |
| `configure-watch.mjs` | Configure literal native source-watch roots without enabling arbitrary expressions |
| `macos_setup.py` | Generate an observation-mode installation, stable entry points, and a LaunchAgent plist; explicit activation remains an operator step |
| `windows_platform.py` and Windows scripts | Native process/file primitives, preview enrollment, installed CLI, and fixture acceptance |

The generic macOS installer defaults to fixed mode. `--development` explicitly sets development mode and requires the bridge to be registered already through native plugin management. Enrollment does not prepare a release, install the bridge, load launchd, or adopt an existing listener. The setup contract and generated paths are documented in [docs/macos.md](docs/macos.md). DSH home defaults to `DSH_HOME` or `~/.dsh`; Guard state and application paths respect XDG settings.

## Native development cooperation

DSH remains responsible for installing, removing, enabling, disabling, and hot-reloading plugins. Guard manages one registered `web` profile and its listener. Host registries remain in the DSH host plane; the optional bridge observes them without publishing a service or replacing native HMR.

Development mode is an explicit configuration workflow:

1. Install and enable the [bridge bundle](bridge/README.md) with native plugin management before preparing the development baseline.
2. Keep profile `cordis.yml` as `[]`; composition edits belong in `cordis.patch.yml`. Build linked plugins and declare concrete distributable paths in their `package.json` `files` lists.
3. Configure native HMR roots if source watching is needed. A `link:` dependency alone does not opt into source HMR.
4. Enroll with `macos_setup.py --development` to write `"development_mode": true` in the private Guard configuration. Prepare and validate a compatible baseline separately, with a handover plan that preserves the live workspace.
5. Let Guard launch the registered process with its bridge destination and epoch. Require fresh, idle bridge status and ready/disabled capabilities, then use `dsh-guard checkpoint`. Confirm the accepted checkpoint in status.

Enrollment does not convert an existing deployment or transfer process ownership. Enabling a flag alone does not establish a baseline. Follow the macOS operational guide for preparation, validation, and handover.

## Checkpoint acceptance

The native `package.json.lock` writer protocol and `.plugin-manager/run.json` activity marker are coordination points. Guard does not steal them. It captures under the native writer lock, releases the lock for expensive independent validation, then reacquires it before acceptance.

A checkpoint requires stable configuration/dependency fingerprints, a live identified child, authenticated `session/list`, fresh idle bridge status, and all reported capabilities ready or disabled. Fingerprints include local packages' declared build artifacts. Snapshot preparation materializes those artifacts independently; it does not turn the active development tree into a frozen release tree.

Automatic acceptance waits for readiness and a quiet period, normally at least 60 seconds. The same changed generation receives at most three automatic attempts, five minutes apart; new edits reset that generation's attempt count. Concurrent edits retain the candidate and defer acceptance. Healthy application status and pending candidate acceptance are reported separately.

The bridge's generation combines observer activation and observed event sequence. It is not a native transaction commit identifier. Native HMR does not expose complete pre-change notifications, so Guard cannot promise a snapshot before every write. Missing or ambiguous capability information blocks acceptance.

## Candidate isolation

`--dump-config` runs only in a scratch DSH home because native composition preparation can write profile files. macOS `sandbox-exec` restricts production filesystem access, external commands, external networking, and production loopback connections. Positive and negative containment checks run before candidate acceptance. Scratch homes do not link production sessions, storage, or credentials.

A full candidate boot remaps literal HMR roots into empty scratch directories. Nonliteral root expressions are rejected; other configuration and existing `!!js` are preserved. Real source HMR is exercised separately in native integration acceptance.

An operator may explicitly enroll unique native `mcp-*` rows in `validation_mcp_rows` when their external credentials cannot enter scratch. Each must resolve to one `@deepseek-ai/dsh-mcp-client` row; core rows cannot be excluded this way. The scratch-only overlay and release `validation_excluded_rows` record the exception. It establishes no external integration acceptance. Live development capability checks still include these rows.

## Failure and recovery

Every process action checks PID, account identity, startup identity, and complete command, together with listener ownership. Unknown listeners are not adopted or killed. Authentication uncertainty, optional component degradation, and sleep gaps are not by themselves proof of a whole-process crash. Native writer activity has a bounded grace period.

With the default incident policy, confirmed core failure receives at most two restart attempts. Development recovery then uses a compatible verified snapshot in a separate rescue home. Original configuration, linked source, and failed artifacts stay in the development workspace. Rescue shares current persistent data and directs credential writes to the original regular file; it does not restore historical sessions, storage, credentials, or databases. Legacy settings are not copied in a way that would repeat migration.

Changed development files can trigger another independent validation during rescue. Return requires successful real startup and stability checks. Edits made in the rescue profile block automatic return so both sets of changes remain available for reconciliation. The older fixed deployment mode retains journaled configuration/dependency publication and interruption recovery. Unknown cross-version data migrations are rejected.

Manual stop is written before waiting for other operations, can cancel bounded validation/repair, and survives Guard and launcher restarts. Graceful DSH stop does not escalate to a force kill. Planned restart must use the Guard control path because DSH does not provide a reliable native restart-intent notification.

## Restricted model repair

macOS repair is disabled by default. When explicitly configured, the existing Claude CLI receives only a reduced diagnostic and a JSON Schema for enrolled operations. It runs with safe/restricted settings, no built-in tools, strict empty MCP configuration, no interactive permission wait, and no session persistence. Selected authentication/routing/model settings are read privately for each call; they are not copied into a plist or Guard configuration.

The generic installer enrolls no optional bundles or YAML fields. A prior local policy enrolled only git-graph, skill-explorer, task-board, and thoughtdag; those identifiers remain in the legacy preparation defaults. Any repair scope must be reviewed explicitly. The validator permits only existing enrolled scalar values with expected-value checks and rejects sensitive paths. Proposals cannot change arbitrary source, shipped packages, shared instructions, permissions, tools, network endpoints, credentials, or Guard itself. Existing `!!js` is preserved rather than evaluated or added.

Per incident, limits are two calls, 20 minutes total, 30 reserved turns, and an estimated USD 6 threshold. Budget is reserved before invocation; interrupted or missing usage retains uncertain accounting and blocks further calls. Estimated CLI cost is not a billing guarantee. Authentication or policy failure does not broaden permissions. Windows refuses every repair execution path.

## Supervisor and console

The launcher runs from an installed location outside the development tree. It checks the version manifest, exact artifact membership, regular files, and SHA-256 hashes. Development source edits do not update a running installed Guard.

Normal heartbeat freshness is bounded at 180 seconds, with a separate startup grace period. Long operations have an absolute deadline of at most 20 minutes; refreshing their heartbeat cannot extend it indefinitely. CLI invocations do not impersonate the supervising loop. Retry accounting survives launcher restart. Only the launcher's own Guard child may be terminated during recovery; the independently identified DSH process remains available for the next Guard to verify.

Console requests use same-origin/token checks, an atomic queue, priority stop handling, and request identifiers linking submission to final results. “Open DSH” verifies identity and listener ownership around a bounded private log read and returns a local authentication redirect. DSH authentication URLs are not placed in console HTML or status JSON.

Same-account processes can still rewrite installed files. Artifact hashes detect drift, not publisher identity. Bootstrap/interpreter damage, operating-system failure, and availability during logout, sleep, or shutdown remain outside this recovery model. See [SECURITY.md](SECURITY.md) and the architecture notes for [external supervision](.agents/notes/implemented/architecture/external-supervisor.md) and the [Windows preview](.agents/notes/implemented/architecture/windows-supervision-preview.md).

# External supervision and bounded recovery

Decision recorded 2026-11-08; development cooperation added 2026-11-09.

## Context and decision

Recovery must remain usable when the Cordis application cannot start or its event loop fails. Use an external Python standard-library supervisor, an independently installed launcher, and a macOS user LaunchAgent. Register one DSH `web` profile and listener explicitly; independent profiles and unknown listeners remain outside that registration.

This is community ecosystem code targeting DSH 0.2.2-rc.1. It does not modify shipped DSH packages, replace native plugin management, or establish compatibility with unknown data migrations. Windows has a narrower [supervision preview](windows-supervision-preview.md); Linux and WSL are unsupported.

## Process and recovery boundary

A PID alone is insufficient authority. Process actions require account identity, startup identity, the complete command, and listener ownership. macOS identity sampling fixes the locale and timezone so launchd and an interactive shell agree. Legacy records require explicit matching evidence before migration; the identity predicate is not generally weakened.

Manual stop persists before waiting for a busy operation and survives supervisor restart. DSH receives a graceful stop only; timeout blocks rather than escalating to force kill. Planned restart uses the Guard control path, since native DSH events and exit codes do not establish user restart intent.

Confirmed core failures receive bounded restarts, then a compatible verified recovery path. Incident budgets and blocking survive process restart. Authentication uncertainty, an optional component's degradation, or a sleep interval alone do not establish a core crash.

Recovery snapshots freeze runtime and installed plugin artifacts separately from the live workspace. macOS candidate validation first proves containment with positive and negative checks, composes only in scratch, then checks authenticated readiness and the declared Host capability scope. Validation excludes production persistent data and external networking. Journaled publication remains available for the older fixed mode. Cross-version runtime changes are rejected without a separate data-migration design.

## Development cooperation

The active development environment retains mutable dependencies and real local links. Native DSH still owns package operations, configuration HMR, and explicit source HMR roots. Guard does not make linked source watchable automatically.

The optional Host bridge reports owned scalar diagnostics through an atomic private file. It publishes no service, does not modify HMR, and preserves native continuation errors. Its generation is observer activation plus observed event sequence, not a native transaction commit identifier. The absence of full native pre-change events prevents a guarantee of checkpointing before every file write.

The native writer-lock protocol and operation marker provide cooperation points. Capture occurs under the native lock, independent validation runs after releasing it, and acceptance reacquires the lock to compare fingerprints and bridge generation. A quiet window and actual startup checks complement this coordination. Concurrent changes retain the candidate and defer acceptance rather than replacing a good checkpoint.

A checkpoint freezes declared distribution artifacts without replacing the healthy process or active development directories. Recovery in development mode creates a separate rescue DSH home from a verified same-version snapshot. It preserves the broken configuration and source for diagnosis, shares current persistent data, and directs credential writes to the original regular file. Sessions, storage, credentials, and databases are not rolled back. Legacy settings are not copied in a way that repeats migration.

A changed development workspace is independently validated before return. Actual startup and stability checks must then pass. Rescue-profile edits block automatic return to preserve both sets of changes for reconciliation. No external agent action is replayed automatically, and process switches may interrupt active work.

Explicitly enrolled external MCP exclusions apply only to a scratch validation overlay, must identify unique native MCP client rows, and are recorded in the release manifest. They do not establish external service behavior. Live development checkpoint acceptance still requires every bridge capability to be ready or disabled.

## Repair authority

Model repair is optional and disabled by default. The generic installer enrolls no bundle or scalar edits. Earlier local acceptance used an explicit four-optional-bundle policy; it is not a general permission to repair arbitrary plugins.

The existing Claude CLI proposes JSON Schema constrained operations without tools, MCP servers, arbitrary source access, or interactive permission waits. Authentication and model settings are selectively read privately rather than copied into a plist. Per incident, at most two calls share 20 minutes, 30 reserved turns, and an estimated USD 6 threshold. Missing or interrupted usage retains uncertain accounting. Cost estimation is not a billing hard cap, and policy failures never cause broader permissions.

A candidate proposal still passes deterministic policy checks and independent validation before publication. Existing `!!js` is preserved rather than evaluated or added. Shared instructions, permissions, tools, endpoints, credentials, shipped code, and the supervisor itself remain outside repair authority.

## Launcher and independent console

Guard versions, bootstrap, and CLI are installed outside the development tree. Artifact membership, regular-file status, and hashes are checked before launch. The launcher monitors the supervising loop, with fixed deadlines for long operations and a persistent bounded retry ledger. CLI calls cannot refresh that loop's heartbeat. Recovery may terminate only the launcher's own Guard child, preserving DSH for the next Guard to identify.

The console remains available independently of DSH. It reports application readiness, pending checkpoints, supervisor heartbeat, and final control results separately. Same-origin controls, private tokens, an atomic queue, priority stop, and request correlation prevent a submitted operation from appearing completed prematurely. Opening DSH uses an identity-checked local authentication redirect; authentication URLs are absent from status JSON and console HTML.

The generic macOS installer generates an observation-mode installation and LaunchAgent file. `--development` explicitly selects development configuration after bridge installation. Baseline preparation, validation, handover, and launchd activation are separate operator steps. It does not infer ownership from an existing port.

## Consequences and evidence

The supervisor cannot repair its own bootstrap, a broken interpreter, operating-system failure, or third-party dependencies outside its registered scope. User login is required; sleep and shutdown interrupt availability. Hashes detect drift without authenticating a publisher, and same-account access is not a strong isolation boundary.

Prior local macOS acceptance exercised native HMR, checkpointing, real fatal-callback rescue, return to development, persistent stop, independent browser entry, and console behavior. Model-repair acceptance used synthetic diagnostics and does not establish unattended fault-to-model recovery. New installer verification remains separate. Public methods and coverage limits are recorded in [ACCEPTANCE.md](../../../../ACCEPTANCE.md); installation is documented in [docs/macos.md](../../../../docs/macos.md).

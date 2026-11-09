# Native Windows supervision preview

This preview provides a Windows process backend, installation scripts, and fixture acceptance tools for **DSH 0.2.2-rc.1**. **It has not been run on native Windows.** The PowerShell scripts have not been parsed or executed there, and real DSH lifecycle behavior remains unverified. Linux and WSL are unsupported.

The preview does not provide macOS recovery parity. Windows candidate containment, checkpoints, publication, and rescue are **not implemented**. Claude repair is disabled at execution boundaries, including when a configuration mistakenly enables it.

## Implemented scope and verification gaps

| Capability | Implementation | Verification |
|---|---|---|
| Native paths and encoding | Python 3.11+, native Node executable, UTF-8 and paths with spaces | Portable contract checks only; native Windows pending |
| Process identity and listeners | FILETIME, account SID, executable path, full command snapshot, IPv4/IPv6 listener tables | Native checks provided, not run; failed inspection blocks rather than implying exit or a free port |
| Stop, resume, planned restart, fault restart | Shared persistent state machine; default maximum of two fault restarts before blocking | Native fixtures provided, not run |
| Graceful stop | Separate hidden console; identity-checked helper sends CTRL+C | Positive/negative Node fixtures provided; real DSH not accepted; timeout never escalates to force kill |
| Guard/launcher failure | Guard-only kill-on-close Job; DSH must successfully break away; verified Guard versions and bounded heartbeat recovery | Native child-process fixtures provided, not run |
| Locks and command publication | LockFileEx, regular-file checks, rejection of reparse/junction paths, NTFS exclusive hard-link queue | Native checks provided, not run |
| Independent console | Preview limitations displayed; recovery controls hidden; authenticated local redirect | HTTP checks and a Windows-shaped fixture rendered on macOS; Windows browser pending |
| Login startup | Current-user Task Scheduler; no stored password or administrator service account | PowerShell/background task behavior pending |
| Existing native development flow | Installer preserves DSH home and runtime; supervisor observes native writer markers | Real Windows install/enable/disable/link/HMR not tested |
| Plugin inventory | Preview installer does not install the Host bridge; authenticated core interface only | Core readiness does not prove all plugins, models, or MCP services are ready |
| Candidate isolation and recovery | Not implemented; execution paths reject | No acceptance claim |
| Claude repair | Disabled on Windows | No model invocation is required by fixture acceptance |

Enrollment records a `launch_only: true`, `validated: false` baseline. It pins the local Node and DSH entry paths and hashes, without freezing the full runtime or plugin dependencies. It never becomes a known-good recovery point. Broken configuration may exhaust the restart limit and require manual repair. Active native configuration, HMR, and local links remain in their original workspace.

## Requirements

- Native Windows 10/11, a normal Python 3.11+ installation with `python.exe` and `pythonw.exe`, and native Node.js **24.9.0 or later** (`node.exe`), as required by DSH 0.2.2-rc.1. AMD64 and ARM64 each require real-machine acceptance.
- A local NTFS installation directory, current-user execution, and an interactive user login for scheduled startup.
- An already prepared DSH `web` profile and a persistent runtime containing `node_modules/@deepseek-ai/dsh/lib/bin.js`. Keep the selected Node, runtime, and home available; use no disposable package cache as an installation dependency.
- PowerShell/CIM support. Scripts do not change execution policy or elevate privileges.
- Native executable paths, without `.cmd`/`.bat` wrappers, UNC/device/ADS paths, or installation-directory junction/cloud reparse points. Explicit runtime inputs are resolved before regular-file checks.
- Background Job inheritance must permit DSH to break away. Failure blocks startup; disabling that constraint is not an accepted workaround.

Installation ACLs grant access to the current user and SYSTEM. They do not defend against the same account or an administrator. Windows metadata durability is not equivalent to POSIX directory `fsync`.

## Run fixture acceptance first

Use a fresh checkout or extracted package directory and select actual local executables. The following paths are placeholders:

```powershell
$python = 'C:\Python311\python.exe'
$node = 'C:\Program Files\nodejs\node.exe'
.\Test-DshGuard.ps1 -Python $python -Node $node
.\Test-DshGuard.ps1 -Python $python -Node $node -Scheduled
```

The second command creates a temporary current-user scheduled task to exercise background Job inheritance. Fixtures use temporary directories, fresh ports, and owned subprocesses. They do not load a production Guard configuration, modify a production DSH home, or invoke Claude. Preserve each generated private JSON report.

The seven native tests cover identity and IPv4/IPv6, authenticated Node readiness and graceful stop/reload, refusal to control unknown or unproven processes, no force kill after stop timeout, cross-process locks and complete queue publication, rejected junction/ADS paths, DSH surviving Guard Job closure, and a real installed launcher restarting Guard after its source tree is removed. Separate contracts cover disabled recovery, console behavior, and fixed heartbeat deadlines.

A successful fixture report sets only `native_windows_fixture_checks_passed: true`. It retains `native_dsh_accepted: false` and `candidate_isolation_accepted: false`. Missing Node, CIM, IPv6, or Job breakaway, and any failed or skipped native check, prevent success. On macOS, the Python acceptance driver records skips and returns 2; this is an incomplete result.

## Enrollment and operation

After fixture acceptance, use a disposable same-version DSH home for real-application testing before considering an existing service:

```powershell
$runtime = 'C:\tools\dsh-runtime'
$dshHome = 'C:\tools\dsh-test-home'
.\Install-DshGuard.ps1 -Python $python -Node $node -Runtime $runtime -DshHome $dshHome -Port 33080 -ConsolePort 33081
$guard = Join-Path $env:LOCALAPPDATA 'dsh-guard\dsh-guard.ps1'
& $guard status
```

The default install is `%LOCALAPPDATA%\dsh-guard` and starts in observation mode. Enrollment alone does not start the launcher. `-RegisterStartup` registers a current-user login task; `-Start` starts the launcher, using that task when registration is also requested. Apply those switches only after checking their generated paths and background fixture results. Existing installation/configuration/bootstrap files are not overwritten. The package SHA-256 manifest establishes file integrity, not publisher identity.

When both fixture modes pass and real DSH has demonstrated graceful CTRL+C shutdown, stop an existing registered `web` service through its original terminal before explicitly enabling management:

```powershell
& $guard resume --manage
& $guard status
& $guard restart
& $guard stop
```

The console defaults to <http://127.0.0.1:3081> while Guard is running; the disposable example above selects <http://127.0.0.1:33081>. Stop persists and leaves the console available. Resume does not adopt an unknown listener. Planned restart does not consume fault-restart budget. A stop timeout leaves the process and stop marker intact for investigation.

The startup task is named `DSH Guard - <current user SID>` and runs only at user login. Inspect its interpreter, arguments, working directory, account, and `LastTaskResult`. Task Scheduler and the launcher both have bounded retry policies. Availability during logout, sleep, or shutdown is not promised.

## Real DSH acceptance still required

1. Use a disposable same-version home and spare ports, for example `-Port 33080 -ConsolePort 33081`. Verify authenticated session listing, full page initialization, and local console entry.
2. Stop from the console and verify DSH's native cleanup. Restart the launcher and confirm stop persists. Resume and verify the application is editable again.
3. Exercise native install/remove/enable/disable, configuration HMR, and real source link/HMR independently. An optional integration failure must not trigger a whole-process restart. Core API success is not a full Host capability audit.
4. Under the scheduled task, exercise fault limits, persistent blocking, Guard/launcher crashes, duplicate startup, and a verified previous Guard version. Check original process identity and persistent data.
5. Implement and prove Windows candidate containment separately before enabling recovery. Required negative and positive checks include production reads/writes, external commands, parent-process control, external network access, and production loopback access.

None of these native Windows acceptance results is currently available. Treat this as a supervision preview requiring local evaluation, not an accepted unattended recovery installation. The design is recorded in the [architecture note](.agents/notes/implemented/architecture/windows-supervision-preview.md).

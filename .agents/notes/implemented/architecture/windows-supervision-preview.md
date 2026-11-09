# Native Windows supervision preview

Decision recorded 2026-11-09.

## Scope

Reuse the existing stop/resume, health, incident-budget, console, and launcher state machines for a limited native Windows process supervisor. Keep platform primitives in `windows_platform.py`, and install the bootstrap and versioned application outside the development source directory.

The target is native Windows with Python 3.11+ and DSH 0.2.2-rc.1. Real DSH requires Node.js 24.9.0 or later. Linux, WSL, and Wine are not supported. The implementation and acceptance tools exist, but **native Windows acceptance has not been run**. Portable contracts and a Windows-shaped status fixture rendered on macOS do not establish Win32, PowerShell, scheduled-task, or real DSH behavior.

## Identity and stopping

Identify a process by FILETIME, account SID, executable path, and a full command snapshot. CIM command results are cached by PID and creation time; this is not continuous detection of PEB command-text modification. If identity inspection fails, check native liveness: a live or uninspectable process blocks action instead of being treated as exited. Listener inspection covers IPv4 and IPv6; query failure is not an empty listener table.

Persistent DSH requires a separate hidden console and `CREATE_BREAKAWAY_FROM_JOB`. Failed breakaway does not fall back to a weaker launch. A short-lived helper attaches to that console, holds and rechecks the target handle's creation time, then broadcasts CTRL+C. Guard also requires proof that it created the console and verifies listener ownership. Graceful-stop timeout does not escalate to force kill.

## Job and background startup

The launcher creates a fresh kill-on-close Job allowing breakaway for each Guard child. The child waits to be assigned before it may start DSH. Closing the launcher's Job terminates Guard; DSH must already have broken away so a replacement Guard can verify it. Job assignment and termination use the current owned process handle rather than a historical PID.

Current-user Task Scheduler provides login startup without a stored password or administrator service account. The inherited outer Job must allow DSH breakaway. This requires a real scheduled fixture; an interactive terminal pass is insufficient, and relaxing the breakaway requirement is not an accepted workaround.

## Files and launch baseline

Windows file primitives use regular-file/reparse checks, LockFileEx, content flushing, `MoveFileEx` with write-through, and exclusive NTFS hard-link command publication. Installation ACLs grant access to the current user and SYSTEM. They do not create same-account isolation, resist administrators, or promise POSIX directory `fsync` durability.

A preview manifest is explicitly `launch_only: true` and `validated: false`. It records local launch paths and hashes without freezing mutable dependencies or granting known-good status. Guard-version verification and bounded fallback apply to the supervisor, not to an accepted DSH recovery snapshot. Bootstrap or interpreter damage remains outside this mechanism.

## Recovery remains closed

There is no Windows candidate containment implementation with the required positive and negative proof. Candidate execution, checkpointing, publication, rescue, and model-repair entry points therefore reject on Windows. A mistakenly enabled `repair_enabled` setting cannot bypass this boundary. Exhausted process restart attempts lead to persistent blocking and manual investigation.

A temporary directory, ACL, or Job is not by itself equivalent to the macOS candidate sandbox. Windows recovery requires a separate design and checks for production reads/writes, external commands, parent-process control, external networking, and production loopback access before these paths can open.

## Verification contract

Provide native Node/Python fixtures for process identity, listener ownership, graceful stop, locking, path rejection, Guard-only Job termination, installed-source independence, launcher restart, and background inheritance. Ordinary fixtures use only temporary directories, fresh ports, and owned subprocesses; they do not depend on production credentials or a live DSH installation.

Fixture success sets only `native_windows_fixture_checks_passed`. Real DSH and candidate-isolation acceptance remain false. Skipped native tests are incomplete results. PowerShell parsing/execution, real DSH cleanup, browser initialization, native package operations, configuration/source HMR, and both processor architectures require actual Windows results.

See [WINDOWS.md](../../../../WINDOWS.md) for commands and the remaining acceptance checklist, and [ACCEPTANCE.md](../../../../ACCEPTANCE.md) for historical results.

## Platform references

- [Process creation flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)
- [AttachConsole](https://learn.microsoft.com/en-us/windows/console/attachconsole)
- [GenerateConsoleCtrlEvent](https://learn.microsoft.com/en-us/windows/console/generateconsolectrlevent)
- [Job objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- [GetExtendedTcpTable](https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-getextendedtcptable)
- [Win32_Process](https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-process)
- [MoveFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-movefileexw)

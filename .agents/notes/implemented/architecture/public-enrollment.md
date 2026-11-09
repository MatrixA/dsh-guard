# Public enrollment and environment contract

2026-11-09.

The public project is an independent MIT-licensed DeepSeek Harness ecosystem utility. Local migration scripts, personal startup files, raw deployment evidence and old packages are excluded from source distribution. Public acceptance describes scope and reproducible methods; it does not distribute private runtime state.

A first-install macOS tool generates a private stable application, pinned command, observation-only configuration and launchd plist. It neither loads the job nor changes DSH profiles. Executables and directories are discovered once; the installed command supplies its absolute registered state path. Environment changes cannot silently select another home or runtime for an existing installation. Existing installations are refused rather than overwritten.

Preparation before initial management selects the explicitly captured baseline. Development capture records the working fingerprint; initial management requires independent candidate validation and an unchanged fingerprint. It records the live hashes without invoking fixed publication, preserving the original node_modules and source links. Later checkpoints retain their existing independent acceptance policy.

Both process launch filters preserve the same transport, trust and package-directory settings. They keep lowercase proxy precedence, do not forward Node/Python injection variables or ambient model credentials, and do not disable TLS verification. Claude's registered settings file supplies only its dedicated authentication/routing/model and transport allowlist; hooks and permissions are not loaded. Startup managers do not inherit terminal shell configuration, and installers do not automatically serialize proxy or credential values into startup files.

DSH 0.2.2-rc.1 declares Node >=24.9.0. Installers check that requirement; the independent bridge's narrower engine declaration does not lower the runtime requirement. New Guard versions carry the MIT notice. The launcher still validates older manifests which predate that notice, retaining previous-version fallback compatibility.

Windows remains a native supervision preview with no candidate containment or recovery. Portable tests and proposed CI do not establish a native Windows pass. The macOS first-install fixture runs in temporary directories and does not replace an existing deployment. See [acceptance](../../../../ACCEPTANCE.md), [macOS setup](../../../../docs/macos.md) and [environment](../../../../docs/environment.md).

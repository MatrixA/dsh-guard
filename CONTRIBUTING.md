# Contributing

Contributions are welcome to this independent community project in the DeepSeek Harness ecosystem. The project's original code is MIT-licensed; contributions should be compatible with that license. Upstream DSH, Node.js, plugins, and dependencies keep their own licenses.

## Local checks

Use Python 3.11+ and Node.js 24.9.0 or later, matching the supported DSH runtime. From the repository root:

```sh
python3 -m unittest discover -s . -v
node --test bridge/test-bridge.mjs
```

Python tests use the standard library. The default bridge tests use Node's built-in test runner and need no package installation. Ordinary CI should use temporary fixtures, fresh ports, and owned subprocesses, with no production DSH dependency, credentials, external service calls, or paid model requests.

Some tests require a native platform or an explicitly selected existing dependency. Report skips as skips. Two YAML-preservation checks read `DSH_GUARD_TEST_YAML_PACKAGE`, an absolute path to an installed `yaml/package.json`; without it they skip. CI installs exact YAML 2.9.1 into a temporary test-dependency directory and sets that variable, without requiring DSH or production configuration. The optional native bridge contract uses an installed DSH 0.2.2-rc.1 package:

```sh
DSH_GUARD_TEST_INSTALL_ANCHOR=/absolute/dsh-runtime/node_modules/@deepseek-ai/dsh/package.json \
  node --expose-internals --test bridge/test-bridge.mjs
```

This mounts a temporary profile and checks native bundle/parser/event behavior. It does not certify the real application's lifecycle or all integrations. See [ACCEPTANCE.md](ACCEPTANCE.md) for native macOS integration methods and [WINDOWS.md](WINDOWS.md) for Windows fixture and scheduled-task checks. Live acceptance belongs on a disposable local installation, outside ordinary CI.

## Change scope

Keep changes small and explain the concrete behavior they fix. Add a regression check when changing process identity, ownership, stop behavior, persistence, isolation, recovery policy, or accounting. Prefer the standard library and existing native platform mechanisms over new dependencies.

Preserve these boundaries:

- DSH owns plugin management, profile composition, and HMR. Do not patch shipped packages or move host services into an agent preset to bypass a limitation.
- Profile `cordis.yml` remains `[]`; composition changes belong in `cordis.patch.yml`. Test composition in a disposable home because native preparation may write profile files.
- Unknown processes and listeners are not adopted or killed. Manual stop persists, and graceful DSH stop does not escalate to force kill.
- Candidate acceptance requires the declared isolation and readiness checks. Optional capability failure or unknown state must not be labeled success.
- Recovery does not roll back persistent sessions, storage, credentials, or databases. Model repair stays explicitly enrolled, bounded, and disabled on Windows.
- A portable fixture pass is not native Windows acceptance, and a native fixture pass is not real DSH acceptance. Linux and WSL remain unsupported unless separately implemented and verified.

Use synthetic data. Do not commit local configuration, credentials, authentication URLs, private logs, session content, installation state, or machine-specific evidence. Review diagnostics manually before sharing; automated redaction is not a guarantee. Report suspected vulnerabilities through the process in [SECURITY.md](SECURITY.md).

## Documentation and review

Keep `README.md`, `README.zh.md`, and `README.i18n.yaml` aligned, including the bridge's README trio. Update behavior and limitations together. Record architectural decisions under `.agents/notes/implemented/architecture/` when they change a meaningful boundary.

For a proposed change, describe the trigger, resulting behavior, relevant checks and their outcomes, and remaining platform limitations. Keep historical acceptance distinct from current verification. Use placeholders for paths and never invent a repository URL or reporting address.

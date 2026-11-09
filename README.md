# dsh-guard

[简体中文](README.zh.md)

dsh-guard is an external supervisor for [DeepSeek Harness (DSH)](https://github.com/deepseek-ai/deepseek-harness). It keeps supervision outside the application being recovered, preserves native plugin management and hot reload, and verifies recovery candidates before accepting them.

This is an independent community project in the DeepSeek Harness ecosystem. It is not an official DeepSeek project and is not affiliated with or endorsed by DeepSeek.

## Status

| Platform | Scope | Verification |
|---|---|---|
| macOS, DSH **0.2.2-rc.1** | Process supervision, verified checkpoints, separate rescue home, return to development, optional restricted repair | Prior local process, development, and browser acceptance; generic enrollment also checked with temporary fixtures and an isolated native DSH home |
| Native Windows | Limited process supervision preview and acceptance scripts | **Not tested on native Windows**; candidate isolation and recovery are not implemented, and Claude repair is disabled |
| Linux / WSL | Unsupported | No acceptance claim |

See [acceptance scope](ACCEPTANCE.md) and the [Windows preview](WINDOWS.md). Prior deployment acceptance does not certify a fresh installation or a different DSH version.

## macOS installation

Start with the [environment requirements](docs/environment.md) and [macOS setup and operation](docs/macos.md). Use Python 3.11+, a persistent DSH runtime, and an absolute Node.js **24.9.0 or later** executable path:

```sh
python3 macos_setup.py --runtime /absolute/dsh-runtime --node /absolute/node
```

The installer defaults to `DSH_HOME` or `~/.dsh`, respects XDG directories for Guard files, and generates an observation-mode installation, a LaunchAgent plist, and a stable CLI. It does not prepare a release, load launchd, or take over an existing DSH listener. Review the generated installation and follow the macOS guide to prepare and validate a baseline before enabling management.

Once installed, the CLI exposes:

```sh
dsh-guard status
dsh-guard stop
dsh-guard resume --manage
dsh-guard restart
dsh-guard checkpoint
dsh-guard return-development
```

`resume --manage` requires explicit handover and a usable validated baseline. `checkpoint` and `return-development` require development mode. Manual stop persists across supervisor restarts. Planned restart uses the Guard command or console; a direct exit signal is not native proof of planned restart intent.

The independent console defaults to <http://127.0.0.1:3081> when the supervisor is running. It shows application readiness, Guard heartbeat, pending changes, and operation results separately. Its “Open DSH” action checks the managed process and listener before using a local authentication redirect.

## Development and recovery

Keep using DSH's native Plugin Manager, profile patches, and source HMR. For a mutable development workflow, install the optional [Host bridge](bridge/README.md) through native plugin management first, then add `--development` during installation to set `development_mode` explicitly. Baseline preparation, validation, handover, and the first checkpoint remain separate steps in the macOS guide. The installer defaults to fixed mode. A local package link alone does not enable source watching; configure native HMR roots separately.

In development mode, Guard freezes declared plugin artifacts while preserving the live workspace. It accepts a checkpoint only after native writers are idle, capabilities are ready or disabled, independent sandbox checks pass, and file fingerprints and observer generation remain stable. Invalid configuration or an optional plugin failure can defer a checkpoint while the current healthy process continues running.

After bounded restart attempts, a compatible verified checkpoint can run in a separate rescue home. The original development configuration and broken source stay available for repair. Return to development requires independent validation and actual startup checks. Persistent sessions, storage, credentials, and databases are not rolled back. Process switches can interrupt active work, and external agent actions are never replayed automatically.

## Boundaries

- One registered `web` profile and its configured listener are managed. Unknown listeners are neither adopted nor terminated.
- Candidate validation requires native macOS containment, including positive and negative checks. Scratch validation does not establish every external model, MCP, IM, or browser integration's business behavior.
- Optional macOS Claude repair is disabled by default. It accepts only schema-constrained proposals within enrolled policy and incident budgets; it cannot edit arbitrary source or execute tools. Windows rejects repair at the execution boundary.
- The stable launcher verifies installed Guard versions and bounds heartbeat recovery. It cannot repair its own bootstrap, a missing interpreter, or the operating system. Same-account processes are outside the isolation guarantee.
- User login is required for LaunchAgent operation; sleep and shutdown interrupt availability. Cross-version DSH data migration is unsupported.

Read [implementation details](IMPLEMENTATION.md), [contribution instructions](CONTRIBUTING.md), and the [security policy](SECURITY.md).

## License

This project's original code is available under the [MIT License](LICENSE). DSH, Node.js, plugins, and other upstream software retain their own licenses.

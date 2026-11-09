# dsh-guard Host bridge

[简体中文](README.zh.md)

`@local/dsh-guard-bridge` is an optional filesystem bundle for DSH **0.2.2-rc.1**. It gives the external [dsh-guard supervisor](../README.md) a bounded view of Host activity and capability state. It is part of an independent community project, with no official DeepSeek affiliation.

The bridge observes native plugin operations and HMR; it does not install plugins, publish services, replace HMR, or repair source. Its composition row needs no `isolate` realm because it publishes no service. The package is private and intended for local native installation, not a claimed registry release.

## Install and opt into development mode

Use Python 3.11+ and Node.js **24.9.0 or later** for the supported DSH runtime. Install and enable the local bundle through DSH's native Plugin Manager or native plugin CLI, for example:

```sh
dsh plugin --profile web add 'link:/absolute/path/to/dsh-guard/bridge'
dsh --profile web --dump-config
```

Run these against the intended DSH home. Native composition preparation may write profile files; use a disposable home when evaluating an unfamiliar installation. Keep profile `cordis.yml` as `[]`, with user edits in `cordis.patch.yml`. Confirm the bundle is selected and its row appears in the composed Host tree. A local link does not enable source watching; native HMR roots are a separate opt-in.

Then use the [macOS setup guide](../docs/macos.md) to enroll with `macos_setup.py --development`, explicitly setting `development_mode` in Guard configuration. The installer requires the bridge to be selected already and still generates only an observation-mode installation. Preparing and validating a baseline, starting the supervisor, and handing over management remain separate steps. Installing this bundle alone does not enable recovery or transfer ownership of a running DSH process.

Guard supplies both `DSH_GUARD_BRIDGE_FILE` and `DSH_GUARD_EPOCH` when launching its managed development process. Without a valid absolute destination and a nonempty epoch the bridge is inert. A process started before that launch environment exists does not become owned simply because the bundle was installed.

After handover, require a fresh idle bridge and ready/disabled capabilities, then create and inspect the first checkpoint:

```sh
dsh-guard checkpoint
dsh-guard status
```

A successful core readiness probe is insufficient if the bridge or a reported capability is missing, waiting, or failed. The Windows supervision preview does not install this bridge or enable the checkpoint workflow; Linux and WSL are unsupported.

## Observation contract

The observer emits schema-1 JSON about every two seconds, using a private same-directory temporary file and atomic rename. It projects owned scalar records rather than serializing native runtime objects or diagnostic bodies.

| Field | Meaning |
|---|---|
| `pid`, `epoch`, `updated_at` | Bind the report to the Guard-launched process and report freshness |
| `phase` | `busy`, `idle`, or `error` from observed operations and capability state |
| `generation` | New observer activation plus a reconcile/reload event sequence |
| `operation` | Bounded latest/active operation summary, with uncertainty retained |
| `capabilities` | Loader rows, patch syntax, and bundle inventory with ready/disabled/waiting/failed status and a core-required marker |
| `restart_intent` | Always `null`; no reliable native planned-restart notification is inferred |

Native plugin-manager operation events cover manager work and lock waits. Root include reconciliation and HMR events have narrower coverage: a resolved continuation is not proof of transaction completion. Generation is **not a native commit identifier**, and the bridge cannot provide a snapshot before every write. Use the Guard restart command or console for a planned restart.

Native continuation failures propagate unchanged. Observer failures remain contained and produce error or stale status. Hung bundle inventory reads become waiting without stopping heartbeat publication. Teardown waits for an in-flight write and retains the last report, allowing Guard to detect staleness. The supervisor also checks process identity, epoch, a freshness limit, native writers, stable file fingerprints, and independent candidate validation before accepting a checkpoint.

## Tests

Dependency-free observer checks use Node's built-in runner:

```sh
node --test bridge/test-bridge.mjs
```

From the repository root, an optional existing-installation contract check is:

```sh
DSH_GUARD_TEST_INSTALL_ANCHOR=/absolute/dsh-runtime/node_modules/@deepseek-ai/dsh/package.json \
  node --expose-internals --test bridge/test-bridge.mjs
```

Without that environment variable, the native contract is skipped. It mounts a temporary profile without package installation, external endpoints, or production service control. A pass establishes the tested native bundle/parser/event contract, not all DSH business integrations. Prior acceptance and remaining limitations are recorded in [ACCEPTANCE.md](../ACCEPTANCE.md).

The bridge's original code uses the project's [MIT License](../LICENSE); upstream dependencies retain their own licenses. See the [security policy](../SECURITY.md) before sharing diagnostics.

# macOS installation

This guide targets DSH **0.2.2-rc.1**, native Node.js **24.9.0+**, and Python **3.11+**. Use a persistent DSH runtime, an already prepared native `web` profile, and local writable storage. The installer refuses an existing application/state directory, command or LaunchAgent; it is a first-install tool, not an upgrade or migration tool.

## Prepare the native environment

Choose a reviewed checkout of this project and a persistent runtime directory. If a persistent runtime is needed, install the exact DSH version there using your normal package manager, for example:

```sh
mkdir -p "$HOME/dsh-runtime"
npm install --prefix "$HOME/dsh-runtime" --save-exact @deepseek-ai/dsh@0.2.2-rc.1
```

Use the selected Node binary to prepare your DSH `web` profile through the normal DSH workflow before enrolling it. Configure providers, credentials and any native plugins there. Do not install over shipped preset files. Keep `profiles/web/cordis.yml` as `[]`; authored changes belong in `cordis.patch.yml`.

For native development collaboration, first install the optional Host bridge through native plugin management. From this project checkout:

```sh
export DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
node "$HOME/dsh-runtime/node_modules/@deepseek-ai/dsh/lib/bin.js" \
  plugin --profile web add "link:$PWD/bridge"
```

Keep that reviewed bridge directory available. The native bundle contributes a read-only Host observer; it does not belong in an agent preset. Use a disposable home to check a new composition with native `--dump-config`; Guard also runs this check in its contained scratch home when validating a baseline. A live profile can contain optional integrations requiring credentials which will not be available in candidate isolation; validation can block until its scope is explicitly reviewed.

## Generate an independent install

From the project checkout:

```sh
python3 macos_setup.py \
  --runtime "$HOME/dsh-runtime" \
  --node "$(command -v node)" \
  --development
```

`--home` overrides the first-enrollment `DSH_HOME` default. See [environment behavior](environment.md) for XDG paths, pnpm, proxies, trust settings, and background launch differences. The installer prints absolute application, state, command and plist paths. Substitute those printed paths below when using custom directories.

The default command is `~/.local/bin/dsh-guard`; add that directory to your interactive PATH if needed. The plist is `~/Library/LaunchAgents/org.dsh-guard.supervisor.plist`. The job and command pin the installing Python and the registered state directory. The installer copies a hash-verified app outside the checkout, generates startup files, and writes observation-only configuration. It does not load launchd, prepare a runtime snapshot, stop an application or take over an existing listener.

Generate the native baseline using the installed command:

```sh
"$HOME/.local/bin/dsh-guard" prepare \
  --runtime "$HOME/dsh-runtime" --node "$(command -v node)" --id initial
"$HOME/.local/bin/dsh-guard" validate initial
"$HOME/.local/bin/dsh-guard" status
```

Preparation freezes runtime and declared plugin artifacts without replacing live dependencies. Local `link:`/`file:` packages need an explicit `package.json.files` artifact list that Guard can freeze. A failed capture or validation is retained as unvalidated and cannot be used for management. Repeat with a fresh release ID after resolving the cause; a prepared release directory is never overwritten. Initial development activation also checks that the working profile still matches the validated capture.

Validation executes `--dump-config`, containment positive/negative probes and a complete candidate boot in a new scratch home. It does not link production sessions, storage or credentials. Do not bypass a failed isolation or capability check. The optional `validation_mcp_rows` enrollment may exclude explicitly reviewed native external MCP rows in the validation overlay only; the manifest records those exclusions, and live readiness still includes them. There is no default exclusion list.

Without `--development`, enrollment uses the older fixed publication workflow. Its first managed publication can replace enrolled profile files/dependencies with the validated snapshot. Use development mode to preserve native mutable dependencies and local source links.

## Start in observation mode

Inspect the generated plist, including its `EnvironmentVariables`, then load only that job:

```sh
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/org.dsh-guard.supervisor.plist"
"$HOME/.local/bin/dsh-guard" status
```

A running service on the configured port remains unowned and untouched. The independent console is normally at <http://127.0.0.1:3081>. An observing instance does not supply an authenticated entry into someone else's DSH process.

After the baseline validates, exit an existing DSH through its original terminal or supervisor and verify that its port is free. Then explicitly enable management:

```sh
"$HOME/.local/bin/dsh-guard" resume --manage
"$HOME/.local/bin/dsh-guard" status
```

The launcher runs the installed app; the installed app starts DSH with its fixed Node path and hash. Development mode uses your original profile and local links. Authentication and Host readiness must stabilize before a checkpoint is accepted; use the console to distinguish a running service from an accepted recovery point. Do not infer a known-good checkpoint from an unvalidated preparation or a successful CLI submission alone.

## Daily controls

```sh
dsh-guard status
dsh-guard restart
dsh-guard checkpoint
dsh-guard stop
dsh-guard resume --manage
dsh-guard return-development
```

Use native Plugin Manager, configuration HMR and explicit native source HMR during normal development. A local link alone does not enable source watching. Use Guard's planned restart command when you intend a full process restart. Optional capability failures block checkpoints and normally report degradation rather than restart the whole process.

`stop` persists across launcher/guard restarts and leaves the console available. Graceful stop timeout reports a block; it does not escalate to a production SIGKILL. In rescue mode, the original development workspace stays in place and current persistent data is shared, never rolled back. Return to development requires separate validation and startup checks.

## Optional Claude repair

New installs have `repair_enabled: false`, no optional bundle allowlist, and no editable YAML fields. Register a reviewed repair scope in the private guard configuration before a real `check-claude --enable-repair` invocation. That check calls the real Claude CLI and can incur costs. No automatic repair is needed for ordinary process supervision or recovery-point operation. See [environment and authentication](environment.md) and [security scope](../SECURITY.md).

## Upgrade and removal

Source edits do not upgrade the running guard. `launcher.py install --source <reviewed-checkout> --store <installed-base>/app` stages an explicit application version; selection takes effect at the next launcher start. Changing the standalone bootstrap is a separate reviewed update. Check application-manifest compatibility before updating it; do not rerun the first-install tool over existing state.

To remove background supervision, first use `dsh-guard stop` and confirm the owned DSH process has exited. Then unload the exact generated job:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/org.dsh-guard.supervisor.plist"
```

Keep private state, diagnostic records and release artifacts until you have decided what to archive. Removing supervision does not remove the native Host bridge or your DSH home. launchd operates only while the user is logged in; machine sleep, interpreter damage, standalone bootstrap damage and same-account malicious writes remain outside guard-version recovery.

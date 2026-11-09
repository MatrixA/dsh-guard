# Environment and enrollment

Paths are resolved during enrollment and stored as absolute paths. The installed command and startup job use that enrollment even if a later terminal exports a different `DSH_HOME` or XDG directory. Separate foreground instances require separate installations, state directories and ports. The macOS installer generates one fixed launchd label per user; custom paths do not register a second background service.

## Path discovery

| Input | First enrollment | After enrollment |
|---|---|---|
| DSH home | Explicit `--home` / `-DshHome`, then `DSH_HOME`, then the user home plus `.dsh` | `config.json.home`; DSH children receive that value as `DSH_HOME` |
| macOS application directory | `--base`, otherwise `$XDG_DATA_HOME/dsh-guard`, default `~/.local/share/dsh-guard` | Installed command location and absolute startup arguments |
| macOS state directory | `--state`, otherwise `$XDG_STATE_HOME/dsh-guard`, default `~/.local/state/dsh-guard` | Absolute path in installation metadata and startup arguments |
| Windows installation | `-Base`, otherwise `%LOCALAPPDATA%\dsh-guard` | Installed command location; state under `state` |
| Node | Explicit binary or macOS `PATH` discovery | Absolute path and SHA-256 in each release; DSH 0.2.2-rc.1 requires Node >=24.9.0 |
| DSH runtime | Explicit persistent runtime directory containing `node_modules/@deepseek-ai/dsh` | Prepared release; Windows preview records the original entry path and hash |
| Python | Interpreter running the installer | Absolute executable in the generated command/startup job; Python 3.11+ |
| Claude CLI | macOS installer discovers `claude` through `PATH` | Recorded absolute executable; repair stays disabled until explicitly checked and enabled |
| Claude settings | macOS installer uses `CLAUDE_CONFIG_DIR/settings.json`, default `~/.claude/settings.json` | Recorded settings path, read afresh for each repair call |

Relative XDG base directories are ignored. `prepare --home` can specify the same enrolled home; it refuses to switch an existing enrollment to another home. The installed command refuses a `--state` override. Direct source commands are maintenance interfaces and accept explicit state paths.

The installer does not install Node, Python, pnpm, DSH or Claude. Native DSH plugin management launches `pnpm` from its configured command or `PATH`. Keep the actual pnpm executable/shim directory on the startup `PATH`; `PNPM_HOME` alone does not add it to `PATH`. The macOS installer captures absolute PATH entries, prepends the selected Node/Claude directories and an absolute `PNPM_HOME`, and writes no shell startup commands.

## Process environment

Both launcher → guard and guard → DSH preserve these selected groups:

- User/process basics: `HOME`, `USER`, `LOGNAME`, `PATH`, `TMPDIR`, `LANG`, `LC_ALL`, `TZ`, `SHELL`.
- Proxy settings: `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` and all four lowercase forms. DSH's native proxy policy decides precedence and supported schemes. This DSH version supports HTTP/HTTPS proxies and rejects SOCKS proxy URLs.
- Trust configuration: `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `SSL_CERT_DIR`. Use absolute paths. `NODE_EXTRA_CA_CERTS` is needed before Node starts; SSL variables depend on the selected runtime/tools.
- Package and directory configuration: `PNPM_HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`.
- The DSH provider endpoint override `DEEPSEEK_BASE_URL`.

Windows additionally preserves its system, user profile, app-data, temp, command-interpreter and executable-extension variables through a case-insensitive lookup. Guard alone receives fixed Python isolation settings. Guard-created bridge paths and epochs are set for the owned DSH process, and cannot be supplied by a parent agent.

`NODE_OPTIONS`, `NODE_PATH`, `PYTHONPATH`, parent agent/session identifiers, permission overrides, arbitrary `npm_config_*` variables and ambient model credentials are not forwarded by the launcher. Disabling TLS verification is not an accepted substitute for configuring trust. This is an intentional supported environment contract: arbitrary environment references in YAML, pnpm configuration or custom plugins may need a native credential/configuration source instead.

## Terminal and background differences

launchd and Task Scheduler do not run your interactive shell configuration. Exporting a value in a terminal does not set it in an already running background job. The generated macOS plist captures the executable search path, HOME and locale; it does **not** copy proxy URLs, authentication, CA settings or the entire terminal environment into the plist.

For a foreground run, export the required transport variables before invoking the installed launcher. For background service, configure nonsecret transport/CA values in the generated startup job environment before loading it, or arrange for its service manager to supply them. On macOS, edit the generated plist's `EnvironmentVariables` dictionary and reload that specific job; on Windows, set the corresponding user environment and sign in again before starting the task. Keep secrets out of launch arguments and public files. DSH's supported home `.env`/credential service can supply normal provider and MCP credentials; permission/bootstrap names rejected by DSH's `.env` policy still need the launch environment. In particular, DSH does not accept `NODE_EXTRA_CA_CERTS` or `DEEPSEEK_BASE_URL` from its home `.env`.

Proxy URLs can contain credentials. Values are passed without being printed, and are not included in status or package manifests. Share diagnostic codes and redacted summaries, not raw environments or application logs.

## Claude authentication and routing

Automatic repair is optional and disabled on a new macOS install; Windows rejects the execution entry point. The restricted CLI receives a small authentication/routing/model allowlist from the registered existing settings file, with `settings.env` taking precedence. That overlay also permits the proxy and CA settings above. Hooks, permissions, arbitrary environment keys and custom tools from that file are not loaded.

The normal launcher deliberately strips ambient model credentials, including a terminal-only `ANTHROPIC_API_KEY`. Use an existing supported Claude login or the registered Claude settings source; do not copy credentials into guard configuration or startup files. A `check-claude` call uses a real model and can incur costs. Its tool, turn, duration and cost controls remain in force; missing CLI features or authentication blocks repair.

## Basis

These choices were checked against the installed DSH 0.2.2-rc.1 modules `dsh-home-paths` (home precedence), `dsh-app-boot` (layered environment and bootstrap exclusions), `dsh-http-proxy` (casing, proxy policy), `dsh-plugin-manager` (pnpm spawn), `dsh-subprocess` (native credential scrubbing), and the package's Node engine declaration. They are version-specific, not a promise about future DSH releases. The runnable environment and installation checks are in `test_publication.py`.

# Security policy

This is an independent community project for DeepSeek Harness. Its full recovery implementation targets macOS and DSH 0.2.2-rc.1. Native Windows is an untested supervision preview with recovery and Claude repair disabled. Linux and WSL are unsupported. See [ACCEPTANCE.md](ACCEPTANCE.md) for verification limits.

## Reporting a vulnerability

If the repository enables private vulnerability reporting, use its private “Report a vulnerability” channel. Do not open a public issue containing exploit details, credentials, authentication URLs, private configuration, session content, or raw logs.

If no private channel is available, open only a minimal public request asking the maintainers to arrange a private reporting route. Do not include reproduction details or sensitive attachments in that request. This document does not specify an email address or guarantee a response time.

Once a private route is established, provide the affected revision, OS/architecture, Python/Node/DSH versions, expected and observed behavior, and a minimal reproduction using synthetic files and disposable processes. Include only reviewed, relevant excerpts. Guard redaction may miss secrets; inspect all material before sharing. Keep full incident directories and browser profiles private. If credentials were exposed, revoke or rotate them through their provider rather than posting them for diagnosis.

## Trust and isolation boundaries

Guard runs as the current user and has authority over the explicitly registered DSH profile, its identified process, and enrolled recovery files. Keep the console on loopback. It uses origin checks and private control tokens, but it is not designed as an internet-facing multi-user service. Local authenticated entry to DSH is sensitive even though its redirect URL is excluded from console HTML and status JSON.

Process identity and listener ownership must both be established before control. An inspection failure is not authority to adopt or terminate a process. Persistent manual stop and graceful-stop limits are intentional safeguards.

Candidate isolation is a separate macOS sandbox boundary. Validation must prove both allowed actions and denied access before accepting a candidate. Scratch validation does not authorize production credentials or external business calls. Explicit scratch MCP exclusions narrow what was tested and must remain visible in the release manifest. Windows ACLs, Jobs, and temporary directories are not an implemented equivalent; Windows recovery entry points reject execution.

Checkpoints recover enrolled configuration and code artifacts. They do not recover historical sessions, storage, credentials, or databases, and they do not make third-party plugin code trustworthy. DSH and its installed plugins still execute with their own privileges and dependencies. Cross-version data migration requires a separate design.

## Repair and installed artifacts

Model repair is optional and disabled by default. The generic installer enrolls no optional bundle or YAML scalar edits. A proposal must remain within explicitly reviewed scope, deterministic validation, incident limits, and candidate containment. Do not work around a failed policy, missing usage report, or unavailable sandbox by broadening tools, permissions, or credentials.

The launcher checks installed artifact membership and hashes and may fall back only to a verifiable installed version. SHA-256 manifests detect drift; they are not publisher signatures. The stable bootstrap, interpreter, native operating system, and selected runtime remain trusted dependencies. The launcher does not defend against a process with full access to the same user account or an administrator.

Keep installation/state directories private and use persistent runtime paths. Configuration, incident data, application logs, and local authentication state can contain sensitive information even when the public console looks sanitized. Public source packages should contain only reviewed source, documentation, and synthetic test fixtures, subject to each upstream dependency's license.

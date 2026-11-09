import { randomUUID } from 'node:crypto';
import { open, rename, unlink } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

// Verified against installed 0.2.2-rc.1 types AND lib implementations:
// cordis/lib/types/fiber.d.ts:67; cordis-plugin-loader/lib/types/config/{entry,tree}.d.ts;
// dsh-plugin-manager/lib/types/types.d.ts:351 and lib/index.js:3142;
// dsh-app-boot/lib/types/index.d.ts:30 and lib/index.js:4618,4886;
// dsh-hmr/lib/types/index.d.ts:10 and lib/index.js:1151.
// Native startup audit policy, not inferred service names. Disabled rows are exempt.
const REQUIRED = new Set([
  'agent-loop', 'tool-executor-native', 'tool-executor-ptc', 'webserver',
  'web-runtime', 'modules', 'connection', 'headless-runner', 'acp', 'sdk-jsonrpc-server',
]);
const APPLICATIONS = new Set(['applied', 'restart-required', 'overridden', 'failed', 'cancelled']);
const REASONS = new Set(['plugin', 'bundle', 'install', 'remove']);
const now = () => Date.now() / 1000;
const text = value => typeof value === 'string' && value.length > 0 && value.length <= 1024;

// Only this owned JSON reaches stringify. Same-directory rename is atomic; the
// guard owns the destination directory and supplies a new epoch on every launch.
async function publish(file, document, closed) {
  const temporary = `${file}.${process.pid}.${randomUUID()}.tmp`;
  const handle = await open(temporary, 'wx', 0o600);
  try {
    try { await handle.writeFile(`${JSON.stringify(document)}\n`); }
    finally { await handle.close(); }
    if (!closed()) await rename(temporary, file);
  } finally {
    await unlink(temporary).catch(() => {});
  }
}

// Exported for a dependency-free node:test check. Production enters through apply.
// patchReader resolves to the installed first-party loadOptionalPatches function.
export function startObserver(ctx, { file, epoch, patchReader }) {
  let closed = false;
  let writer;
  let observerFailed = false;
  let generation = 0;
  const activation = randomUUID();
  const active = new Map(); // Contains owned scalar records only, never event payloads.
  let last = null;
  let patches;
  let patchStatus = 'waiting';
  let bundleStatus = 'waiting';
  let bundles = [];
  let bundleRead;
  let bundleReadStarted = 0;
  const disposers = [];
  const guard = callback => (...args) => {
    try { return callback(...args); } catch { observerFailed = true; }
  };
  const listen = (event, callback) => disposers.push(ctx.on(event, guard(callback), { global: true }));
  const begin = (id, kind, time) => {
    // ponytail: cap outstanding diagnostics; a lost completion blocks health
    // until remount, rather than evicting work and claiming the Host is idle.
    if (active.size >= 128) { observerFailed = true; return; }
    active.set(id, { kind, status: 'started', started_at: time, finished_at: null });
  };
  const finish = (id, status, time, kind) => {
    const started = active.get(id);
    active.delete(id);
    last = { kind: started?.kind ?? kind, status, started_at: started?.started_at ?? null, finished_at: time };
  };

  // Public diagnostics cover manager invocations, including lock waits. Do not
  // use install-log exit codes, /changed, or restart-required as restart intent.
  try {
  listen('plugin-manager/operation', operation => {
    const id = operation.id;
    const reason = operation.reason;
    const phase = operation.phase;
    const time = Date.parse(operation.time) / 1000;
    if (!text(id) || !REASONS.has(reason) || !Number.isFinite(time)) {
      observerFailed = true;
      return;
    }
    if (phase === 'started') begin(`manager:${id}`, reason, time);
    else if (phase === 'settled' || phase === 'unknown') {
      const status = phase === 'settled' && APPLICATIONS.has(operation.application)
        ? operation.application : 'unknown';
      finish(`manager:${id}`, status, time, reason);
    } else observerFailed = true;
  });

  // This waterfall wraps the root Include UPDATE, not the whole HMR transaction.
  // Read/prepare failures before it and final activation judgement after it are
  // outside its scope. Its completion is UNKNOWN, even when next() resolves.
  disposers.push(ctx.on('loader/patch-context', (entry, next) => {
    let id;
    try {
      if (entry.id === 'include' && entry.options.name === 'cordis:include') {
        id = `reconcile:${++generation}`;
        begin(id, 'profile-reconcile', now());
      }
    } catch { observerFailed = true; }
    const settle = status => {
      try { if (!closed && id) finish(id, status, now(), 'profile-reconcile'); }
      catch { observerFailed = true; }
    };
    // Preserve the native continuation and its rejection; never swallow a
    // native lifecycle failure. All observer-owned exceptions stay contained.
    try {
      return Promise.resolve(next()).then(value => {
        settle('unknown');
        return value;
      }, error => { settle('failed'); throw error; });
    } catch (error) { settle('failed'); throw error; }
  }, { global: true }));
  listen('app-boot/config-reload', () => { generation++; });
  listen('hmr/reload', () => {
    generation++;
    last = { kind: 'source-reload', status: 'unknown', started_at: null, finished_at: now() };
  });
  } catch {
    observerFailed = true;
    for (const dispose of disposers.splice(0).reverse()) { try { dispose(); } catch {} }
  }

  // Do not call loadProfileDirectory/readProfilePatches: the former may WRITE
  // retired bundle selections. This parser only reads the user/home patch files.
  Promise.resolve(patchReader).then(reader => {
    if (closed) return;
    if (typeof reader === 'function') patches = reader;
    else patchStatus = 'failed';
  }).catch(() => { if (!closed) patchStatus = 'failed'; });

  function refreshBundles() {
    if (closed) return;
    if (bundleRead) {
      if (Date.now() - bundleReadStarted >= 2000) bundleStatus = 'waiting';
      return;
    }
    const manager = ctx.get('pluginManager');
    if (manager === undefined) { bundleStatus = 'waiting'; bundles = []; return; }
    bundleReadStarted = Date.now();
    bundleRead = Promise.resolve().then(() => manager.listBundles()).then(list => {
      if (closed) return;
      const extracted = [];
      for (const bundle of list) {
        const name = bundle.name;
        if (!text(name)) throw new Error('Invalid bundle identity');
        const enabled = bundle.enabled;
        const rows = [];
        for (const row of bundle.rows) rows.push(text(row.entryId) ? row.entryId : null);
        extracted.push({ id: `bundle:${name}`, enabled: enabled === true,
          failed: bundle.error !== undefined || bundle.availability === 'missing', rows });
      }
      bundles = extracted;
      bundleStatus = 'ready';
    }).catch(() => { if (!closed) bundleStatus = 'failed'; })
      .finally(() => { bundleRead = undefined; });
  }

  function snapshot() {
    const capabilities = [];
    const add = (id, status, required = false) => capabilities.push({ id, status, required });
    const loader = ctx.get('loader');
    let loading = false;
    if (loader === undefined) add('bridge:loader', 'waiting', true);
    else {
      loading = loader.getTasks().length > 0;
      add('bridge:loader', loading ? 'waiting' : 'ready', true);
      for (const entry of loader.entries()) {
        const id = entry.id;
        if (!text(id)) throw new Error('Invalid Loader identity');
        try {
          const disabled = entry.disabled;
          const state = entry.fiber?.state;
          // No fiber after Loader settles includes import failures. Never await
          // a fiber here: a hung apply/disposer must not stop the heartbeat.
          const status = disabled ? 'disabled' : state === 2 ? 'ready'
            : state === 3 ? 'failed' : state === undefined || state === 4
              ? loading ? 'waiting' : 'failed' : 'waiting';
          add(id, status, !disabled && (id === 'include' || REQUIRED.has(entry.options.id)));
          if (!disabled && (state === 1 || state === 5)) loading = true;
        } catch { add(id, 'failed'); }
      }
    }
    if (patches) {
      try {
        const profile = ctx.get('profileContext');
        if (profile === undefined) patchStatus = 'waiting';
        else {
          patches('dsh-guard-bridge', profile.patchPath);
          patches('dsh-guard-bridge', join(profile.home, 'cordis.patch.yml'));
          patchStatus = 'ready';
        }
      } catch { patchStatus = 'failed'; }
    }
    add('bridge:patch-syntax', patchStatus);
    add('bridge:bundle-inventory', bundleStatus);
    const rows = new Map(capabilities.map(capability => [capability.id, capability.status]));
    for (const bundle of bundles) {
      const states = bundle.rows.map(id => rows.get(id) ?? 'waiting');
      add(bundle.id, !bundle.enabled ? 'disabled' : bundle.failed || states.includes('failed')
        ? 'failed' : states.includes('waiting') ? 'waiting' : 'ready');
    }
    if (observerFailed) add('bridge:observer', 'failed');
    const operation = [...active.values()].at(-1) ?? last;
    const error = observerFailed || capabilities.some(capability => capability.status === 'failed')
      || operation?.status === 'failed'
      || (operation?.status === 'unknown' && REASONS.has(operation.kind));
    return {
      schema: 1, pid: process.pid, epoch, updated_at: now(),
      phase: active.size || loading ? 'busy' : error ? 'error' : 'idle',
      // Observer activation + reconcile/reload sequence, NOT a native commit id.
      // A successful event carries neither a generation id nor pending modules.
      generation: `${activation}:${generation}`, operation, capabilities,
      // CLI has no cooperative request event. loader.exit() is a default noop;
      // disposal, signal delivery and exit codes cannot establish user intent.
      restart_intent: null,
    };
  }

  function pulse() {
    if (closed || writer) return;
    let document;
    try {
      refreshBundles();
      document = snapshot();
    } catch {
      observerFailed = true;
      document = {
        schema: 1, pid: process.pid, epoch, updated_at: now(), phase: 'error',
        generation: `${activation}:${generation}`, operation: null,
        capabilities: [{ id: 'bridge:observer', status: 'failed', required: false }],
        restart_intent: null,
      };
    }
    writer = publish(file, document, () => closed).catch(() => {
      // A failed publication leaves the last file stale for the external guard.
      observerFailed = true;
    }).finally(() => { writer = undefined; });
  }
  const timer = setInterval(pulse, 2000);
  timer.unref();
  pulse();
  return async () => {
    closed = true;
    clearInterval(timer);
    for (const dispose of disposers.reverse()) { try { dispose(); } catch {} }
    // Serialize teardown with our one in-flight atomic write. No late rename
    // from this activation can overwrite a newly mounted observer's heartbeat.
    await writer?.catch(() => {});
    // Do not unlink the last heartbeat: staleness is evidence, and a successor
    // may already own the external path. Bundle readers never publish after close.
  };
}

export function apply(ctx) {
  const file = process.env.DSH_GUARD_BRIDGE_FILE;
  const epoch = process.env.DSH_GUARD_EPOCH;
  if (!text(file) || !isAbsolute(file) || !text(epoch)) return;
  // Normal filesystem Host module, no VM, service provision or registry patch.
  ctx.effect(() => startObserver(ctx, { file, epoch,
    patchReader: Promise.resolve().then(async () => {
      // A linked bundle's own ancestors do not contain the Host packages.
      // Resolve from the launcher's native installation anchor (also frozen
      // rescue runtimes), without installing or changing a dependency.
      const anchor = ctx.get('profileContext')?.installAnchor;
      if (!text(anchor) || !isAbsolute(anchor)) throw new Error('Missing installation anchor');
      const entry = createRequire(anchor).resolve('@deepseek-ai/dsh-app-boot');
      return (await import(pathToFileURL(entry).href)).loadOptionalPatches;
    }),
  }));
}

export default { name: 'dsh-guard-bridge', apply };

import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, symlink, readFile, readdir, rm, stat } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { apply, startObserver } from './index.mjs';

const hostile = object => Object.assign(object, { toJSON() { throw new Error('Runtime object serialized'); } });

function host() {
  const listeners = new Map();
  const row = (id, state, disabled = false) => hostile({
    id: `include:${id}`, options: hostile({ id }), disabled,
    fiber: state === null ? undefined : hostile({ state }),
  });
  const rows = [row('agent-loop', 2), row('optional', 2), row('disabled', null, true)];
  let tasks = [];
  let bundleList = [hostile({ name: 'example', enabled: true, availability: 'profile',
    rows: [hostile({ entryId: 'include:optional' })] })];
  const ctx = hostile({
    get(name) {
      if (name === 'loader') return hostile({ entries: () => rows.values(), getTasks: () => tasks });
      if (name === 'profileContext') return hostile({ patchPath: '/fake/profile/cordis.patch.yml', home: '/fake/home' });
      if (name === 'pluginManager') return hostile({ listBundles: () => Promise.resolve(bundleList) });
      throw new Error(`Unexpected service: ${name}`);
    },
    on(event, callback) {
      const hooks = listeners.get(event) ?? new Set();
      hooks.add(callback);
      listeners.set(event, hooks);
      return () => hooks.delete(callback);
    },
  });
  return { ctx, rows, listeners, row,
    emit(event, ...args) { return [...(listeners.get(event) ?? [])].map(callback => callback(...args)); },
    tasks(value) { tasks = value; }, bundles(value) { bundleList = value; },
  };
}

async function until(file, predicate) {
  const deadline = Date.now() + 6500;
  while (Date.now() < deadline) {
    try {
      // A parse error is a test failure: publication must never expose partial JSON.
      const document = JSON.parse(await readFile(file, 'utf8'));
      if (predicate(document)) return document;
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    await delay(15);
  }
  assert.fail('Heartbeat did not reach the expected state');
}
const status = (document, id) => document.capabilities.find(capability => capability.id === id)?.status;
const operation = (id, phase, application) => hostile({
  id, phase, application, reason: 'install', time: new Date().toISOString(),
  get output() { throw new Error('Diagnostic body read'); },
});

test('filesystem bundle is explicit and absent/invalid guard environment is inert', async () => {
  const manifest = JSON.parse(await readFile(new URL('./package.json', import.meta.url), 'utf8'));
  assert.deepEqual(manifest.files, ['index.mjs', 'cordis.patch.yml']);
  assert.equal(manifest.dsh.bundle.patch, './cordis.patch.yml');
  const patch = await readFile(new URL('./cordis.patch.yml', import.meta.url), 'utf8');
  assert.match(patch, /name: '@local\/dsh-guard-bridge'/);
  assert.doesNotMatch(patch, /\bisolate:|!!js/);
  const previous = [process.env.DSH_GUARD_BRIDGE_FILE, process.env.DSH_GUARD_EPOCH];
  const ctx = new Proxy({}, { get() { assert.fail('Inert plugin touched its Host'); } });
  try {
    for (const [file, epoch] of [[undefined, undefined], ['/tmp/unused', undefined],
      [undefined, 'epoch'], ['relative.json', 'epoch'], ['/tmp/unused', '']]) {
      if (file === undefined) delete process.env.DSH_GUARD_BRIDGE_FILE;
      else process.env.DSH_GUARD_BRIDGE_FILE = file;
      if (epoch === undefined) delete process.env.DSH_GUARD_EPOCH;
      else process.env.DSH_GUARD_EPOCH = epoch;
      apply(ctx);
    }
  } finally {
    for (const [index, key] of ['DSH_GUARD_BRIDGE_FILE', 'DSH_GUARD_EPOCH'].entries()) {
      if (previous[index] === undefined) delete process.env[key];
      else process.env[key] = previous[index];
    }
  }
});

test('atomic heartbeat projects scalars and preserves native operation uncertainty', { timeout: 45000 }, async t => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-guard-bridge-'));
  const file = join(directory, 'heartbeat.json');
  const h = host();
  let invalidPatch = false;
  const paths = new Set();
  const dispose = startObserver(h.ctx, { file, epoch: 'guard-owned-epoch',
    patchReader: Promise.resolve((_name, path) => {
      paths.add(path);
      if (invalidPatch) throw new Error('Malformed saved YAML');
      return hostile([]);
    }),
  });
  t.after(async () => { await dispose(); await rm(directory, { recursive: true, force: true }); });
  let document = await until(file, d => d.phase === 'idle' && status(d, 'bundle:example') === 'ready');
  assert.deepEqual(Object.keys(document).sort(), ['schema', 'pid', 'epoch', 'updated_at', 'phase',
    'generation', 'operation', 'capabilities', 'restart_intent'].sort());
  assert.equal(document.schema, 1);
  assert.equal(document.pid, process.pid);
  assert.equal(document.epoch, 'guard-owned-epoch');
  assert.equal(document.operation, null);
  assert.equal(document.restart_intent, null);
  assert.equal(status(document, 'include:disabled'), 'disabled');
  assert.equal(status(document, 'bridge:bundle-inventory'), 'ready');
  assert.equal(status(document, 'bridge:patch-syntax'), 'ready');
  assert.equal(document.capabilities.find(c => c.id === 'include:agent-loop').required, true);
  assert.equal(document.capabilities.find(c => c.id === 'include:optional').required, false);
  assert.equal((await stat(file)).mode & 0o777, 0o600);
  assert.equal(paths.size, 2);
  const initialTime = document.updated_at;
  document = await until(file, d => d.updated_at > initialTime);
  assert(document.updated_at >= initialTime + 1.5, 'Timer emits on the Host event loop');

  h.emit('plugin-manager/operation', operation('one', 'started'));
  h.emit('plugin-manager/operation', operation('two', 'started'));
  document = await until(file, d => d.phase === 'busy');
  assert.equal(document.operation.status, 'started');
  const bothStarted = document.updated_at;
  h.emit('plugin-manager/operation', operation('two', 'settled', 'applied'));
  h.emit('plugin-manager/changed', hostile({ reason: 'install' }));
  document = await until(file, d => d.updated_at > bothStarted && d.phase === 'busy' && d.operation.started_at !== null);
  h.emit('plugin-manager/operation', operation('one', 'settled', 'restart-required'));
  document = await until(file, d => d.operation?.status === 'restart-required');
  assert.equal(document.phase, 'idle');
  assert.equal(document.restart_intent, null);

  const generation = document.generation;
  let release;
  const root = hostile({ id: 'include', options: hostile({ name: 'cordis:include' }) });
  const pending = h.emit('loader/patch-context', root, () => new Promise(resolve => { release = resolve; }));
  document = await until(file, d => d.phase === 'busy' && d.operation.kind === 'profile-reconcile');
  assert.notEqual(document.generation, generation);
  release();
  await Promise.all(pending);
  h.emit('app-boot/config-reload');
  document = await until(file, d => d.operation?.status === 'unknown');
  assert.equal(document.phase, 'idle', 'Settled HMR uncertainty is informational; separate validation establishes apply success');
  h.emit('plugin-manager/operation', operation('unknown-manager', 'unknown'));
  document = await until(file, d => d.operation?.kind === 'install' && d.operation.status === 'unknown');
  assert.equal(document.phase, 'error', 'Manager unknown is an explicit uncertain operation outcome');
  h.emit('hmr/reload', new Map());
  document = await until(file, d => d.operation?.kind === 'source-reload');
  assert.equal(document.phase, 'idle');
  assert.equal(document.operation.status, 'unknown');
  const failure = new Error('Native continuation rejection');
  await assert.rejects(Promise.all(h.emit('loader/patch-context', root, () => Promise.reject(failure))), error => error === failure);
  assert.throws(() => h.emit('loader/patch-context', root, () => { throw failure; }), error => error === failure);
  document = await until(file, d => d.operation?.status === 'failed');

  h.emit('plugin-manager/operation', operation('recovered', 'settled', 'applied'));
  h.rows[1].fiber.state = 3;
  h.rows.push(h.row('import-failed', null));
  h.rows.push(h.row('waiting', 0));
  invalidPatch = true;
  h.bundles([hostile({ name: 'skipped', enabled: true, availability: 'missing',
    rows: [], error: hostile({ code: 'not-bundle' }) })]);
  document = await until(file, d => status(d, 'bundle:skipped') === 'failed');
  assert.equal(document.phase, 'error');
  assert.equal(status(document, 'include:optional'), 'failed');
  assert.equal(status(document, 'include:import-failed'), 'failed');
  assert.equal(status(document, 'include:waiting'), 'waiting');
  assert.equal(status(document, 'bridge:patch-syntax'), 'failed');
  assert.equal(document.capabilities.find(c => c.id === 'bridge:patch-syntax').required, false);
  assert.equal(document.capabilities.find(c => c.id === 'bridge:bundle-inventory').required, false);
  assert(document.capabilities.every(c => Object.keys(c).length === 3));

  h.emit('plugin-manager/operation', operation('uncertain', 'unknown'));
  h.emit('hmr/reload', new Map([[hostile({}), hostile({})]]));
  document = await until(file, d => d.operation?.kind === 'source-reload');
  assert.equal(document.operation.status, 'unknown');
  assert.equal(document.restart_intent, null);
  await dispose();
  const finalText = await readFile(file, 'utf8');
  await delay(30);
  assert.equal(await readFile(file, 'utf8'), finalText);
  assert([...h.listeners.values()].every(hooks => hooks.size === 0));
  assert.deepEqual(await readdir(directory), ['heartbeat.json']);
});

test('a hung bundle read cannot stop the heartbeat or retain ready inventory indefinitely', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-guard-bridge-'));
  const file = join(directory, 'heartbeat.json');
  const h = host();
  const dispose = startObserver(h.ctx, { file, epoch: 'epoch', patchReader: Promise.resolve(() => []) });
  t.after(async () => { await dispose(); await rm(directory, { recursive: true, force: true }); });
  const initial = await until(file, d => status(d, 'bridge:bundle-inventory') === 'ready');
  h.bundles(new Promise(() => {}));
  const waiting = await until(file, d => d.updated_at > initial.updated_at && status(d, 'bridge:bundle-inventory') === 'waiting');
  assert(waiting.updated_at > initial.updated_at);
});

test('failed readers, writes and teardown stay contained; delayed readers cannot revive the observer', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'dsh-guard-bridge-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const h = host();
  const dispose = startObserver(h.ctx, { file: join(directory, 'absent', 'heartbeat.json'),
    epoch: 'epoch', patchReader: Promise.reject(new Error('Native parser unavailable')) });
  await delay(30);
  await dispose();
  assert.deepEqual(await readdir(directory), []);
  let resolveReader;
  let resolveBundles;
  const delayed = host();
  delayed.ctx.get = name => name === 'pluginManager'
    ? { listBundles: () => new Promise(resolve => { resolveBundles = resolve; }) } : undefined;
  const close = startObserver(delayed.ctx, { file: join(directory, 'delayed.json'), epoch: 'epoch',
    patchReader: new Promise(resolve => { resolveReader = resolve; }) });
  await delay(10);
  await close();
  const before = await readdir(directory);
  resolveReader(() => { assert.fail('Disposed reader ran'); });
  resolveBundles([]);
  await delay(30);
  assert.deepEqual(await readdir(directory), before);
  assert([...delayed.listeners.values()].every(hooks => hooks.size === 0));
});

// Optional native contract check. Default tests need only Node and this source.
// Set DSH_GUARD_TEST_INSTALL_ANCHOR to the installed dsh/package.json and run
// node --expose-internals --test test-bridge.mjs. Only a temporary profile mounts;
// no endpoints, package installation or existing services are involved.
test('installed native filesystem bundle, parser, events and disable/re-enable', {
  skip: !process.env.DSH_GUARD_TEST_INSTALL_ANCHOR, timeout: 30000,
}, async t => {
  const anchor = process.env.DSH_GUARD_TEST_INSTALL_ANCHOR;
  const require = createRequire(anchor);
  const native = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-app-boot')).href);
  const bridge = new URL('.', import.meta.url).pathname;
  const directory = await mkdtemp(join(tmpdir(), 'dsh-bridge-native-'));
  const dir = join(directory, 'profiles', 'bridge-check');
  const file = join(directory, 'heartbeat.json');
  const keys = ['DSH_HOME', 'DSH_GUARD_BRIDGE_FILE', 'DSH_GUARD_EPOCH'];
  const previous = keys.map(key => process.env[key]);
  let ctx;
  t.after(async () => {
    await ctx?.fiber.dispose();
    await rm(directory, { recursive: true, force: true });
    keys.forEach((key, index) => {
      if (previous[index] === undefined) delete process.env[key];
      else process.env[key] = previous[index];
    });
  });
  await mkdir(join(dir, 'node_modules', '@local'), { recursive: true });
  await symlink(bridge, join(dir, 'node_modules', '@local', 'dsh-guard-bridge'));
  await writeFile(join(dir, 'package.json'), JSON.stringify({ name: 'bridge-check', private: true,
    dependencies: { '@local/dsh-guard-bridge': `link:${bridge}` },
    dsh: { profile: { bundles: ['@local/dsh-guard-bridge'] } } }));
  await writeFile(join(dir, 'cordis.yml'), '[]\n');
  await writeFile(join(dir, 'cordis.patch.yml'), '[]\n');
  const bin = new URL('./lib/bin.js', pathToFileURL(anchor));
  const dumped = spawnSync(process.execPath, [bin.pathname, '--profile', 'bridge-check', '--dump-config'], {
    env: { ...process.env, DSH_HOME: directory }, encoding: 'utf8', timeout: 10000,
  });
  assert.equal(dumped.status, 0, dumped.stderr);
  assert.match(dumped.stdout, /@local\/dsh-guard-bridge/);
  const profile = native.loadProfileDirectory('bridge-check', dir, anchor);
  const facts = { name: 'bridge-check', dir, patchPath: profile.patchPath, home: directory,
    installAnchor: anchor, cwd: directory, startedBundles: ['@local/dsh-guard-bridge'], overlays: [] };
  const patches = native.readProfilePatches('bridge-check', facts, profile);
  const resolution = await native.createRuntimeResolution({ installAnchor: anchor, profile, home: directory });
  process.env.DSH_HOME = directory;
  process.env.DSH_GUARD_BRIDGE_FILE = file;
  process.env.DSH_GUARD_EPOCH = 'native-test';
  ctx = await native.boot('bridge-check', join(dir, 'cordis.yml'), patches, async host => {
    host.provide('profileContext', facts);
    host.provide('pluginManager', { listBundles: async () => [] });
    await host.plugin(native.PluginPackages, { resolution });
  });
  const initial = await until(file, d => status(d, 'bridge:patch-syntax') === 'ready');
  assert.equal(status(initial, 'include:dsh-guard-bridge'), 'ready');
  assert.equal(status(initial, 'bridge:bundle-inventory'), 'ready');
  await ctx.parallel('plugin-manager/operation', operation('native-operation', 'started'));
  await until(file, d => d.phase === 'busy' && d.operation.kind === 'install');
  await ctx.parallel('plugin-manager/operation', operation('native-operation', 'settled', 'restart-required'));
  const settled = await until(file, d => d.operation?.status === 'restart-required');
  assert.equal(settled.restart_intent, null);
  await native.reconcileProfilePatches(ctx, [...patches, { id: 'dsh-guard-bridge', disabled: true }], 'bridge-check');
  const stopped = await readFile(file, 'utf8');
  await delay(2100);
  assert.equal(await readFile(file, 'utf8'), stopped);
  await native.reconcileProfilePatches(ctx, patches, 'bridge-check');
  const resumed = await until(file, d => d.generation.split(':')[0] !== initial.generation.split(':')[0]
    && status(d, 'bridge:patch-syntax') === 'ready');
  assert.equal(status(resumed, 'include:dsh-guard-bridge'), 'ready');
});

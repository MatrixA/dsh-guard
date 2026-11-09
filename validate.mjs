import { createRequire } from 'node:module';
import { readFileSync, realpathSync } from 'node:fs';
import { basename, dirname, isAbsolute, join } from 'node:path';
import { isDeepStrictEqual } from 'node:util';

const MAX_DOCUMENT = 1024 * 1024;
const PROTOTYPE_KEYS = new Set(['__proto__', 'constructor', 'prototype']);
const words = text => text
  .replace(/([A-Z])([A-Z][a-z])/g, '$1 $2')
  .replace(/([a-z0-9])([A-Z])/g, '$1 $2');
const DENIED_KEY = /(?:^|[^a-z0-9])(?:__proto__|constructor|prototype|names?|commands?|args?|includes?|approvals?|sandbox|permissions?|hosts?|ports?|isolate|inject|credentials?|secrets?|tools?|env|transport|urls?|api[\W_]*keys?|tokens?)(?:$|[^a-z0-9])/i;
const UNSAFE_STRING = /!!js|tag:yaml\.org,2002:js|(?:^|[^a-z0-9])(?:commands?|includes?)(?:$|[^a-z0-9])/i;
const fail = message => { throw new Error(message); };
const primitive = value => value === null || typeof value === 'boolean' ||
  typeof value === 'string' || (typeof value === 'number' && Number.isFinite(value));

function fields(value, required, optional = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('Expected an object');
  if (required.some(key => !Object.hasOwn(value, key)) ||
      Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))) {
    fail(`Expected fields: ${[...required, ...optional].join(', ')}`);
  }
}

function pathKey(path) {
  if (!Array.isArray(path) || path.length === 0 || path.length > 32 ||
      path.some(part => typeof part !== 'string' &&
        !(Number.isSafeInteger(part) && part >= 0))) fail('Invalid path (depth must be 1..32)');
  if (path.some(part => typeof part === 'string' && DENIED_KEY.test(words(part)))) {
    fail('Security-sensitive path is forbidden');
  }
  return JSON.stringify(path);
}

function safeValue(value) {
  if (!primitive(value)) fail('Replacement and enrolled values must be finite JSON primitives');
  if (typeof value === 'string' && UNSAFE_STRING.test(words(value))) {
    fail('Executable tags and command/include tokens are forbidden in replacement strings');
  }
}

function validate(request) {
  fields(request, ['action', 'yamlPackage', 'text'], ['changes', 'allowed']);
  if (request.action !== 'inspect' && request.action !== 'apply') fail('Unknown action');
  if (typeof request.text !== 'string' || Buffer.byteLength(request.text) > MAX_DOCUMENT) {
    fail('YAML text must be a string of at most 1 MiB');
  }
  if (typeof request.yamlPackage !== 'string' || !isAbsolute(request.yamlPackage) ||
      basename(request.yamlPackage) !== 'package.json' ||
      basename(dirname(request.yamlPackage)) !== 'yaml' ||
      basename(dirname(dirname(request.yamlPackage))) !== 'node_modules') {
    fail('yamlPackage must be an absolute node_modules/yaml/package.json path');
  }
  // The parent supplies this trusted dependency path; YAML never selects a module.
  const runtimeRoot = dirname(dirname(dirname(request.yamlPackage)));
  const require = createRequire(join(runtimeRoot, 'package.json'));
  if (realpathSync(require.resolve('yaml/package.json')) !== realpathSync(request.yamlPackage) ||
      JSON.parse(readFileSync(request.yamlPackage, 'utf8')).name !== 'yaml') {
    fail('yamlPackage does not match the runtime YAML dependency');
  }
  const YAML = require('yaml');
  const options = {
    schema: 'core', strict: true, uniqueKeys: true, merge: false,
    // 'silent' also suppresses yaml's MULTIPLE_DOCS error.
    resolveKnownTags: false, logLevel: 'error',
    customTags: [{ tag: 'tag:yaml.org,2002:js', resolve: source => source }],
  };
  const parse = text => {
    if (Buffer.byteLength(text) > MAX_DOCUMENT) fail('YAML text exceeds 1 MiB');
    const doc = YAML.parseDocument(text, options);
    if (doc.errors.length) fail(doc.errors.map(error => error.message).join('; '));
    return doc;
  };

  // Convert via Maps so YAML keys never become assignments to Object.prototype.
  // Bound alias expansion separately from the source-size limit; cycles are rejected.
  function plain(doc) {
    let budget = 8 * MAX_DOCUMENT;
    const active = new Set();
    function convert(value) {
      budget -= typeof value === 'string' ? Buffer.byteLength(value) + 8 : 16;
      if (budget < 0) fail('Expanded YAML data exceeds 8 MiB');
      if (primitive(value)) return value;
      if (!value || !(value instanceof Map || Array.isArray(value))) {
        fail('YAML data must contain only JSON values and string mapping keys');
      }
      if (active.has(value)) fail('Cyclic YAML aliases are forbidden');
      active.add(value);
      let result;
      if (Array.isArray(value)) result = value.map(convert);
      else {
        result = Object.create(null);
        for (const [key, item] of value) {
          if (typeof key !== 'string' || PROTOTYPE_KEYS.has(key)) {
            fail('Non-string or prototype-sensitive YAML mapping key');
          }
          convert(key);
          result[key] = convert(item);
        }
      }
      active.delete(value);
      return result;
    }
    return convert(doc.toJS({ mapAsMap: true, maxAliasCount: 100 }));
  }

  const pair = (node, key) => node.items.find(item =>
    YAML.isScalar(item.key) && item.key.value === key);
  const isRow = node => YAML.isMap(node) &&
    ['id', 'name'].some(key => {
      const identifier = pair(node, key)?.value;
      return YAML.isScalar(identifier) && typeof identifier.value === 'string' && identifier.value.length;
    }) && (pair(node, 'config') || pair(node, 'disabled'));

  function target(doc, path) {
    let node = doc.contents;
    let rowField;
    for (const part of path) {
      if (!node || node.tag || node.anchor || YAML.isAlias(node)) {
        fail('Cannot edit through a tag, anchor, alias, or missing node');
      }
      if (YAML.isSeq(node)) {
        if (!Number.isSafeInteger(part) || part < 0 || part >= node.items.length) {
          fail('Path must reference an existing sequence item with an integer index');
        }
        node = node.items[part];
      } else if (YAML.isMap(node)) {
        if (pair(node, '<<')) fail('Cannot edit a mapping containing a merge key');
        if (typeof part !== 'string') fail('Mapping paths require string keys');
        const entry = pair(node, part);
        if (!entry) fail('Path must reference an existing direct key');
        if (entry.key.tag || entry.key.anchor) fail('Cannot edit through a tagged or anchored key');
        if (isRow(node)) {
          if (part !== 'disabled' && part !== 'config') {
            fail('Only direct row disabled or config fields may be edited');
          }
          rowField = part;
        }
        node = entry.value;
      } else fail('Path traverses a scalar');
    }
    if (!rowField) fail('Path must select an existing row disabled or config field');
    if (!YAML.isScalar(node) || node.tag || node.anchor || !primitive(node.value)) {
      fail('Only existing untagged, unanchored JSON scalar nodes may be edited');
    }
    return { node, rowField };
  }

  // The expected tree is built BEFORE mutation, substituting only selected values.
  // Locations/source spelling are formatting; tags, comments and unselected styles are not.
  function tree(doc, selected, replacements = new Map()) {
    function visit(node) {
      if (node === null) return null;
      const meta = {};
      const keys = ['tag', 'anchor', 'comment', 'commentBefore', 'spaceBefore', 'flow'];
      if (!selected.has(node)) keys.push('type', 'format', 'minFractionDigits');
      for (const key of keys) if (node[key] !== undefined) meta[key] = node[key];
      if (YAML.isScalar(node)) {
        return ['scalar', meta, replacements.has(node) ? replacements.get(node) : node.value];
      }
      if (YAML.isAlias(node)) return ['alias', meta, node.source];
      if (YAML.isSeq(node)) return ['sequence', meta, node.items.map(visit)];
      if (YAML.isMap(node)) return ['mapping', meta, node.items.map(item => [visit(item.key), visit(item.value)])];
      fail('Unsupported YAML node');
    }
    return [doc.commentBefore, doc.comment, { ...doc.directives?.yaml }, { ...doc.directives?.tags },
      doc.directives?.docStart, doc.directives?.docEnd, visit(doc.contents)];
  }

  const doc = parse(request.text);
  const data = plain(doc);
  if (request.action === 'inspect') return { ok: true, text: request.text, data };
  if (!Array.isArray(request.changes) || request.changes.length === 0 || request.changes.length > 32) {
    fail('apply requires 1..32 changes');
  }
  if (!Array.isArray(request.allowed) || request.allowed.length === 0) fail('apply requires an allowed list');
  const enrolled = new Map();
  for (const entry of request.allowed) {
    fields(entry, ['path', 'values']);
    const key = pathKey(entry.path);
    if (enrolled.has(key)) fail('Duplicate enrolled path');
    if (!Array.isArray(entry.values) || entry.values.length === 0) fail('Allowed values must be a nonempty array');
    const selected = target(doc, entry.path);
    for (const value of entry.values) {
      safeValue(value);
      if (selected.rowField === 'disabled' &&
          (typeof selected.node.value !== 'boolean' || typeof value !== 'boolean')) {
        fail('Row disabled edits require boolean values');
      }
    }
    enrolled.set(key, { ...selected, values: entry.values });
  }
  const replacements = new Map();
  for (const change of request.changes) {
    fields(change, ['path', 'expected', 'value']);
    const entry = enrolled.get(pathKey(change.path));
    if (!entry) fail('Path is not explicitly enrolled');
    if (replacements.has(entry.node)) fail('Duplicate change path');
    safeValue(change.value);
    if (!entry.values.some(value => isDeepStrictEqual(value, change.value))) fail('Value is not explicitly allowed');
    if (!isDeepStrictEqual(entry.node.value, change.expected)) fail('Expected value does not match existing value');
    replacements.set(entry.node, change.value);
  }
  const selected = new Set(replacements.keys());
  const expectedTree = tree(doc, selected, replacements);
  for (const [node, value] of replacements) node.value = value;
  const text = doc.toString({ lineWidth: 0 });
  const updated = parse(text);
  const updatedSelected = new Set(request.changes.map(change => target(updated, change.path).node));
  if (!isDeepStrictEqual(expectedTree, tree(updated, updatedSelected))) {
    fail('Roundtrip changed unselected YAML content, tags, comments, or styles');
  }
  return { ok: true, text, data: plain(updated) };
}

try {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length;
    if (bytes > 16 * MAX_DOCUMENT) fail('JSON request exceeds 16 MiB');
    chunks.push(chunk);
  }
  const result = validate(JSON.parse(Buffer.concat(chunks).toString('utf8')));
  process.stdout.write(`${JSON.stringify(result)}\n`);
} catch (error) {
  process.stdout.write(`${JSON.stringify({ ok: false, error: error instanceof Error ? error.message : String(error) })}\n`);
  process.exitCode = 1;
}

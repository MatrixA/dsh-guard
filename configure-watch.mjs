// Render a native HMR patch without evaluating !!js or replacing unrelated config.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const [yamlPackage, patchPath, rootsJson] = process.argv.slice(2);
const require = createRequire(yamlPackage);
const YAML = require(yamlPackage.replace(/package\.json$/, ''));
const roots = JSON.parse(rootsJson);
if (!Array.isArray(roots) || roots.some(root => typeof root !== 'string' || !root.startsWith('/'))) throw new Error('Absolute HMR roots required');
const document = YAML.parseDocument(readFileSync(patchPath, 'utf8'), {
  schema: 'core', uniqueKeys: true, resolveKnownTags: false,
  customTags: [{ tag: 'tag:yaml.org,2002:js', resolve: value => value }],
});
if (document.errors.length || !YAML.isSeq(document.contents)) throw new Error('Invalid profile patch');
const rows = document.contents.items.filter(row => YAML.isMap(row) && row.get('id') === 'hmr');
if (rows.length > 1) throw new Error('Ambiguous HMR patch');
let row = rows[0];
if (!row) {
  row = document.createNode({ id: 'hmr', config: { root: roots } });
  document.contents.add(row);
} else {
  let config = row.get('config', true);
  if (config === undefined) { config = document.createNode({}); row.set('config', config); }
  if (!YAML.isMap(config)) throw new Error('HMR config is executable or nonliteral; preserve it and configure source roots natively');
  const existing = config.get('root', true);
  if (existing !== undefined && (!YAML.isSeq(existing) || existing.items.some(item => !YAML.isScalar(item) || typeof item.value !== 'string' || item.tag))) {
    throw new Error('HMR roots are executable or nonliteral; preserve them');
  }
  config.set('root', document.createNode([...new Set([...(existing?.items.map(item => item.value) ?? []), ...roots])]));
}
console.log(JSON.stringify({ text: document.toString(), roots }));

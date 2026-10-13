import {mkdir, copyFile, readFile, writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';

const here = new URL('./', import.meta.url);
// Bundled verbatim core permits uploading web/ as a standalone Vercel root.
const coreUrl = new URL('./core.mjs', here);
const dist = new URL('./dist/', here);
const files = ['index.html', 'styles.css', 'app.mjs', 'model.mjs', 'transport.mjs', 'config.mjs'];
await mkdir(dist, {recursive: true});
for (const name of files) {
  if (['app.mjs', 'model.mjs', 'transport.mjs'].includes(name)) {
    const source = await readFile(new URL(name, here), 'utf8');
    await writeFile(new URL(name, dist), source.replaceAll("from '../core.mjs'", "from './core.mjs'"));
  } else await copyFile(new URL(name, here), new URL(name, dist));
}
// Package the shared source verbatim. The parent remains read-only.
await copyFile(coreUrl, new URL('core.mjs', dist));
await writeFile(new URL('.nojekyll', dist), '');
const core = await readFile(coreUrl);
const manifest = {files: [...files, 'core.mjs', '.nojekyll'], sharedCoreSha256: createHash('sha256').update(core).digest('hex')};
await writeFile(new URL('build-manifest.json', dist), JSON.stringify(manifest, null, 2) + '\n');
console.log(JSON.stringify({output: dist.pathname, ...manifest}, null, 2));

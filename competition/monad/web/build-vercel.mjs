import './build.mjs';
import {writeFile} from 'node:fs/promises';
// Vercel serves the static UI and its bounded API on one HTTPS origin.
await writeFile(new URL('./dist/config.mjs', import.meta.url), "export const config = Object.freeze({proxyBase: '/api'});\n");
console.log('Vercel bundle configured for same-origin /api.');

// Optional competition media is supplied for deployment; generated binaries stay out of Git.
const {cp,access} = await import('node:fs/promises');
const media = new URL('./media/', import.meta.url);
if (await access(media).then(()=>true,()=>false)) await cp(media, new URL('./dist/media/',import.meta.url),{recursive:true});
for (const name of ['demo.html','pitch.html']) await cp(new URL('./'+name,import.meta.url),new URL('./dist/'+name,import.meta.url));

import {CONTEXT_URL, RPC_URL} from './core.mjs';

export function proxyTarget(url, base) {
  if (!base) return url;
  const root = base.replace(/\/$/, '');
  if (url === CONTEXT_URL) return `${root}/context`;
  if (url === RPC_URL) return `${root}/rpc`;
  const path = new URL(url);
  if (path.origin === 'https://app.perpl.xyz' && /^\/api\/v1\/market-data\/\d+\/candles\/3600\/\d+-\d+$/.test(path.pathname) && !path.search) {
    return `${root}/candles/${path.pathname.split('/market-data/')[1]}`;
  }
  throw new Error('Public source is outside the proxy allowlist');
}

export function installTransport(base) {
  if (base) {
    const url = new URL(base, location.href);
    if (location.protocol === 'https:' && url.protocol !== 'https:') throw new Error('An HTTPS page requires an HTTPS public-data proxy.');
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('Invalid public proxy URL.');
  }
  const nativeFetch = globalThis.fetch.bind(globalThis);
  globalThis.fetch = async (url, options) => {
    const target = proxyTarget(String(url), base);
    try {
      const response = await nativeFetch(target, {...options, credentials: 'omit', cache: 'no-store'});
      if (!response.ok) throw new Error(`Public request failed: HTTP ${response.status} (${new URL(target, location.href).pathname}).`);
      return response;
    } catch (error) {
      if (error instanceof TypeError) throw new Error(`Public request could not be read from ${new URL(target, location.href).hostname}. Network or CORS may be blocking it. ${base ? 'Check the public proxy.' : 'See Hosting & sources for the HTTPS proxy requirement.'}`);
      throw error;
    }
  };
}

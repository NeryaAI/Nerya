import {createPublicHandler} from '../server.mjs';
const handler = createPublicHandler();
export default async function publicData(req, res) {
  // Vercel rewrites the public route into this function with a routing-only key.
  // Never accept extra query arguments or an arbitrary upstream URL.
  const url = new URL(req.url, 'https://localhost');
  const route = req.query?.route ?? url.searchParams.get('route');
  const extra = [...url.searchParams.keys()].some(key => key !== 'route');
  if (typeof route !== 'string' || extra || url.searchParams.getAll('route').length > 1) {
    res.statusCode = 400;
    res.setHeader('Content-Type', 'application/json');
    return res.end(JSON.stringify({error:'Invalid public proxy route'}));
  }
  req.url = '/api/' + route;
  return handler(req, res);
}

# Nerya · Monad / Perpl public judge explorer

Web-only public interaction surface. Real public market reads, transparent hypothetical
comparison, no account connection, signing, secrets, mock data, or order execution.
The shared ../core.mjs is read-only. core.mjs here is a byte-for-byte packaging copy
so the web folder can be deployed independently; tests verify equality with its source.

## Vercel deployment bundle (recommended for live hosting)

Deploy THIS web directory as the Vercel project root. No dependency installation,
API credentials, or environment secrets are needed. vercel.json selects
node build-vercel.mjs and dist as the static output. api/public.mjs is the serverless
entry and /api/:route* rewrites to its bounded public-data handler. The Vercel build
sets dist/config.mjs to same-origin /api. HTTPS static assets and public API share
one origin, avoiding Perpl's missing cross-origin response permission.

From this directory, you can run:

    npm run build:vercel
    vercel

The user handles deployment. No deployment, CLI login, commit, or push was performed.
Vercel's deployed routing/runtime has not been tested here. Its adapter was tested
through local HTTP. A 20-second function duration permits the 15-second upstream
timeout. Same-host HTTP or HTTPS origins are accepted, including TLS termination;
this does not depend on a spoofable forwarded-protocol header.

## Local recording

Node 20+ required; validated with Node 26.0.0. No packages to install.

    npm run build
    npm start

The server serves dist on http://127.0.0.1:8787 and injects /api into the HTML.
PORT can change the port. It binds only to loopback. Open the page yourself and
stop with Ctrl-C. No browser was launched or controlled during this task.

MON is the initial market; BTC and ETH are selectable. Refresh and market change
perform real public requests. Hypothetical input changes recalculate locally.
The page shows loading, API/CORS errors, invalid inputs, insufficient candles,
and stale observations. Failed refresh clears prices and comparisons. There is
no offline fallback and no automatic network polling. Source age updates every
five seconds and can change the verdict to Review required.

## Static HTTPS / GitHub Pages candidate

npm run build creates the self-contained dist directory with relative asset paths
and .nojekyll. Publish its contents through your own static hosting process.
Opening via file:// is unsupported; use HTTP(S). No CDN scripts or external fonts.

GitHub Pages cannot run the proxy. Direct mode (empty proxyBase in config.mjs)
tries real browser requests, but current HTTP evidence shows Perpl does not grant
the tested cross-origin read. For working Pages hosting, provide a separate HTTPS
proxy and set config.mjs proxyBase to its public URL ending in /api, then rebuild.
Set PUBLIC_ORIGINS on that proxy to the exact Pages origin, without repository path.
The included server can run behind your own HTTPS reverse proxy. It is a minimal
local tool; public operations should add rate/resource limits at the hosting layer.
An HTTPS page cannot use an HTTP proxy. A judge's localhost is not your server.

HTTP CORS evidence on 2026-10-13, with Origin https://nerya-judge.github.io
(an illustrative origin, not a deployed URL):

- Perpl context GET: 200, no Access-Control-Allow-Origin.
- Perpl MON candles GET: 200, no Access-Control-Allow-Origin.
- Monad RPC OPTIONS and JSON POST: 200, matching allow-origin.
- Local proxy context GET: 200; RPC OPTIONS: 204, matching configured origin.

A successful HTTP response is not proof of browser readability. Do not use no-cors:
opaque responses cannot provide data. Recheck with the actual hosted origin; policies
may change. CORS enforcement/rendering was not browser-tested under this task.

## Bounded public proxy

Only these routes are supported:

| Route | Method | Upstream |
| --- | --- | --- |
| /api/context | GET | Perpl public context |
| /api/rpc | POST | Monad RPC, only eth_chainId |
| /api/candles/{1,10,20}/candles/3600/{start}-{end} | GET | Perpl closed hourly candles |

RPC body must exactly match the shared-core read:
{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}.
No transaction methods, batches, arbitrary URLs, user credentials/cookies, or headers
are forwarded. Query parameters are rejected (the Vercel adapter consumes only its
internal route key). Candle windows are limited to 72 hours within the last 96 hours,
with no future end. Other routes/methods/origins fail explicitly. Upstreams time out
after 15 seconds; failures return 502 with no substituted payload. Cross-origin
OPTIONS is allowed only for configured PUBLIC_ORIGINS; no credentialed CORS.

## Hypothetical assumptions

Both columns call the unchanged shared scenario function. Baseline is $1,000;
candidate defaults to $300, adjustable from $0 to $1,000 with presets. Default
capital $1,000, adverse move 5%, loss budget 2%, assumed slippage 10 bps per side.
The fee comes from each live observation. At the verified 3.45 bps taker fee:

| Notional | Move loss | Round-trip costs | Stress loss |
| --- | ---: | ---: | ---: |
| $1,000 | $50 | $2.69 | $52.69 |
| $300 | $15 | $0.807 | $15.807 (displayed $15.81) |

The shared budget is $20 and the loss reduction is $36.883. No hard-coded product
results: changes in source fee or assumptions change the result. Observations
older than 120 seconds or over 10 seconds in the future require review at any size.

Flat account, one hypothetical position, linear adverse move, two taker fees plus
two assumed slippage allowances. USD is a comparison unit and capital is assumed,
not a wallet balance. Funding, liquidation, margin requirements, gas, actual fills,
existing exposure, and execution depth are excluded. The mathematical notional
ceiling is not executable sizing advice. No outcome authorizes a trade or claims profit.

## Validation

    npm run build:vercel
    npm test
    npm run validate:live

Offline tests check shared arithmetic, aging, invalid inputs, fixed-source mapping,
proxy rejection paths, preflight, same-host HTTPS, Vercel adapter, and core byte equality.
Only loopback HTTP is used by npm test. Numerical test inputs are never UI market data.
Live validation explicitly requests the actual external sources and then exercises
shared snapshot through the real loopback proxy for MON, BTC, ETH. It writes raw
responses/snapshots and report.json inside validation and closes the test server.
These are dated evidence artifacts, not product fixtures. dist and validation are
ignored generated directories. Browser visual and interaction acceptance remain
unverified by instruction. Production Vercel acceptance awaits your deployment.

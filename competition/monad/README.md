# Nerya · Monad Metropolis contribution

Event: **Monad Metropolis**. Nerya is a skill-first autonomous trading workspace across LP, perpetuals and onchain token strategies. Agents author strategy packages, research with specialist teams, test assumptions and propose the next version. The event contribution extends that workspace to Monad/Perpl; the public calculator is an auxiliary interface.

- Auxiliary live Monad preview: https://nerya-monad-metropolis.vercel.app
- Source branch: competition/monad-metropolis-20261013
- Contribution source: competition/monad/
- Revised multi-chain pitch with MiMo narration: https://youtu.be/BwE8dyEVloY
- Editable deck: https://nerya-monad-metropolis.vercel.app/media/Nerya-Monad-Metropolis.pptx

## Wider product

Existing execution source includes reviewed Uniswap/PancakeSwap LP add/remove/collect, perpetual connectors and Jupiter swaps. Account binding, quote/risk/approval controls, capital reservations and reconciliation mediate execution. Genuine native recordings show RaydiumLP and Velocityperpetual packages/replays, Solana meme pool-watcher proposals, teams and an agent-authored strategy lab. These are distinct from real-money fill acceptance; the submission does not claim every adapter is live accepted or Monad LP/meme execution completed.

## New contribution / existing product disclosure

The event contribution is the Monad mainnet identity check, Perpl public market
and closed-hour candle observations, shared hypothetical loss-budget calculation,
native tool/skill adapter, and public judge website with a bounded serverless proxy.
The website compares $1,000 notional with an editable candidate defaulting to $300,
using the actual venue fee and explicit assumptions. No sample prices or invented
execution history are shown. Both CLI and website reuse core.mjs; the web packaging
copy is byte-for-byte identical.

**Nerya predates this contribution.** Its agent runtime, dashboard, native tool and
skill framework, strategy/proposal workflows, and runtime Risk Gate / Approval Gate
are existing product foundations, not claimed as newly built for this event. This
public website is a narrow observation and scenario interface. Its budget verdict
is a shared-core calculation, not evidence that the full runtime gates or an
agent-driven order workflow have executed. The native adapter is included in source. The technical video separately records its real tool call, a saved MON report and successful reread in an isolated Nerya workspace. No order was placed. A relative-path read initially failed and was corrected; the video retains that recovery.

## Run and test

From the repository root, with Node.js 20+:

    node competition/monad/test.mjs
    node competition/monad/scripts/market.mjs '{"symbol":"MON","notional":300}'

The first command tests scenario arithmetic without external requests. The second
makes real public HTTP requests and prints the observation and hypothetical result.

For the website:

    cd competition/monad/web
    npm run build
    npm test
    npm start

Open http://127.0.0.1:8787 yourself. No dependency installation or secrets required.
For Vercel packaging use npm run build:vercel; vercel.json explicitly selects
framework:null, dist static output, and the /api serverless rewrite.

Explicit network validation:

    npm run validate:live
    node verify-hosted.mjs

The hosted check uses unauthenticated HTTP against the live URL, verifies real root
HTML and module/CSS/config assets, reads context/candles/eth_chainId, checks rejected
routes and methods, and runs the shared snapshot/scenario through the deployed API
for MON/BTC/ETH. Raw responses and the dated acceptance report are saved under
web/validation/hosted/. Neither command controls a browser or trading runtime.
See web/README.md for hosting and CORS details. GitHub Pages needs a separate HTTPS
proxy: current Perpl HTTP responses do not grant the tested cross-origin read.

## Capability boundaries

Public reads only: Perpl context, recent closed hourly candles for MON/BTC/ETH, and
Monad RPC eth_chainId (chain 143). The proxy rejects arbitrary upstream URLs,
transaction RPC methods, unsupported markets, writes, unapproved origins, and
out-of-bound candle windows. No account keys, wallet connection, signatures,
collateral transfer, or order execution.

Default scenario: assumed $1,000 capital, 5% adverse move, 2% loss budget, two taker
fees, and assumed 10 bps slippage per side. At the live reported 3.45 bps fee,
$1,000 notional produces $52.69 stress loss and $300 produces $15.807 (displayed
$15.81), against a $20 assumed budget. Outcomes recalculate with actual source fees
and inputs. Old or future-skewed observations require review.

Funding, liquidation, gas, margin requirements, actual fills, existing positions,
and execution depth are excluded. The mathematical ceiling is not an executable
size recommendation. No live fills, profit, backtest performance, or real-money
acceptance is claimed. Hosted HTTP acceptance is distinct from browser visual /
interaction acceptance. The final recording shows actual refresh and resizing in the deployed interface; desktop BTC and a 390px mobile viewport were also checked.

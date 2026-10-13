# Monad testnet canonical UniV3 deployment kit

Operator bootstrap for chain 10143 LP and meme swap acceptance. This is a
canonical test deployment, not an official Uniswap venue. nUSDC/nMEME are
fixed-supply test assets with no monetary value. Parent handles gateway,
native recording and Perpl. This task does not read a funded key or broadcast.

## Provenance

Factory and Pool: @uniswap/v3-core@1.0.1 npm artifacts. SwapRouter (8-field
exactInputSingle), QuoterV2 and NonfungiblePositionManager:
@uniswap/v3-periphery@1.4.4 npm artifacts. Canonical creation bytecode is
unchanged and SHA256-pinned in deploy.py. TestAssets.sol uses solc@0.8.28,
optimizer 200, EVM Paris. Only NFT metadata is simplified; factory/pool/
router/quoter/NFPM are genuine. Original npm licenses remain applicable
(core BUSL-1.1, periphery GPL-2.0-or-later); test contracts are MIT.

Existing v3/fork/node_modules is used by default; MONAD_ARTIFACT_NODE_MODULES
can relocate it. build/artifacts.json records versions, compiler, source and
artifact hashes/settings. Python checks creation hashes before signing.

Official testnet WMON: 0xFb8bf4c1CC7a94c73D209a149eA2AbEa852BC541.
Read-only preflight observed code hash
0xa32df4f1226f1bb0d6ce9b917752ed687857899a35ebf902c9992edb73b138b6
and isolated wallet balance 5 test MON.

## Build and preflight

Run from isolated repository root:

    node competition/monad/testnet/build.mjs
    /Users/rick/Documents/Project/Nerya/agent/.venv/bin/python competition/monad/testnet/deploy.py \
      --journal competition/monad/testnet/validation/monad.journal.json \
      --output-prefix competition/monad/testnet/validation/monad \
      --gas-budget-mon 3

Without --broadcast: read-only chain/balance/WMON/artifact checks, no key read.
Only https://testnet-rpc.monad.xyz (optional trailing slash) is allowlisted;
alternate hosts, mainnet, query paths and localhost cannot be selected by CLI.
Every transaction checks chain ID 10143. Expected sender is fixed to funded
isolated demo wallet 0x6BB4380Bb2aD6dbA67cAb898dFFa2ED2D4c9E5fe.

## Parent-authorized execution

Parent sets MONAD_TESTNET_PRIVATE_KEY_PATH to the existing private file and
adds --broadcast to the command. Key path is environment-only, never logged.
Default total gas cap is 0.1 MON; --gas-budget-mon 3 explicitly permits the
parent-authorized maximum. Caps above 3 MON fail. Each new signature requires
settled gas + pending maximum gas + proposed maximum gas within budget.
Liquid native balance separately covers value + gas. Estimates add 20%
headroom; legacy gasPrice and no replacement transactions are used.

Local bootstrap measured 27,793,403 gas for 23 transactions, ~2.835 MON at
the observed 102 gwei. Summed maximum reservations would be ~3.431 MON,
but actual settled costs replace reservations after each receipt. Testnet
estimates/receipts determine whether the 3 MON cap fits. The kit stops before
exceeding it, keeps checkpoints, and never raises the cap automatically.

Wrap 0.8 test MON total: each pool uses 0.15 WMON, leaving at least 0.5 WMON
for gateway swaps. Never wrap the full 5 MON balance. Pools:

- wmon_nusdc: fee 500, spacing 10; 0.15 WMON / 0.15 nUSDC.
- nmeme_wmon: fee 3000, spacing 60; 150 nMEME / 0.15 WMON.

Initial ratios use WMON=1, nUSDC=1, nMEME=0.001 test units and account for
sorting/decimals exactly. Seeds use full tick ranges; approvals are exact
finite seed amounts and revoked after mint. Bootstrap NFTs belong to the
operator; gateway acceptance creates its own strategy-owned LP normally.

FixedTestPriceFeed returns immutable chosen units and current block observation
time. It is not a market oracle; timestamps do not prove market-price freshness.
Never use it for mainnet/real-value risk. Config carries test valuation labels.

## Recovery and output

Journal binds chain/sender/artifacts/nonce/intent. Signed bytes and derived hash
are checkpointed BEFORE broadcast, then canonical block/transaction receipts
and gas accounted. Mode-0600 atomic journal and exclusive lock protect state.
Journal contains signed authorizations, no private key: do not publish it.

Interrupted sends reconcile by hash; only exact already-signed bytes may be
rebroadcast. Nonce conflict, revert, reorganized checkpoint, altered artifact,
intent or code fails closed. No new nonce retry or duplicate wrap/mint/deploy.

Outputs: raw deployment in <prefix>.deployment.json, nested config in
<prefix>.config.json at financial.defi.deployments.monad-testnet.uniswap_v3.
Fields: reviewed/testnet/official=false/deployment_origin/protocol_variant/
source_revision/wrapped_native, contracts/code hashes, pools/ticks/hash,
tokens/decimals/hash/standard behavior, price feeds/start block/seed NFT evidence.
Outputs do not enable trading, permissions or approval bypasses. All generated
artifacts, journals and output configs are Git-ignored.

## Local acceptance

Use a separate empty local EVM, not the parent's fork:

    /Users/rick/Documents/Project/Nerya/pitch-video/monad-metropolis-20261013/v3/fork/node_modules/@foundry-rs/anvil-darwin-arm64/bin/anvil \
      --port 19643 --chain-id 10143 --silent --accounts 0
    /Users/rick/Documents/Project/Nerya/agent/.venv/bin/python competition/monad/testnet/self_test.py

self_test.py uses loopback and a random memory-only account. Only local WMON
is a harness; all V3 contracts/pools are genuine pinned artifacts. Checks:
23 bootstrap transactions, two seeded pools/NFTs, actual Quoter quotes and
eight-field Router swaps/canonical events, finite consumed allowances, NFT
metadata/ownership, wrong-chain and unsafe-RPC rejection, pre-sign budget
rejection, and idempotent restart with zero new nonces. These prove local EVM
behavior only. Remote testnet deployment/receipt acceptance is parent's step.

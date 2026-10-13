# Monad testnet bounded LP range

Native deterministic Nerya script strategy. One cycle: read the configured reviewed
UniV3 pool, align a bounded range around its actual tick, mint within two token
input caps, watch fee growth and range, collect on positive fees/range exit/hold
expiry, then remove the confirmed NFT's full liquidity. Original seed NFTs are
never adopted. The FinancialGateway's ProtocolBook enforces sole strategy ownership.

No promise of fees: an idle pool can produce zero. Test valuations are fixed test
units and cannot establish real-world USD return. Collection/exit receipts carry
actual cashflows; gas is separate. Bounded collect caps can leave residual owed
tokens; inspect inventory and reconcile before claiming the position fully closed.

Defaults are paper, disabled schedule and disabled execution. Configure a reviewed
10143 deployment, isolated account/wallet and finite limits before installation.
Only an operator may enable live testnet execution and a revision-bound grant.
Use the shared README for installation, public-reader registration and recovery.

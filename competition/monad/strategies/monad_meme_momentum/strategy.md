# nMEME test pool momentum roundtrip

Native deterministic Nerya script strategy. The explicitly configured token is
nMEME, a fixed-supply test asset with no monetary value. It reads real slot0 prices
from the reviewed nMEME/WMON UniV3 testnet pool. Two distinct increasing blocks
within the sample window must show bounded positive quote-per-nMEME momentum.
Otherwise it keeps observing and does not trade. No generated social signal.

One finite WMON buy is followed by observation, software stop or hold expiry,
then one reverse swap of exactly the confirmed token credit from that buy. It
never sells a predicted output or the whole wallet inventory. Both swaps use
finite input, nonzero minimum output, grant/permissions and canonical receipts.

Pool liquidity/price can change before execution. Stops depend on active fresh
ticks and authorization; they do not guarantee an exit price or maximum loss.
Test-unit values and synthetic test fixtures must not be described as profit.
Operator-created momentum is a controlled smoke, not organic market discovery.

Defaults are paper, disabled schedule and disabled execution. Configure, propose,
review and install with the shared README; no testnet write is done by this worker.

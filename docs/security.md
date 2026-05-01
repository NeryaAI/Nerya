# Security

## SecretVault

- AES-GCM-encrypted file at `workspace/vault/secrets.enc`.
- Data key wrapped with a key derived from `NERYA_VAULT_PASSPHRASE` (or a
  per-host file referenced by `workspace/vault/keyring.ref` when the
  operator uses the system keyring).
- Each secret is tagged with:
  - `name` (e.g. `binance_spot_trade_key`)
  - `kind` (`exchange_api_key`, `chain_private_key`, `bot_token`, `provider_api_key`)
  - `scope` (`read`, `trade`, `withdraw`, `sign`, `publish`)
  - `owner` (`account:<id>` | `strategy:<id>` | `runtime`)
- Only `secret_ref = "vault://<name>"` ever enters Agent / skill / script
  context; the real value is resolved inside the security layer.

The agent only sees:

```
{
  "name": "binance_spot_trade_key",
  "kind": "exchange_api_key",
  "scope": ["read", "trade"],
  "preview": "****abcd",
  "ref": "vault://binance_spot_trade_key"
}
```

## Risk Gate

See `docs/risk-gate.md`. Key properties:

- Cannot be toggled off from Agent context.
- Kill switch can only be flipped from operator CLI / signed policy.
- Every verdict is journalled with the full snapshot.

## Prompt firewall

`nerya/agent/prompt_firewall.py` wraps every untrusted string (news bodies,
social posts, script outputs, tool outputs, subagent outputs) in
`<untrusted source="..." hash="...">...</untrusted>` markers before it
enters the model prompt. The system prompt instructs the model to treat
those segments as data, not instructions.

When the output contains a candidate TradeIntent that references
directives coming from an untrusted segment, `output_parser.py` flags it
for Risk Gate escalation (`reason: untrusted_influence`).

## Script sandbox

See `docs/script-system.md`.

## Signer

`security/signer.py` owns every private key. The agent calls
`onchain_skill.prepare_signed_tx` with the unsigned payload; the skill
hands it to `signer.sign_payload(intent=...)`. `signer` refuses to sign
if:

- The target chain / contract is not on the strategy's allow list
- The nonce / gas / value exceeds `signer.policy`
- The wallet scope is wrong (`sign` missing)
- The intent is not referenced by a live approved TradeIntent

## Policy signer

`security/policy_signer.py` provides detached HMAC / ed25519 signatures
for operator-issued policies (kill switch toggles, live trading flags,
approvals > `hard_approval_threshold`). Policies are stored in
`workspace/approvals/` and verified on load.

## Audit log

`journals/security.jsonl` captures every vault access, signer invocation,
policy verification, prompt-firewall annotation and redaction event.
Agents and skills **cannot** write to this journal; only security code
paths can.

## Secret scanner + buffer

`nerya/security/secret_scanner.py` runs *before* anything reaches the
Agent. It scans incoming chat / gateway / dashboard input for known
secret shapes (EVM private keys, BIP-39 mnemonics, JWTs, AWS keys,
prefixed exchange API tokens, bare 64-char hex/Base58 keys, generic
high-entropy API tokens) and replaces every match with the placeholder
`<<NERYA_SECRET:<token>>>`. The plaintext is parked in
`SecretBuffer` (in-memory, process-wide, 10-minute TTL, max 64 entries).
The Agent only ever sees the placeholder, and the operator gets a
courteous notice listing what was captured and when it expires.

Two writers consume from the buffer:

1. `routes_account_intake.submit_intake` — the agent submits the
   *placeholder* in the structured payload, the route resolves it
   just-in-time, encrypts it into the SecretVault, and discards the
   plaintext.
2. `routes_security.buffer_*` — the operator can list buffer metadata
   (`/security/secrets/buffer`) and clear it (`/security/secrets/buffer/clear`).
   Plaintext is never returned over HTTP; only previews and types.

Tokens are single-use: once the intake route resolves a placeholder,
the buffer entry is dropped. Any other consumer requesting the same
token gets `unknown_or_expired_secret`.

## Account intake (sandboxed credential entry)

`nerya/trading/account_intake.py` is the Agent-driven account creation
flow. The Agent issues `intake.create` with the venue id and gets back
a schema describing each credential field (label, kind, sensitivity).
The operator (or the Agent prompting the operator) fills the form;
sensitive fields use the placeholder syntax above so the actual secret
never reaches the model.

`submit_intake` then:

- Resolves placeholders against the `SecretBuffer`.
- Refuses if any required-secret token is missing or expired.
- Stores plaintext into `SecretVault` with scope `exchange` and the
  owner pinned to `account:<id>`.
- Auto-pins `mode: paper` when the venue's `ExchangeProviderSpec`
  reports `supports.place_order = False` or when the account kind is
  `data_source`.
- Writes the resulting `AccountProfile` row into `accounts.yml`,
  populating `credentials` with `vault://<name>` refs and parking
  non-secret fields under `provider_config`.

## HTTP auth headers for data sources

`nerya/connectors/http_auth.py` lets a data-source account ship custom
HTTP headers (e.g. `Authorization: Bearer <token>`) without any code
changes. Headers live on the account row at
`provider_config.headers`. Each value can include the substring
`vault://<name>`; resolution happens at request time and only ever in
memory, so the disk view (and the `/accounts/headers/list` route)
shows masked previews. `/accounts/headers/patch` rejects values that
look like plaintext secrets — operators must store the secret via
`/security/secrets/put` first and then reference it.

"""Wallet provider registry.

The registry exposes every provider that ships with Nerya — regardless
of whether its optional dependencies are currently installed. Use
:func:`build_provider` to materialize one, :func:`resolve_active` to
instantiate the provider selected in ``nerya.yml`` (``wallet.provider``),
and :func:`list_providers` to render a readiness report for the dashboard
/ CLI.

No factory in this module installs anything on its own. When an operator
selects a provider whose deps are missing, instantiation succeeds but the
provider's :meth:`~WalletProvider.readiness` returns ``ready=False`` and
every method that actually needs the dep raises
:class:`WalletDependencyError` with a precise install hint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import WalletProviderNotFound
from .protocol import WalletCapabilities, WalletProvider, WalletReadiness
from .providers import (
    BinanceAgenticWallet,
    BitgetWalletSkill,
    ByrealWallet,
    CoinbaseWallet,
    OkxOsWallet,
    SelfCustodyWallet,
)


# Per-provider credential schemas. Same shape as
# :class:`nerya.connectors.provider_spec.CredentialField` so the
# dashboard / agent intake flow can render exchange and wallet
# providers with the same renderer. Each field is keyed by the entry
# under ``wallet.<provider>.<field>`` in ``nerya.yml`` (or per-account
# overrides via ``wallet.<provider>.accounts.<id>.<field>``); secret
# values must already be ``vault://`` references — the system never
# stores plaintext.
def _field(
    name: str,
    label: str,
    *,
    kind: str = "secret",
    required: bool = True,
    description: str = "",
    placeholder: str = "",
    sensitive: bool = True,
    vault_scope: str = "wallet",
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "kind": kind,
        "required": required,
        "description": description,
        "placeholder": placeholder,
        "sensitive": sensitive,
        "vault_scope": vault_scope,
    }


def _market_source(
    venue: str,
    canonical: str,
    label: str,
    *,
    market_format: str,
    fetch_method: str,
    description: str = "",
) -> dict[str, Any]:
    return {
        "venue": venue,
        "canonical": canonical,
        "label": label,
        "market_format": market_format,
        "fetch_method": fetch_method,
        "description": description,
    }


def _auth_flow(
    flow_id: str,
    kind: str,
    label: str,
    description: str,
    *,
    docs_url: str = "",
    commands: list[str] | tuple[str, ...] = (),
    stores: list[str] | tuple[str, ...] = (),
    notes: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "id": flow_id,
        "kind": kind,
        "label": label,
        "description": description,
        "docs_url": docs_url,
        "commands": list(commands),
        "stores": list(stores),
        "notes": list(notes),
    }


PROVIDERS: dict[str, dict[str, Any]] = {
    "self_custody": {
        "id": "self_custody",
        "label": "Self-custody (goat-sdk / eth_account / solders)",
        "description": (
            "Hold keys locally, sign swaps via goat-sdk or the eth_account "
            "/ solders fallback. Chain coverage tracks the installed SDK."
        ),
        "install_hint": (
            "Install GOAT packages with npm:@goat-sdk/core and "
            "npm:@goat-sdk/wallet-viem, then install Python fallbacks with "
            "pip install eth-account web3 solders solana."
        ),
        "install_command": "pip install eth-account web3 solders solana",
        "install_alternatives": [
            {
                "label": "GOAT SDK core",
                "command": "npm:@goat-sdk/core",
                "kind": "npm",
                "note": "Installs the GOAT core package in the workspace node-skill area.",
            },
            {
                "label": "GOAT viem wallet adapter",
                "command": "npm:@goat-sdk/wallet-viem",
                "kind": "npm",
                "note": "Installs the GOAT EVM viem wallet adapter for self-custody routing.",
            },
            {
                "label": "Python EVM/Solana fallback",
                "command": "pip install eth-account web3 solders solana",
                "kind": "pip",
                "note": "Keeps the current Python self_custody provider dependency-ready.",
            },
        ],
        "links": {
            "docs": "https://github.com/goat-sdk/goat",
            "config": "wallet.self_custody.{signer_ref, rpc_urls, chains}",
        },
        "runtime": "python",
        "credential_fields": [
            _field(
                "signer_ref", "Signer Vault Ref", kind="public",
                required=False, sensitive=False,
                description=(
                    "Pointer to a workspace signer policy entry. Leave "
                    "blank to keep the wallet read-only."
                ),
                placeholder="local:my_signer",
            ),
            _field(
                "rpc_urls.ethereum", "Ethereum RPC URL", kind="url",
                required=False, sensitive=False,
                placeholder="https://...",
            ),
            _field(
                "rpc_urls.solana", "Solana RPC URL", kind="url",
                required=False, sensitive=False,
                placeholder="https://api.mainnet-beta.solana.com",
            ),
        ],
    },
    "okx_os": {
        "id": "okx_os",
        "label": "OKX Agentic Wallet / Onchain OS",
        "description": (
            "Install the OKX OnchainOS CLI, log in to Agentic Wallet with "
            "email + verification code, then use the local login session "
            "for wallet commands and OnchainOS market data."
        ),
        "install_hint": (
            "Install the checksum-verified OnchainOS release binary, run `onchainos wallet login "
            "<email>` and `onchainos wallet verify <code>`, then store the "
            "local session. Advanced OKX Open API keys are optional and "
            "only used for direct signed API fallback."
        ),
        "install_command": "github-release-bin:okx/onchainos-skills#binary=onchainos",
        "install_alternatives": [
            {
                "label": "OnchainOS CLI release binary",
                "command": "github-release-bin:okx/onchainos-skills#binary=onchainos",
                "kind": "github-release-bin",
                "note": "Downloads the latest official onchainos release asset and verifies checksums.txt.",
            },
            {
                "label": "OnchainOS skills repository",
                "command": "git-repo:https://github.com/okx/onchainos-skills#entry=.codex/INSTALL.md",
                "kind": "git-repo",
                "note": "Installs the official skills/workflows source tree; the release binary is still required for wallet login.",
            },
        ],
        "links": {
            "docs": "https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet",
            "api_access": "https://web3.okx.com/zh-hans/onchainos/dev-docs/home/api-access-and-usage",
            "skills": "https://github.com/okx/onchainos-skills",
            "config": "wallet.okx_os.{account_id, api_project_id, api_key_ref, api_secret_ref, api_passphrase_ref}",
        },
        "runtime": "http",
        "auth_cli": {
            "kind": "binary",
            "binary": "onchainos",
            "install_command": "github-release-bin:okx/onchainos-skills#binary=onchainos",
        },
        "auth_flows": [
            _auth_flow(
                "okx_email_otp",
                "email_otp",
                "OKX Agentic Wallet email verification",
                (
                    "Official quick-start login uses an email address and "
                    "verification code through the OnchainOS CLI. Use the "
                    "returned account id for Agentic Wallet skills."
                ),
                docs_url="https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet",
                commands=[
                    "install OnchainOS CLI release from okx/onchainos-skills",
                    "onchainos wallet login <email>",
                    "onchainos wallet verify <code>",
                    "onchainos wallet status",
                    "onchainos wallet balance",
                ],
                stores=["account_id", "api_project_id"],
                notes=[
                    "For Chinese Mainland email login, OKX documents the +86 locale flow.",
                    "Advanced Open API keys are not part of the default login flow.",
                ],
            ),
        ],
        "market_data_sources": [
            _market_source(
                "okx_onchain",
                "OKX_ONCHAIN",
                "OKX Onchain OS",
                market_format="chain:token",
                fetch_method="get_token_klines",
                description=(
                    "OKX OnchainOS token OHLCV via the installed CLI/login "
                    "session; direct signed API credentials are only an "
                    "advanced fallback."
                ),
            ),
        ],
        "credential_fields": [
            _field(
                "account_id", "OKX Agentic Wallet Account ID", kind="public",
                sensitive=False, required=False,
                description="Account id returned by `onchainos wallet account`.",
                placeholder="0x... or OKX account id",
            ),
            _field(
                "api_project_id", "OKX Project ID", kind="public",
                sensitive=False, required=False,
                description="Project ID used by OKX Agentic Wallet skills and Open API.",
            ),
        ],
        "advanced_credential_fields": [
            _field(
                "api_key", "OKX Open API Key", kind="secret", required=False,
                description="Advanced: only needed for signed OKX Web3 Open API calls.",
            ),
            _field(
                "api_secret", "OKX Open API Secret", kind="secret", required=False,
                description="Advanced: only needed for signed OKX Web3 Open API calls.",
            ),
            _field(
                "api_passphrase", "OKX Open API Passphrase", kind="secret", required=False,
                description="Advanced: only needed for signed OKX Web3 Open API calls.",
            ),
        ],
    },
    "bitget": {
        "id": "bitget",
        "label": "Bitget Wallet Skill / Market API",
        "description": (
            "Use Bitget Wallet's official agent skill for keyless token "
            "actions, with an optional Bitget Wallet Market API key only "
            "when direct signed market-data endpoints are required."
        ),
        "install_hint": (
            "Clone https://github.com/bitget-wallet-ai-lab/bitget-wallet-skill "
            "and follow its README. The official skill path uses built-in "
            "token authentication and does not require a user API key; "
            "developer Market API keys are only for direct K-line calls."
        ),
        "install_command": (
            "git-repo:https://github.com/bitget-wallet-ai-lab/bitget-wallet-skill"
            "#entry=scripts/bitget-wallet-agent-api.py"
        ),
        "install_alternatives": [
            {
                "label": "Official skill repository",
                "command": (
                    "git-repo:https://github.com/bitget-wallet-ai-lab/bitget-wallet-skill"
                    "#entry=scripts/bitget-wallet-agent-api.py"
                ),
                "kind": "git-repo",
                "note": "Clone/update the official Bitget Wallet Skill scripts into the workspace.",
            },
            {
                "label": "Developer Market API",
                "command": "",
                "kind": "manual",
                "note": "Create x-api-key credentials in the Bitget Wallet Web3 portal for signed market endpoints.",
            },
        ],
        "links": {
            "docs": "https://github.com/bitget-wallet-ai-lab/bitget-wallet-skill",
            "skill_docs": "https://web3.bitget.com/bitget-wallet-skill-dist/bitget-wallet-skill.html?lang=en",
            "market_api": "https://web3.bitget.com/en/docs/market/market-price",
            "auth": "https://web3.bitget.com/en/docs/authentication/",
            "config": "wallet.bitget.{skill_path, entry, bitget_token_ref, market_api_key_ref, market_api_secret_ref}",
        },
        "runtime": "node",
        "auth_flows": [
            _auth_flow(
                "bitget_wallet_skill",
                "skill_builtin_token",
                "Bitget Wallet Skill built-in token",
                (
                    "The official Bitget Wallet Skill README says the "
                    "included scripts use built-in token authentication and "
                    "do not require an API key for the default actions."
                ),
                docs_url="https://github.com/bitget-wallet-ai-lab/bitget-wallet-skill",
                commands=[
                    "git clone https://github.com/bitget-wallet-ai-lab/bitget-wallet-skill",
                    "python scripts/bitget-wallet-agent-api.py --action token-price --chain eth --contract <token>",
                    "export BITGET_TOKEN=<optional_custom_token>",
                ],
                stores=["skill_path", "bitget_token_ref"],
                notes=[
                    "BITGET_TOKEN is optional when the built-in token is sufficient.",
                    "Direct Market API K-line calls use the separate x-api-key signature flow.",
                ],
            ),
        ],
        "market_data_sources": [
            _market_source(
                "bitget_onchain",
                "BITGET_ONCHAIN",
                "Bitget Wallet Markets",
                market_format="chain:token",
                fetch_method="get_token_klines",
                description="Bitget Wallet Markets token OHLCV; direct API mode requires x-api-key credentials.",
            ),
        ],
        "credential_fields": [
            _field(
                "skill_path", "Skill Directory", kind="public",
                sensitive=False,
                required=False,
                description="Absolute path to the cloned Bitget Wallet Skill checkout.",
                placeholder="/Users/me/skills/bitget-wallet-skill",
            ),
            _field(
                "entry", "Entry file", kind="public",
                sensitive=False, required=False,
                description="Relative path inside the skill dir (defaults to scripts/bitget-wallet-agent-api.py).",
                placeholder="scripts/bitget-wallet-agent-api.py",
            ),
            _field(
                "bitget_token", "Optional Bitget Skill Token",
                kind="secret", required=False,
                description="Optional BITGET_TOKEN override; leave blank for the official built-in token path.",
            ),
            _field(
                "bitget_api_url", "Optional Bitget Skill API URL",
                kind="url", sensitive=False, required=False,
                placeholder="https://copenapi.bitgetapp.com/v1/wallet/agent-api/swap",
            ),
        ],
        "advanced_credential_fields": [
            _field("market_api_key", "Bitget Wallet Market API Key", kind="secret", required=False),
            _field("market_api_secret", "Bitget Wallet Market API Secret", kind="secret", required=False),
            _field(
                "market_base_url", "Bitget Wallet Market Base URL",
                kind="url", sensitive=False, required=False,
                placeholder="https://bopenapi.bgwapi.io",
            ),
        ],
    },
    "binance_agentic": {
        "id": "binance_agentic",
        "label": "Binance Agentic Wallet (binance-web3 skill)",
        "description": (
            "Wrap the binance-web3 agentic wallet skill from "
            "binance-skills-hub. Login is performed by Binance App "
            "QR/link approval, not by API keys."
        ),
        "install_hint": (
            "Install `@binance/agentic-wallet@1.0.9`, run `baw auth signin --json`, "
            "approve in Binance App, then verify with "
            "`baw auth verify --qrCodeId <id> --json`."
        ),
        "install_command": "npm:@binance/agentic-wallet#version=1.0.9&entry=dist/index.js",
        "install_alternatives": [
            {
                "label": "Binance Agentic Wallet npm CLI",
                "command": "npm:@binance/agentic-wallet#version=1.0.9&entry=dist/index.js",
                "kind": "npm",
            },
            {
                "label": "Binance skills hub source",
                "command": (
                    "node-skill:https://github.com/binance/binance-skills-hub"
                    "#path=skills/binance-web3/binance-agentic-wallet&entry=dist/index.js"
                ),
                "kind": "node-skill",
            },
        ],
        "links": {
            "docs": "https://github.com/binance/binance-skills-hub/tree/main/skills/binance-web3/binance-agentic-wallet",
            "auth": "https://github.com/binance/binance-skills-hub/tree/main/skills/binance-web3/binance-agentic-wallet/references/authentication.md",
            "skill_detail": "https://www.binance.com/en/skills/detail/binance-web3/binance-agentic-wallet",
            "config": "wallet.binance_agentic.{skill_path, entry}",
        },
        "runtime": "node",
        "auth_cli": {
            "kind": "npm",
            "package": "@binance/agentic-wallet",
            "version": "1.0.9",
            "bin": "baw",
            "install_command": "npm:@binance/agentic-wallet#version=1.0.9&entry=dist/index.js",
        },
        "auth_flows": [
            _auth_flow(
                "binance_app_qr",
                "app_qr",
                "Binance App QR/link approval",
                (
                    "The official Binance Agentic Wallet auth flow creates "
                    "a QR/link login request, then the user approves it in "
                    "the Binance App before Nerya uses the local session."
                ),
                docs_url="https://github.com/binance/binance-skills-hub/tree/main/skills/binance-web3/binance-agentic-wallet/references/authentication.md",
                commands=[
                    "baw auth signin --json",
                    "baw auth verify --qrCodeId <qrCodeId> --json",
                    "baw wallet status --json",
                ],
                stores=["sessionPath", "skill_path"],
            ),
        ],
        "market_data_sources": [
            _market_source(
                "binance_alpha",
                "BINANCE_ALPHA",
                "Binance Alpha Market Data",
                market_format="symbol",
                fetch_method="get_market_klines",
                description="Public Binance Alpha token candlesticks.",
            ),
        ],
        "credential_fields": [
            _field(
                "skill_path", "Skill Directory", kind="public",
                sensitive=False,
                description="Absolute path to binance-agentic-wallet inside the cloned hub.",
                placeholder="/Users/me/skills/binance-skills-hub/skills/binance-web3/binance-agentic-wallet",
            ),
            _field(
                "entry", "Entry file", kind="public",
                sensitive=False, required=False,
                description="Relative path inside the skill dir.",
                placeholder="dist/nerya.js",
            ),
        ],
    },
    "coinbase": {
        "id": "coinbase",
        "label": "Coinbase Agentic Wallet / CDP Wallet",
        "description": (
            "Use Coinbase Agentic Wallet email OTP login for end-user wallet "
            "sessions, or CDP SDK credentials for developer/API-backed wallet "
            "operations."
        ),
        "install_hint": (
            "For Agentic Wallet login, install/run the official AWAL CLI and "
            "use `npx awal@2.10.0 auth login <email> --json`, then verify the "
            "email OTP. For CDP SDK operations, install cdp-sdk and store "
            "api_key_name/api_private_key as vault:// refs."
        ),
        "install_command": "npm:awal#version=2.10.0&entry=dist/index.js",
        "install_alternatives": [
            {
                "label": "Agentic Wallet CLI (awal)",
                "command": "npm:awal#version=2.10.0&entry=dist/index.js",
                "kind": "npm",
            },
            {
                "label": "Python SDK (cdp-sdk)",
                "command": "pip install cdp-sdk",
                "kind": "pip",
            },
            {
                "label": "Python AgentKit",
                "command": "pip install coinbase-agentkit",
                "kind": "pip",
            },
            {
                "label": "Node SDK (@coinbase/cdp-sdk via git)",
                "command": "node-skill:https://github.com/coinbase/cdp-sdk",
                "kind": "node-skill",
            },
            {
                "label": "Node SDK (@coinbase/cdp-sdk via npm)",
                "command": "npm:@coinbase/cdp-sdk",
                "kind": "npm",
                "note": "Cleanest install when only the TS SDK is needed.",
            },
        ],
        "links": {
            "docs": "https://github.com/coinbase/agentic-wallet-skills",
            "auth_skill": "https://github.com/coinbase/agentic-wallet-skills/blob/main/skills/authenticate-wallet/SKILL.md",
            "cdp_docs": "https://docs.cdp.coinbase.com/",
            "agentkit": "https://github.com/coinbase/coinbase-agentkit",
            "ts_sdk": "https://github.com/coinbase/cdp-sdk",
            "market_data": "https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles",
            "config": "wallet.coinbase.{agentic_session_path, wallet_address, api_key_name_ref, api_private_key_ref, network_id, skill_path}",
        },
        "runtime": "python_or_node",
        "auth_cli": {
            "kind": "npm",
            "package": "awal",
            "version": "2.10.0",
            "bin": "awal",
            "install_command": "npm:awal#version=2.10.0&entry=dist/index.js",
        },
        "auth_flows": [
            _auth_flow(
                "coinbase_email_otp",
                "email_otp",
                "Coinbase Agentic Wallet email OTP",
                (
                    "The official authenticate-wallet skill uses the AWAL "
                    "CLI: start login with an email address, then verify the "
                    "six-digit OTP from email."
                ),
                docs_url="https://github.com/coinbase/agentic-wallet-skills/blob/main/skills/authenticate-wallet/SKILL.md",
                commands=[
                    "npm install awal@2.10.0",
                    "npx awal@2.10.0 auth login <email> --json",
                    "npx awal@2.10.0 auth verify <otp> --json",
                    "npx awal@2.10.0 status --json",
                ],
                stores=["agentic_session_path", "wallet_address", "network_id"],
            ),
        ],
        "market_data_sources": [
            _market_source(
                "coinbase_wallet",
                "COINBASE_WALLET",
                "Coinbase Public Product Candles",
                market_format="product",
                fetch_method="get_market_klines",
                description="Coinbase public product candles for wallet operators.",
            ),
        ],
        "credential_fields": [
            _field(
                "agentic_session_path", "Agentic Wallet Session Path", kind="public",
                sensitive=False, required=False,
                description="Local session path returned by `awal auth verify` / `awal auth status`.",
            ),
            _field(
                "wallet_address", "Agentic Wallet Address", kind="public",
                sensitive=False, required=False,
                description="Wallet address returned by `awal auth verify`.",
            ),
            _field(
                "network_id", "Network ID", kind="public",
                sensitive=False, required=False,
                description="Default network the wallet operates on.",
                placeholder="base-mainnet",
            ),
        ],
        "advanced_credential_fields": [
            _field(
                "api_key_name", "CDP API Key Name", kind="secret", required=False,
                description="Advanced CDP SDK path: the named API key string from CDP (organizations/...).",
            ),
            _field(
                "api_private_key", "CDP Private Key", kind="secret", required=False,
                description="Advanced CDP SDK path: PEM-encoded private key generated alongside the API key name.",
            ),
            _field(
                "skill_path", "Node Skill Directory", kind="public",
                sensitive=False, required=False,
                description="Optional checkout/package path for the Node CDP skill fallback.",
                placeholder="/Users/me/skills/cdp-sdk",
            ),
        ],
    },
    "byreal": {
        "id": "byreal",
        "label": "Byreal CLMM DEX (Solana)",
        "description": (
            "Install the Byreal CLI (`@byreal-io/byreal-cli`) for the Byreal "
            "concentrated-liquidity DEX on Solana, then expose Solana CLMM "
            "pool OHLCV plus pool/token/overview discovery inside Nerya's "
            "market-data routing. Read-only data needs no wallet; swaps and "
            "CLMM positions use a local keypair from `byreal-cli setup`."
        ),
        "install_hint": (
            "Install with `npm install -g @byreal-io/byreal-cli` (or let Nerya "
            "install it into the workspace). Read-only pools/tokens/overview/"
            "klines work immediately; run `byreal-cli setup` only when you need "
            "wallet-signed swaps or CLMM positions."
        ),
        "install_command": "npm:@byreal-io/byreal-cli#version=0.3.6&entry=dist/index.cjs",
        "install_alternatives": [
            {
                "label": "Byreal CLI npm package",
                "command": "npm:@byreal-io/byreal-cli#version=0.3.6&entry=dist/index.cjs",
                "kind": "npm",
                "note": "Installs the official byreal-cli package into the workspace node-skill area.",
            },
            {
                "label": "Global npm install",
                "command": "",
                "kind": "manual",
                "note": "Run `npm install -g @byreal-io/byreal-cli` to expose byreal-cli on PATH.",
            },
            {
                "label": "Agent skill (skills add)",
                "command": "",
                "kind": "manual",
                "note": "Run `npx skills add byreal-git/byreal-agent-skills` to register the byreal-cli agent skill.",
            },
        ],
        "links": {
            "docs": "https://byreal.io",
            "repo": "https://github.com/byreal-git/byreal-agent-skills",
            "npm": "https://www.npmjs.com/package/@byreal-io/byreal-cli",
            "config": "wallet.byreal.{cli_path, rpc_url, keypair_path}",
        },
        "runtime": "node",
        "auth_cli": {
            "kind": "npm",
            "package": "@byreal-io/byreal-cli",
            "version": "0.3.6",
            "bin": "byreal-cli",
            "install_command": "npm:@byreal-io/byreal-cli#version=0.3.6&entry=dist/index.cjs",
        },
        "auth_flows": [
            _auth_flow(
                "byreal_local_keypair",
                "local_keypair",
                "Byreal local keypair setup",
                (
                    "Read-only pool/token/overview/K-line commands need no "
                    "wallet. For swaps and CLMM positions, run `byreal-cli "
                    "setup` to create or import a Solana keypair stored locally "
                    "at ~/.config/byreal/keys/ with strict 0600 permissions."
                ),
                docs_url="https://github.com/byreal-git/byreal-agent-skills",
                commands=[
                    "npm install -g @byreal-io/byreal-cli",
                    "byreal-cli overview -o json",
                    "byreal-cli pools list --sort-field apr24h -o json",
                    "byreal-cli setup   # only for wallet-signed swaps/positions",
                ],
                stores=["cli_path"],
                notes=[
                    "byreal-cli never transmits private keys; keys are used locally for signing only.",
                    "Token K-lines are per-pool: use market id solana:<poolAddress>.",
                ],
            ),
        ],
        "market_data_sources": [
            _market_source(
                "byreal_onchain",
                "BYREAL_ONCHAIN",
                "Byreal CLMM DEX (Solana) Onchain Data",
                market_format="chain:token",
                fetch_method="get_token_klines",
                description=(
                    "Solana CLMM pool OHLCV via byreal-cli `pools klines`. The "
                    "token field is the Byreal pool address (market id "
                    "solana:<poolAddress>)."
                ),
            ),
        ],
        "credential_fields": [
            _field(
                "cli_path", "Byreal CLI path", kind="public",
                sensitive=False, required=False,
                description="Optional explicit path to the byreal-cli binary or dist/index.cjs.",
                placeholder="/usr/local/bin/byreal-cli",
            ),
            _field(
                "rpc_url", "Solana RPC URL", kind="url",
                sensitive=False, required=False,
                placeholder="https://api.mainnet-beta.solana.com",
            ),
        ],
        "advanced_credential_fields": [
            _field(
                "keypair_path", "Byreal keypair path", kind="public",
                sensitive=False, required=False,
                description=(
                    "Optional path to the local Solana keypair directory "
                    "(default ~/.config/byreal/keys/)."
                ),
            ),
        ],
    },
}


def _capabilities_for(name: str) -> WalletCapabilities | None:
    """Best-effort static capability lookup without a full config.

    Instantiates the provider with an empty config to reach its static
    ``capabilities()`` method. If the provider needs config just to
    build, we swallow the error and return ``None`` — the live
    ``readiness_report`` path is free to retry with the real config.
    """
    try:
        provider = build_provider(name, {})
    except Exception:
        return None
    try:
        return provider.capabilities()
    except Exception:
        return None


def list_providers() -> list[dict[str, Any]]:
    """Return the static provider catalog (not instantiated).

    Each entry is enriched with the provider's static capability
    ceiling so operator UIs can render an honest
    *installed / dependency-ready / execution-ready / experimental*
    matrix without having to actually construct credentials.
    """
    out: list[dict[str, Any]] = []
    for name, entry in PROVIDERS.items():
        item = dict(entry)
        item.setdefault("installed", True)
        caps = _capabilities_for(name)
        item["capabilities"] = caps.to_dict() if caps else None
        item["stability"] = (
            caps.execution_profile if caps is not None else "experimental"
        )
        out.append(item)
    return out


def market_data_sources_for_provider(name: str) -> list[dict[str, Any]]:
    """Return static wallet market-data sources declared by a provider."""

    entry = PROVIDERS.get((name or "").strip().lower()) or {}
    return [dict(row) for row in (entry.get("market_data_sources") or [])]


def list_wallet_market_data_sources() -> list[dict[str, Any]]:
    """Return every wallet-provider-backed market-data source."""

    out: list[dict[str, Any]] = []
    for provider, entry in PROVIDERS.items():
        for row in entry.get("market_data_sources") or []:
            item = dict(row)
            item["provider"] = provider
            out.append(item)
    return out


def resolve_provider_name(name: str) -> str | None:
    """Resolve a wallet provider id or one of its market-source aliases.

    Operator surfaces sometimes show wallet-backed market sources such
    as ``byreal_onchain`` next to normal exchange venues. Those are valid
    candle sources, but account management needs the owning wallet
    provider id (``byreal``) so credential schemas and
    wallet bindings route through the wallet registry.
    """

    candidate = (name or "").strip().lower()
    if not candidate:
        return None
    if candidate in PROVIDERS:
        return candidate
    for provider, entry in PROVIDERS.items():
        for row in entry.get("market_data_sources") or []:
            aliases = {
                str(row.get("venue") or "").strip().lower(),
                str(row.get("canonical") or "").strip().lower(),
            }
            if candidate in aliases:
                return provider
    return None


def _npm_install_root(workspace: Path | None, package: str) -> Path | None:
    if workspace is None:
        return None
    safe = package.replace("@", "").replace("/", "__")
    root = Path(workspace) / "skills" / "_node" / safe
    return root if root.exists() else None


def build_provider(
    name: str,
    cfg: dict[str, Any] | None = None,
    *,
    workspace: Path | None = None,
    vault_passphrase: str | None = None,
) -> WalletProvider:
    """Instantiate the given provider, applying cfg without installing anything."""
    name_l = (name or "").lower()
    cfg = dict(cfg or {})
    if name_l == "self_custody":
        return SelfCustodyWallet(
            signer_ref=cfg.get("signer_ref", ""),
            rpc_urls=dict(cfg.get("rpc_urls") or {}),
            chains=tuple(cfg.get("chains") or SelfCustodyWallet.chains),
            config=cfg,
        )
    if name_l == "okx_os":
        api_key = _resolve(cfg.get("api_key_ref") or cfg.get("api_key"),
                           workspace, vault_passphrase)
        api_secret = _resolve(cfg.get("api_secret_ref") or cfg.get("api_secret"),
                              workspace, vault_passphrase)
        api_pass = _resolve(cfg.get("api_passphrase_ref") or cfg.get("api_passphrase"),
                            workspace, vault_passphrase)
        return OkxOsWallet(
            account_id=str(cfg.get("account_id") or ""),
            api_key=api_key or "",
            api_secret=api_secret or "",
            api_passphrase=api_pass or "",
            api_project_id=str(cfg.get("api_project_id") or ""),
            base_url=str(cfg.get("base_url") or OkxOsWallet.base_url),
            workspace=str(workspace or ""),
            config=cfg,
        )
    if name_l == "bitget":
        market_api_key = _resolve(
            cfg.get("market_api_key_ref") or cfg.get("market_api_key"),
            workspace,
            vault_passphrase,
        )
        market_api_secret = _resolve(
            cfg.get("market_api_secret_ref") or cfg.get("market_api_secret"),
            workspace,
            vault_passphrase,
        )
        return BitgetWalletSkill(
            skill_path=str(cfg.get("skill_path") or ""),
            entry=str(cfg.get("entry") or BitgetWalletSkill.entry),
            repo=str(cfg.get("repo") or BitgetWalletSkill.repo),
            market_api_key=market_api_key or "",
            market_api_secret=market_api_secret or "",
            market_base_url=str(cfg.get("market_base_url") or BitgetWalletSkill.market_base_url),
            config=cfg,
        )
    if name_l == "binance_agentic":
        skill_path = str(cfg.get("skill_path") or "")
        entry = str(cfg.get("entry") or BinanceAgenticWallet.entry)
        if not skill_path:
            install_root = _npm_install_root(workspace, "@binance/agentic-wallet")
            package_entry = (
                Path("node_modules") / "@binance" / "agentic-wallet" / "dist" / "index.js"
            )
            if install_root and (install_root / package_entry).exists():
                skill_path = str(install_root)
                entry = str(package_entry)
        return BinanceAgenticWallet(
            skill_path=skill_path,
            entry=entry,
            repo=str(cfg.get("repo") or BinanceAgenticWallet.repo),
            config=cfg,
        )
    if name_l == "coinbase":
        api_key = _resolve(cfg.get("api_key_name_ref") or cfg.get("api_key_name"),
                            workspace, vault_passphrase)
        api_priv = _resolve(cfg.get("api_private_key_ref") or cfg.get("api_private_key"),
                             workspace, vault_passphrase)
        return CoinbaseWallet(
            api_key_name=api_key or "",
            api_private_key=api_priv or "",
            network_id=str(cfg.get("network_id") or CoinbaseWallet.network_id),
            skill_path=str(cfg.get("skill_path") or ""),
            entry=str(cfg.get("entry") or CoinbaseWallet.entry),
            repo=str(cfg.get("repo") or CoinbaseWallet.repo),
            config=cfg,
        )
    if name_l == "byreal":
        cli_path = str(cfg.get("cli_path") or "")
        if not cli_path:
            install_root = _npm_install_root(workspace, "@byreal-io/byreal-cli")
            if install_root:
                bin_shim = install_root / "node_modules" / ".bin" / "byreal-cli"
                pkg_entry = (
                    install_root
                    / "node_modules"
                    / "@byreal-io"
                    / "byreal-cli"
                    / "dist"
                    / "index.cjs"
                )
                if bin_shim.exists():
                    cli_path = str(bin_shim)
                elif pkg_entry.exists():
                    cli_path = str(pkg_entry)
        return ByrealWallet(
            cli_path=cli_path,
            workspace=str(workspace or ""),
            rpc_url=str(cfg.get("rpc_url") or ""),
            keypair_path=str(cfg.get("keypair_path") or ""),
            config=cfg,
        )
    raise WalletProviderNotFound(
        f"unknown wallet provider {name!r}. Known: {sorted(PROVIDERS)}"
    )


def resolve_active(
    config: dict[str, Any] | None,
    *,
    workspace: Path | None = None,
    vault_passphrase: str | None = None,
) -> tuple[str, WalletProvider | None]:
    """Return (selected-name-or-empty, provider-or-None) based on nerya.yml."""
    wallet_cfg = ((config or {}).get("wallet") or {})
    name = str(wallet_cfg.get("provider") or "").strip().lower()
    if not name:
        return "", None
    provider_cfg = dict((wallet_cfg.get(name) or {}))
    provider = build_provider(name, provider_cfg,
                               workspace=workspace, vault_passphrase=vault_passphrase)
    return name, provider


def list_configured_providers(
    config: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return every wallet binding declared in ``nerya.yml``.

    Nerya now lets operators run *multiple*
    wallet providers in parallel by declaring them under
    ``wallet.providers.<wallet_id>`` (each entry carries a provider id
    and a per-id config). The legacy ``wallet.provider`` key still works
    and is surfaced here as a single ``default`` binding for
    backward compatibility.

    The result is the dict:

    ``{wallet_id, provider, label, config, source}``

    where ``source`` is either ``"providers"`` (new map) or ``"legacy"``
    (old ``wallet.provider`` key). Callers that need to render or
    enumerate available wallets — Settings page, account upsert form,
    accountdashboard — use this method instead of poking
    ``nerya.yml`` directly.
    """

    out: list[dict[str, Any]] = []
    wallet_cfg = dict(((config or {}).get("wallet") or {}))
    providers_map = wallet_cfg.get("providers")
    if isinstance(providers_map, dict):
        for wid, entry in providers_map.items():
            if not isinstance(entry, dict):
                continue
            provider_name = str(entry.get("provider") or "").lower().strip()
            if not provider_name:
                continue
            label = str(entry.get("label") or wid)
            cfg = dict(entry.get("config") or {})
            out.append(
                {
                    "wallet_id": str(wid),
                    "provider": provider_name,
                    "label": label,
                    "config": cfg,
                    "source": "providers",
                }
            )
    legacy_provider = str(wallet_cfg.get("provider") or "").lower().strip()
    if legacy_provider:
        legacy_cfg = dict((wallet_cfg.get(legacy_provider) or {}))
        # Don't duplicate: if the legacy provider has the same id as a
        # new map entry, we keep the new map entry (operators typically
        # add the new declaration first then drop the legacy key).
        if not any(b["wallet_id"] == legacy_provider for b in out):
            out.append(
                {
                    "wallet_id": legacy_provider,
                    "provider": legacy_provider,
                    "label": f"{legacy_provider} (default)",
                    "config": legacy_cfg,
                    "source": "legacy",
                }
            )
    return out


def resolve_for_account(
    config: dict[str, Any] | None,
    account_id: str,
    wallet_id: str | None,
    *,
    workspace: Path | None = None,
    vault_passphrase: str | None = None,
) -> tuple[str, WalletProvider | None, str]:
    """Return ``(wallet_id, provider_or_None, source)`` for an account row.

    Resolution order:

    1. If ``wallet_id`` is provided and matches an entry in
       ``wallet.providers``, use that.
    2. If ``wallet_id`` matches a legacy ``wallet.<id>`` block, use that.
    3. Otherwise fall back to the legacy ``wallet.provider`` key.

    ``account_id`` is currently informational only (it shows up in
    error messages and journals). Per-account credential overrides
    live inside the wallet binding's ``config.accounts.<account_id>``
    so multiple accounts can share a provider with different sub-keys.
    """

    bindings = list_configured_providers(config)
    selected: dict[str, Any] | None = None
    if wallet_id:
        for b in bindings:
            if b["wallet_id"] == wallet_id:
                selected = b
                break
    if selected is None and bindings:
        selected = bindings[0]
    if selected is None:
        return "", None, "none"
    cfg = dict(selected["config"])
    per_account = (cfg.pop("accounts", None) or {}).get(account_id)
    if isinstance(per_account, dict):
        cfg.update(per_account)
    try:
        provider = build_provider(
            selected["provider"],
            cfg,
            workspace=workspace,
            vault_passphrase=vault_passphrase,
        )
    except WalletProviderNotFound:
        return selected["wallet_id"], None, selected["source"]
    return selected["wallet_id"], provider, selected["source"]


def resolve_for_strategy(
    config: dict[str, Any] | None,
    strategy_id: str,
    *,
    workspace: Path | None = None,
    vault_passphrase: str | None = None,
) -> tuple[str, WalletProvider | None, str]:
    """Return ``(provider_name, provider_or_None, source)`` for a strategy.

    ``source`` is ``"strategy"`` when the strategy.yml carries an explicit
    ``wallet_id``, ``"global"`` when it falls through to ``wallet.provider``
    in nerya.yml, and ``"none"`` when neither is set. ``strategy_id`` must
    already be sanitised by the caller — we don't touch paths beyond reading
    the strategy yaml.

    Per-strategy config overrides live at ``wallet.<provider>.strategies.<sid>``
    in nerya.yml and are merged on top of the provider's global config
    (so e.g. different strategies can use different OKX sub-keys while
    sharing the provider selection).
    """
    if not workspace:
        return "", None, "none"
    sid = str(strategy_id or "").strip()
    wallet_cfg = dict(((config or {}).get("wallet") or {}))
    strat_path = Path(workspace) / "strategies" / sid / "strategy.yml"

    strategy_wallet_id: str | None = None
    if sid and strat_path.exists():
        try:
            from ..core import yaml_io
            doc = yaml_io.load(strat_path, default={}) or {}
            val = doc.get("wallet_id")
            if val:
                strategy_wallet_id = str(val).strip().lower() or None
        except Exception:
            strategy_wallet_id = None

    selected: dict[str, Any] | None = None
    if strategy_wallet_id:
        for binding in list_configured_providers(config):
            if binding["wallet_id"] == strategy_wallet_id:
                selected = binding
                break
        if selected is None:
            name = strategy_wallet_id
            provider_cfg = dict((wallet_cfg.get(name) or {}))
            source = "strategy"
        else:
            name = str(selected["provider"])
            provider_cfg = dict(selected.get("config") or {})
            source = "strategy"
    else:
        name = str(wallet_cfg.get("provider") or "").strip().lower()
        provider_cfg = dict((wallet_cfg.get(name) or {}))
        source = "global" if name else "none"
    if not name:
        return "", None, "none"

    per_strategy = (
        dict((provider_cfg.pop("strategies", None) or {}).get(sid) or {})
        if sid else {}
    )
    if per_strategy:
        provider_cfg.update(per_strategy)

    provider = build_provider(
        name, provider_cfg,
        workspace=workspace, vault_passphrase=vault_passphrase,
    )
    return name, provider, source


def _meaningful_wallet_config(cfg: dict[str, Any]) -> bool:
    """Return True when a legacy provider block contains real config.

    Default ``nerya.yml`` includes placeholder blocks such as
    ``wallet.bitget.entry: dist/nerya.js``. Those placeholders should not
    mask a real ``wallet.providers.<wallet_id>`` binding.
    """

    for key, value in (cfg or {}).items():
        if value in (None, "", [], {}):
            continue
        if key == "entry" and value in {
            "dist/nerya.js",
            "dist/index.js",
            "scripts/bitget-wallet-agent-api.py",
        }:
            continue
        return True
    return False


def readiness_report(
    config: dict[str, Any] | None,
    *,
    workspace: Path | None = None,
    vault_passphrase: str | None = None,
) -> list[dict[str, Any]]:
    """Return a rendered catalog with live readiness + capability status.

    Each entry carries:

    - ``installed`` — always ``True`` for providers that ship with Nerya
      (the catalog is static); surfaced here so the UI never has to
      re-derive it.
    - ``readiness`` — live *dependency-ready* signal from the provider.
    - ``capabilities`` — the static *execution-ready* ceiling
      (per-method real/partial/experimental/stub) rolled up into
      ``capabilities.execution_profile``.
    - ``stability`` — shortcut for ``capabilities.execution_profile`` so
      UIs can render a single truthful pill per provider.
    """
    out: list[dict[str, Any]] = []
    bindings = list_configured_providers(config)
    for name in PROVIDERS:
        static = dict(PROVIDERS[name])
        static.setdefault("installed", True)
        wallet_cfg = ((config or {}).get("wallet") or {})
        cfg = dict((wallet_cfg.get(name) or {}))
        binding_cfg: dict[str, Any] | None = None
        for binding in bindings:
            if binding.get("provider") == name:
                binding_cfg = dict(binding.get("config") or {})
                static["configured_wallet_id"] = binding.get("wallet_id")
                break
        if binding_cfg is not None:
            cfg = binding_cfg
        elif not _meaningful_wallet_config(cfg):
            cfg = {}
        caps: WalletCapabilities | None = None
        try:
            p = build_provider(name, cfg, workspace=workspace,
                                vault_passphrase=vault_passphrase)
            r: WalletReadiness = p.readiness()
            static["readiness"] = r.to_dict()
            try:
                caps = p.capabilities()
            except Exception:
                caps = None
        except Exception as exc:
            static["readiness"] = WalletReadiness(
                provider=name, ready=False, reason=str(exc),
            ).to_dict()
            caps = _capabilities_for(name)
        static["capabilities"] = caps.to_dict() if caps else None
        static["stability"] = (
            caps.execution_profile if caps is not None else "experimental"
        )
        out.append(static)
    return out


# ------------------------------------------------------------------
def _resolve(
    value: str | None,
    workspace: Path | None,
    vault_passphrase: str | None,
) -> str | None:
    if not value:
        return None
    v = str(value)
    if not v.startswith("vault://") or not workspace:
        return v
    try:
        from ..security.secrets import SecretVault
        vp = Path(workspace) / "vault" / "secrets.enc"
        if not vp.exists():
            return None
        s = SecretVault.open(vp, passphrase=vault_passphrase)
        return s.resolve(v.split("vault://", 1)[-1], required_scope="wallet")
    except Exception:
        return None


# Convenience alias for older code paths.
class WalletRegistry:
    """Tiny stateful wrapper so consumers can cache one provider per workspace."""

    def __init__(self, workspace: Path | None = None,
                 vault_passphrase: str | None = None):
        self.workspace = workspace
        self.vault_passphrase = vault_passphrase
        self._cached: dict[str, WalletProvider] = {}

    def get(self, name: str, cfg: dict[str, Any] | None = None) -> WalletProvider:
        if name in self._cached:
            return self._cached[name]
        p = build_provider(name, cfg, workspace=self.workspace,
                            vault_passphrase=self.vault_passphrase)
        self._cached[name] = p
        return p

    def invalidate(self, name: str | None = None) -> None:
        if name:
            self._cached.pop(name, None)
        else:
            self._cached.clear()


__all__ = [
    "PROVIDERS", "WalletRegistry", "build_provider",
    "list_providers", "readiness_report", "resolve_active",
    "resolve_for_strategy", "resolve_for_account",
    "list_configured_providers", "market_data_sources_for_provider",
    "list_wallet_market_data_sources", "resolve_provider_name",
]

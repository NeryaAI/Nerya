"""Reviewed protocol calldata and exact receipt decoders. No signing or broadcast here."""
from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

from ..contracts import FinancialError

ZERO = "0x" + "00" * 20
POOL_KEY = "(address,address,uint24,int24,address)"


def require(ok, code):
    if not ok:
        raise FinancialError(code, 422)


def uint(value, bits=256, *, zero=False):
    require(type(value) is int, "base_units_must_be_integer")
    require((0 if zero else 1) <= value < 2**bits,
            "integer_out_of_range")
    return value


def address(value):
    require(isinstance(value, str) and len(value) == 42
            and value.startswith("0x"), "invalid_address")
    try:
        result = to_checksum_address(value)
    except (ValueError, TypeError):
        raise FinancialError("invalid_address", 422)
    require(result != ZERO, "native_currency_not_enabled")
    return result


def calldata(signature, types, values):
    return "0x" + (
        keccak(text=signature)[:4] + encode(types, values)
    ).hex()


def transaction(owner, target, data):
    return {
        "from": address(owner),
        "to": address(target),
        "value": "0x0",
        "data": data,
    }


def ticks(lower, upper, spacing):
    require(
        type(lower) is int and type(upper) is int
        and type(spacing) is int and 0 < spacing <= 32767
        and -887272 <= lower < upper <= 887272
        and lower % spacing == upper % spacing == 0,
        "invalid_tick_range",
    )


def aave_call(pool, owner, operation, asset, amount_base):
    owner, asset = address(owner), address(asset)
    quantity = uint(amount_base)
    require(quantity != 2**256 - 1, "unbounded_aave_amount")

    if operation == "supply":
        data = calldata(
            "supply(address,uint256,address,uint16)",
            ["address", "uint256", "address", "uint16"],
            [asset, quantity, owner, 0],
        )
    elif operation == "withdraw":
        data = calldata(
            "withdraw(address,uint256,address)",
            ["address", "uint256", "address"],
            [asset, quantity, owner],
        )
    elif operation == "borrow":
        data = calldata(
            "borrow(address,uint256,uint256,uint16,address)",
            ["address", "uint256", "uint256", "uint16", "address"],
            [asset, quantity, 2, 0, owner],
        )
    elif operation == "repay":
        data = calldata(
            "repay(address,uint256,uint256,address)",
            ["address", "uint256", "uint256", "address"],
            [asset, quantity, 2, owner],
        )
    else:
        raise FinancialError("unsupported_aave_operation", 422)

    return transaction(owner, pool, data)


def v3_call(manager, owner, operation, p, *, spacing):
    owner = address(owner)

    if operation == "mint":
        a, b = address(p["token0"]), address(p["token1"])
        require(int(a, 16) < int(b, 16), "tokens_must_be_sorted")
        ticks(p["tick_lower"], p["tick_upper"], spacing)
        fee = uint(p["fee"], 24, zero=True)
        require(fee <= 1_000_000, "invalid_pool_fee")

        mx = [uint(p[k], zero=True)
              for k in ("amount0_max", "amount1_max")]
        mn = [uint(p[k], zero=True)
              for k in ("amount0_min", "amount1_min")]
        require(any(mx) and all(x <= y for x, y in zip(mn, mx)),
                "invalid_amount_bounds")

        typ = (
            "(address,address,uint24,int24,int24,"
            "uint256,uint256,uint256,uint256,address,uint256)"
        )
        data = calldata(
            "mint(" + typ + ")", [typ],
            [(a, b, fee, p["tick_lower"], p["tick_upper"],
              *mx, *mn, owner, uint(p["deadline"]))],
        )

    elif operation == "increase":
        mx = [uint(p[k], zero=True)
              for k in ("amount0_max", "amount1_max")]
        mn = [uint(p[k], zero=True)
              for k in ("amount0_min", "amount1_min")]
        require(any(mx) and all(x <= y for x, y in zip(mn, mx)),
                "invalid_amount_bounds")

        typ = "(uint256,uint256,uint256,uint256,uint256,uint256)"
        data = calldata(
            "increaseLiquidity(" + typ + ")", [typ],
            [(uint(p["token_id"]), *mx, *mn,
              uint(p["deadline"]))],
        )

    elif operation in {"decrease_collect", "collect"}:
        typ = "(uint256,address,uint128,uint128)"
        collect = calldata(
            "collect(" + typ + ")", [typ],
            [(uint(p["token_id"]), owner,
              uint(p["collect0_max"], 128, zero=True),
              uint(p["collect1_max"], 128, zero=True))],
        )

        if operation == "collect":
            data = collect
        else:
            typ = "(uint256,uint128,uint256,uint256,uint256)"
            decrease = calldata(
                "decreaseLiquidity(" + typ + ")", [typ],
                [(uint(p["token_id"]), uint(p["liquidity"], 127),
                  uint(p["amount0_min"], zero=True),
                  uint(p["amount1_min"], zero=True),
                  uint(p["deadline"]))],
            )
            data = calldata(
                "multicall(bytes[])", ["bytes[]"],
                [[bytes.fromhex(decrease[2:]),
                  bytes.fromhex(collect[2:])]],
            )
    else:
        raise FinancialError("unsupported_v3_operation", 422)

    return transaction(owner, manager, data)


def v4_call(manager, owner, operation, key, p):
    owner = address(owner)
    a, b, fee, spacing, hooks = key
    a, b = address(a), address(b)
    require(int(a, 16) < int(b, 16), "tokens_must_be_sorted")
    fee = uint(fee, 24, zero=True)
    require(
        fee <= 1_000_000
        and isinstance(hooks, str) and hooks.lower() == ZERO,
        "only_static_unhooked_pools",
    )
    ticks(p["tick_lower"], p["tick_upper"], spacing)
    key = (a, b, fee, spacing, ZERO)

    if operation == "mint":
        params = encode(
            [POOL_KEY, "int24", "int24", "uint256",
             "uint128", "uint128", "address", "bytes"],
            [key, p["tick_lower"], p["tick_upper"],
             uint(p["liquidity"], 127),
             uint(p["amount0_max"], 128, zero=True),
             uint(p["amount1_max"], 128, zero=True), owner, b""],
        )
        action = 0x02

    elif operation in {"increase", "decrease", "collect"}:
        adding = operation == "increase"
        liquidity = (
            0 if operation == "collect"
            else uint(p["liquidity"], 127)
        )
        x = uint(p["amount0_max" if adding else "amount0_min"],
                 128, zero=True)
        y = uint(p["amount1_max" if adding else "amount1_min"],
                 128, zero=True)
        require(operation != "collect" or x == y == 0,
                "collect_uses_zero_minima")
        params = encode(
            ["uint256", "uint256", "uint128", "uint128", "bytes"],
            [uint(p["token_id"]), liquidity, x, y, b""],
        )
        action = 0x00 if adding else 0x01
    else:
        raise FinancialError("unsupported_v4_operation", 422)

    if operation in {"mint", "increase"}:
        # Settle debts or return fee credits independently for each currency.
        actions = bytes([action, 0x12, 0x12])  # CLOSE_CURRENCY
        packed = [
            params, encode(["address"], [a]),
            encode(["address"], [b]),
        ]
    else:
        actions = bytes([action, 0x11])  # TAKE_PAIR
        packed = [
            params,
            encode(["address", "address", "address"], [a, b, owner]),
        ]

    unlock_data = encode(["bytes", "bytes[]"], [actions, packed])
    return transaction(
        owner, manager,
        calldata(
            "modifyLiquidities(bytes,uint256)",
            ["bytes", "uint256"],
            [unlock_data, uint(p["deadline"])],
        ),
    )


def permit2_approval(permit2, owner, token, manager,
                     amount_base, expiration):
    quantity = uint(amount_base, 160,zero=True)
    require(quantity != 2**160 - 1, "unlimited_permit2_approval")
    return transaction(
        owner, permit2,
        calldata(
            "approve(address,address,uint160,uint48)",
            ["address", "address", "uint160", "uint48"],
            [address(token), address(manager), quantity,
             uint(expiration, 48)],
        ),
    )

def topic_address(value):
    if isinstance(value,str) and value.lower()==ZERO:return "0x"+"0"*64
    return "0x" + encode(["address"], [address(value)]).hex()


def matching_event(logs, emitter, signature, indexed, types):
    topic0 = "0x" + keccak(text=signature).hex()
    matches = []

    for log in logs:
        topics = log.get("topics") or []
        if (
            log.get("removed")
            or log.get("address", "").lower() != emitter.lower()
            or len(topics) != len(indexed) + 1
            or topics[0].lower() != topic0.lower()
        ):
            continue
        if any(
            expected is not None
            and actual.lower() != expected.lower()
            for actual, expected in zip(topics[1:], indexed)
        ):
            continue
        matches.append(
            (log, decode(types, bytes.fromhex(log["data"][2:])))
        )

    require(len(matches) == 1,
            "protocol_receipt_event_missing_or_ambiguous")
    return matches[0]


def aave_receipt(logs, pool, owner, operation, asset, maximum_base):
    asset_topic = topic_address(asset)
    owner_topic = topic_address(owner)

    if operation == "supply":
        sig = "Supply(address,address,address,uint256,uint16)"
        ix = [asset_topic, owner_topic, "0x" + "00" * 32]
        types = ["address", "uint256"]
    elif operation == "borrow":
        sig = "Borrow(address,address,address,uint256,uint8,uint256,uint16)"
        ix = [asset_topic, owner_topic, "0x" + "00" * 32]
        types = ["address", "uint256", "uint8", "uint256"]
    elif operation == "withdraw":
        sig = "Withdraw(address,address,address,uint256)"
        ix = [asset_topic, owner_topic, owner_topic]
        types = ["uint256"]
    elif operation == "repay":
        sig = "Repay(address,address,address,uint256,bool)"
        ix = [asset_topic, owner_topic, owner_topic]
        types = ["uint256", "bool"]
    else:
        raise FinancialError("unsupported_aave_operation", 422)

    log, values = matching_event(logs, pool, sig, ix, types)

    if operation in {"supply", "borrow"}:
        require(values[0].lower() == owner.lower(),
                "aave_initiator_mismatch")
        quantity = values[1]
        if operation == "borrow":
            require(values[2] == 2, "aave_debt_mode_mismatch")
    else:
        quantity = values[0]
        if operation == "repay":
            require(values[1] is False,
                    "unexpected_atoken_repayment")

    require(0 < quantity <= maximum_base, "aave_quantity_mismatch")
    require(operation == "repay" or quantity == maximum_base,
            "aave_quantity_mismatch")
    return {
        "amount_base": str(quantity),
        "log_index": int(log["logIndex"], 16),
    }


def nft_mint_id(logs, manager, owner):
    log, _ = matching_event(
        logs, manager, "Transfer(address,address,uint256)",
        ["0x" + "00" * 32, topic_address(owner), None], [],
    )
    return int(log["topics"][3], 16)


def v4_receipt(logs, pool_manager, position_manager, key, token_id,
               lower, upper, delta):
    pool_id = "0x" + keccak(encode([POOL_KEY], [key])).hex()
    log, values = matching_event(
        logs, pool_manager,
        "ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)",
        [pool_id, topic_address(position_manager)],
        ["int24", "int24", "int256", "bytes32"],
    )
    require(
        values == (lower, upper, delta, token_id.to_bytes(32, "big")),
        "v4_position_event_mismatch",
    )
    return {
        "token_id": str(token_id),
        "liquidity_delta": str(delta),
        "pool_id": pool_id,
        "log_index": int(log["logIndex"], 16),
    }


def v3_liquidity_receipt(logs, manager, token_id, increasing, *,
                         minimum_liquidity, exact_liquidity=None,
                         minima=(0, 0), maxima=None):
    name = "IncreaseLiquidity" if increasing else "DecreaseLiquidity"
    log, values = matching_event(
        logs, manager,
        name + "(uint256,uint128,uint256,uint256)",
        ["0x" + encode(["uint256"], [token_id]).hex()],
        ["uint128", "uint256", "uint256"],
    )
    liquidity, a, b = values
    require(
        liquidity >= minimum_liquidity
        and (exact_liquidity is None or liquidity == exact_liquidity)
        and a >= minima[0] and b >= minima[1]
        and (maxima is None or (a <= maxima[0] and b <= maxima[1])),
        "v3_liquidity_event_mismatch",
    )
    return {
        "token_id": str(token_id),
        "liquidity_delta": str(liquidity if increasing else -liquidity),
        "amount0_base": str(a),
        "amount1_base": str(b),
        "log_index": int(log["logIndex"], 16),
    }


def v3_collect_receipt(logs, manager, owner, token_id, maxima):
    log, values = matching_event(
        logs, manager, "Collect(uint256,address,uint256,uint256)",
        ["0x" + encode(["uint256"], [token_id]).hex()],
        ["address", "uint256", "uint256"],
    )
    recipient, a, b = values
    require(
        recipient.lower() == owner.lower()
        and a <= maxima[0] and b <= maxima[1],
        "v3_collect_event_mismatch",
    )
    return {
        "amount0_base": str(a),
        "amount1_base": str(b),
        "log_index": int(log["logIndex"], 16),
    }

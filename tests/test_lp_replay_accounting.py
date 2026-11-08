"""Synthetic arithmetic only, not provider history or execution acceptance."""
from decimal import Decimal, Inexact, ROUND_FLOOR, localcontext

import pytest

from nerya.skills.builtin.backtest.scripts.lp_accounting import (
    ConcentratedLiquidityLedger, Q128, UINT256, fee_growth_inside,
)


def ledger(**kwargs):
    return ConcentratedLiquidityLedger(**(dict(token0_units='1000', token1_units='1000',
        decimals=[6, 6], lower=-100, upper=100, action_cost_token1='0') | kwargs))


def observe(book, block=1, growth=0, **kwargs):
    return book.observe(**(dict(block_number=block, sqrt_price_x96=str(2**96), tick=0,
        global_growth=[str(growth), str(growth)], lower_outside=['0', '0'],
        upper_outside=['0', '0'], initialized=[True, True]) | kwargs))


def test_fee_growth_boundaries_and_uint256_wrap():
    args = dict(lower=-100, upper=100, global_growth=1000, lower_outside=100, upper_outside=200)
    assert fee_growth_inside(tick=0, **args) == 700
    assert fee_growth_inside(tick=-100, **args) == 700
    assert fee_growth_inside(tick=100, **args) == 100
    assert fee_growth_inside(tick=-101, **args) == UINT256 - 100
    assert fee_growth_inside(tick=0, lower=-100, upper=100, global_growth=0, lower_outside=1, upper_outside=1) == UINT256 - 2


def test_enter_remove_conserves_units_and_cannot_create_money():
    book = ledger()
    initial = observe(book)
    entered = book.enter()
    assert int(entered['liquidity']) > 0
    assert all(Decimal(v) >= 0 for v in entered['cash_units'])
    assert Decimal(entered['modeled_value_token1']) == Decimal(initial['modeled_value_token1'])
    final = book.remove()
    assert final['liquidity'] == '0'
    assert list(map(Decimal, final['cash_units'])) == [Decimal(1000), Decimal(1000)]
    assert final['performance_evidence'] is False
    assert final['financial_execution_allowed'] is False


def test_fees_accrue_once_per_block_on_existing_liquidity_only():
    book = ledger()
    observe(book, growth=5 * Q128)
    assert book.snapshot()['owed_base'] == ['0', '0']
    book.enter()
    size = book.liquidity
    row = observe(book, block=2, growth=6 * Q128)
    assert list(map(int, row['owed_base'])) == [size, size]
    with pytest.raises(ValueError, match='increasing'):
        observe(book, block=2, growth=7 * Q128)
    assert list(map(int, book.snapshot()['owed_base'])) == [size, size]
    book.collect()
    observe(book, block=3, growth=6 * Q128)
    assert book.snapshot()['owed_base'] == ['0', '0']


def test_uninitialized_boundary_stops_without_mutating_position():
    book = ledger()
    observe(book)
    book.enter()
    before = book.snapshot()
    with pytest.raises(ValueError, match='not initialized'):
        book.observe(block_number=2, sqrt_price_x96=str(2**96), tick=0,
            global_growth=['1', '1'], lower_outside=['0', '0'],
            upper_outside=['0', '0'], initialized=[False, True])
    assert book.snapshot() == before


def test_explicit_costs_reduce_cash_and_negative_inventory_cannot_be_hidden():
    book = ConcentratedLiquidityLedger(token0_units='1', token1_units='1',
        decimals=[6, 6], lower=-100, upper=100, action_cost_token1='2')
    observe(book)
    before = book.snapshot()
    with pytest.raises(ValueError, match='insufficient'):
        book.enter()
    assert book.snapshot() == before


@pytest.mark.parametrize('value', [True, -1, 0.5, 'NaN', 'Infinity', '-1', '9' * 200])
def test_invalid_amounts_fail_closed(value):
    with pytest.raises(ValueError):
        ConcentratedLiquidityLedger(token0_units=value, token1_units='1',
            decimals=[6, 6], lower=-100, upper=100, action_cost_token1='0')


def test_no_action_without_history():
    book = ledger()
    for action in (book.enter, book.remove, book.collect, book.snapshot):
        with pytest.raises(ValueError, match='observation'):
            action()


def test_action_cost_is_preserved_across_extreme_decimal_scales():
    book = ConcentratedLiquidityLedger(token0_units='0', token1_units='1e60',
        decimals=[0, 0], lower=-100, upper=100, action_cost_token1='1e-60')
    observe(book)
    result = book.collect()
    with localcontext() as ctx:
        ctx.prec = 200
        assert Decimal('1e60') - Decimal(result['cash_units'][1]) == Decimal('1e-60')


@pytest.mark.parametrize('sqrt', [1, 4295128738,
    1461446703485210103287273052203988822378723970342, 2**160 - 1])
def test_out_of_protocol_sqrt_price_is_rejected_atomically(sqrt):
    book = ledger()
    observe(book)
    book.enter()
    before = book.snapshot()
    with pytest.raises(ValueError, match='range state'):
        book.observe(block_number=2, sqrt_price_x96=str(sqrt), tick=0,
            global_growth=[str(Q128), str(Q128)], lower_outside=['0', '0'],
            upper_outside=['0', '0'], initialized=[True, True])
    assert book.snapshot() == before


def test_observation_sampling_does_not_discard_fractional_fee_growth():
    books = [ledger(), ledger()]
    for book in books:
        observe(book)
        book.enter()
    # Each half earns less than one base unit; together they earn one.
    half = Q128 // (2 * books[0].liquidity) + 1
    observe(books[0], block=2, growth=half)
    assert books[0].snapshot()['owed_base'] == ['0', '0']
    sampled = observe(books[0], block=3, growth=2 * half)
    direct = observe(books[1], block=3, growth=2 * half)
    assert sampled['owed_base'] == direct['owed_base'] == ['1', '1']


def test_price_and_fee_tick_must_describe_the_same_pool_state():
    book = ledger()
    observe(book)
    before = book.snapshot()
    with pytest.raises(ValueError, match='tick.*price'):
        book.observe(block_number=2, sqrt_price_x96=str(2**96), tick=100,
            global_growth=['0', '0'], lower_outside=['0', '0'],
            upper_outside=['0', '0'], initialized=[True, True])
    assert book.snapshot() == before


def test_exact_lower_boundary_needs_only_token0_inventory():
    book = ConcentratedLiquidityLedger(token0_units='1', token1_units='0',
        decimals=[0, 0], lower=1, upper=2, action_cost_token1='0')
    book.observe(block_number=1, sqrt_price_x96='79232123823359799118286999568',
        tick=1, global_growth=['0', '0'], lower_outside=['0', '0'],
        upper_outside=['0', '0'], initialized=[True, True])
    entered = book.enter()
    assert int(entered['liquidity']) > 0
    assert Decimal(entered['principal_units'][1]) == 0
    assert list(map(Decimal, book.remove()['cash_units'])) == [Decimal(1), Decimal(0)]


def test_fee_accrual_wraps_exactly_without_ambient_decimal_rounding():
    book = ledger()
    observe(book, growth=UINT256 - Q128 // 2)
    book.enter()
    size = book.liquidity
    with localcontext() as ctx:
        ctx.prec = 6
        row = observe(book, block=2, growth=Q128 // 2)
    assert row['owed_base'] == [str(size), str(size)]
    book.collect()
    assert observe(book, block=3, growth=Q128 // 2)['owed_base'] == ['0', '0']


def test_token1_valuation_and_base_fee_conversion_with_unequal_decimals():
    book = ledger(token0_units='1000000', token1_units='0.000004',
        decimals=[6, 18], lower=13000, upper=14000)
    first = observe(book, sqrt_price_x96=str(2 * 2**96), tick=13863)
    assert Decimal(first['price_token1']) == Decimal('4e-12')
    assert Decimal(first['modeled_value_token1']) == Decimal('0.000008')
    book.enter()
    before = book.snapshot()
    row = observe(book, block=2, sqrt_price_x96=str(2 * 2**96), tick=13863,
        global_growth=[str(Q128), str(2 * Q128)])
    assert row['owed_base'] == [str(book.liquidity), str(2 * book.liquidity)]
    with localcontext() as ctx:
        ctx.prec = 512
        # One base token0 is 1e-6 units, worth 4e-18 token1;
        # two base token1 add 2e-18 token1 per unit of liquidity.
        assert Decimal(row['fees_value_token1']) == Decimal(book.liquidity) * Decimal('6e-18')
        collected = book.collect()
        assert [Decimal(collected['cash_units'][i]) - Decimal(before['cash_units'][i])
                for i in (0, 1)] == [Decimal(book.liquidity) * Decimal('1e-6'),
                                    Decimal(book.liquidity) * Decimal('2e-18')]
    assert collected['owed_base'] == ['0', '0']
    assert collected['performance_evidence'] is False
    assert collected['financial_execution_allowed'] is False


@pytest.mark.parametrize('bad', [
    {'block_number': True}, {'block_number': 1},
    {'sqrt_price_x96': True}, {'tick': False}, {'tick': 887273},
    {'initialized': [True, 1]}, {'global_growth': ['0', str(UINT256)]},
    {'lower_outside': ['0', '-1']}, {'upper_outside': ['0']},
])
def test_invalid_observations_preserve_all_future_fee_accounting(bad):
    book = ledger()
    observe(book)
    book.enter()
    before = book.snapshot()
    with pytest.raises(ValueError):
        observe(book, block=2, growth=Q128, **bad)
    assert book.snapshot() == before
    assert observe(book, block=2, growth=Q128)['owed_base'] == [str(book.liquidity)] * 2


def test_unfunded_collect_and_remove_do_not_swap_token0_or_destroy_fees():
    book = ledger(token0_units='1', token1_units='1', decimals=[0, 0],
        lower=0, upper=100, action_cost_token1='1')
    observe(book)
    book.enter()
    observe(book, block=2, global_growth=[str(Q128), '0'])
    before = book.snapshot()
    assert int(before['owed_base'][0]) > 0
    for action in (book.collect, book.remove):
        with pytest.raises(ValueError, match='insufficient token1'):
            action()
        assert book.snapshot() == before
    # New token1 fees can fund the cost, and the failed calls lost no accrual.
    observe(book, block=3, global_growth=[str(Q128), str(Q128)])
    removed = book.remove()
    assert removed['liquidity'] == '0'
    assert Decimal(removed['cash_units'][0]) == 1 + int(before['liquidity'])
    assert Decimal(removed['cash_units'][1]) == int(before['liquidity']) - 1


def test_entry_cannot_auto_swap_one_sided_inventory_inside_range():
    book = ledger(token0_units='1000', token1_units='0')
    before = observe(book)
    with pytest.raises(ValueError, match='cannot fund'):
        book.enter()
    assert book.snapshot() == before


@pytest.mark.parametrize('tick,sqrt', [
    (-887272, 4295128739),
    (-1, 79224201403219477170569942574),
    (0, 2**96), (1, 79232123823359799118286999568),
    (887271, 1461446703485210103287273052203988822378723970341),
])
def test_tickmath_known_vectors_and_downward_boundary_convention(tick, sqrt):
    book = ledger()
    assert observe(book, tick=tick, sqrt_price_x96=str(sqrt))['block_number'] == 1
    if tick in (-1, 0, 1):
        assert observe(book, block=2, tick=tick - 1, sqrt_price_x96=str(sqrt))['block_number'] == 2
        with pytest.raises(ValueError, match='tick.*price'):
            observe(book, block=3, tick=tick, sqrt_price_x96=str(sqrt - 1))


@pytest.mark.parametrize('amount', ['1e-121', '1e-999999999', '1.000000000000000000000000000000000000000000000000000000000001e60'])
def test_decimal_scale_and_upper_magnitude_are_bounded(amount):
    with pytest.raises(ValueError, match='bounds'):
        ledger(token0_units=amount)


def test_uint128_liquidity_limit_rejects_entry_without_debiting_cost():
    book = ledger(token0_units='1e60', token1_units='1e60', action_cost_token1='1')
    before = observe(book)
    with pytest.raises(ValueError, match='bounded liquidity'):
        book.enter()
    assert book.snapshot() == before


def test_arithmetic_is_independent_of_callers_decimal_context():
    def run():
        book = ledger()
        observe(book)
        book.enter()
        observe(book, block=2, growth=Q128)
        book.collect()
        return book.remove()

    expected = run()
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.Emax = 20
        ctx.rounding = ROUND_FLOOR
        ctx.traps[Inexact] = True
        assert run() == expected


@pytest.mark.parametrize('action', ['collect', 'remove'])
def test_explicit_actions_checkpoint_fee_dust_and_prevent_lifecycle_carry(action):
    book = ledger()
    observe(book)
    book.enter()
    half = Q128 // (2 * book.liquidity) + 1
    observe(book, block=2, growth=half)
    getattr(book, action)()
    if action == 'remove':
        book.enter()
    # The first action discarded sub-base-unit dust, matching its fee checkpoint.
    assert observe(book, block=3, growth=2 * half)['owed_base'] == ['0', '0']
    assert observe(book, block=4, growth=3 * half)['owed_base'] == ['1', '1']


def test_every_successful_action_charges_token1_cost_once():
    book = ledger(token0_units='1000', token1_units='2000', action_cost_token1='0.125')
    initial = observe(book)
    for index, action in enumerate((book.enter, book.collect, book.remove), start=1):
        row = action()
        with localcontext() as ctx:
            ctx.prec = 512
            assert Decimal(initial['modeled_value_token1']) - Decimal(row['modeled_value_token1']) == Decimal('0.125') * index
    assert list(map(Decimal, row['cash_units'])) == [Decimal(1000), Decimal('1999.625')]


@pytest.mark.parametrize('kwargs', [
    {'decimals': [True, 6]}, {'decimals': [6, 37]}, {'decimals': [6]},
    {'lower': -887273}, {'upper': 887273}, {'lower': 100}, {'upper': False},
])
def test_explicit_decimal_and_tick_bounds_are_not_coerced(kwargs):
    with pytest.raises(ValueError):
        ledger(**kwargs)

"""Small observation contracts are validated before executing browser actions."""
import pytest
from nerya.integrations.browser_observation import snapshot_options, bounded_int
from nerya.integrations.managed_browser import BrowserError

pytestmark = pytest.mark.smoke


def test_default_compact_and_explicit_full_budgets():
    compact, full = snapshot_options({}), snapshot_options({'observation':'full'})
    assert compact['limit'] == 60 and compact['maxChars'] == 1600
    assert full['limit'] == 150 and full['maxChars'] == 10000
    assert compact['compact'] and not full['compact']


@pytest.mark.parametrize('options', [
    {'observation':None}, {'observation':[]}, {'observation':'guess'},
    {'max_elements':True}, {'max_elements':0}, {'max_elements':151},
    {'max_chars':16001}, {'max_chars':float('nan')}, {'max_chars':'2000'},
    {'element_offset':-1}, {'text_offset':1000001}, {'scope':[]}, {'scope':'x'*1001},
])
def test_malformed_observation_budgets_are_rejected(options):
    with pytest.raises(BrowserError):
        snapshot_options(options)


def test_explicit_pagination_scope_and_receipt_mode():
    value = snapshot_options({'scope':'main','element_offset':60,'text_offset':1600,'observation':'none'})
    assert value['scope'] == 'main' and value['offset'] == 60 and value['textOffset'] == 1600
    assert bounded_int({},'offset',0,0,100) == 0

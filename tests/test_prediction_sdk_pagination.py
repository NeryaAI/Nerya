import pytest
from nerya.connectors.polymarket_client import wire_pages
from nerya.core.errors import TradingError

pytestmark=pytest.mark.smoke


def test_real_sdk_pages_are_flattened_without_losing_records():
    from polymarket.pagination import Page, Paginator
    calls=[]
    def fetch(cursor):
        calls.append(cursor)
        return Page(items=({'id':'first'},),has_more=True,next_cursor='second') if cursor is None else Page(items=({'id':'last'},),has_more=False)
    assert wire_pages(Paginator(fetch))==[{'id':'first'},{'id':'last'}]
    assert calls==[None,'second']


@pytest.mark.parametrize('problem',['cycle','depth','missing_cursor'])
def test_incomplete_sdk_pages_never_become_complete_balances(problem):
    from polymarket.pagination import Page, Paginator
    def fetch(cursor):
        return Page(items=({'id':'first'},),has_more=True,
                    next_cursor=None if problem=='missing_cursor' else 'repeat',limit_reached=problem=='depth')
    with pytest.raises(TradingError):wire_pages(Paginator(fetch))

"""
Unit tests for Entity Resolution's plain-code parts (design 14.1): parsing the two
reference lists, the matcher, the cross-listing question and the coverage rules.

A small hand-made list of listings stands in for the real SEC and NSE files, so these
tests need no network. Run with:
    uv run pytest tests/unit/test_entity_matcher.py -v
"""

import pytest

from invest_agent.mcp_servers.entity.matcher import check_coverage, normalise_name, resolve, resolve_with_guess
from invest_agent.mcp_servers.entity.reference_data import Listing, parse_nse_equity_csv, parse_sec_tickers


def us(ticker, name, cik, exchange="Nasdaq"):
    return Listing(ticker=ticker, name=name, exchange=exchange, market="US", currency="USD", cik=cik)


def nse(ticker, name, isin):
    return Listing(ticker=ticker, name=name, exchange="NSE", market="IN", currency="INR", isin=isin)


# A fixture shared by every test that names `listings` as an argument.
@pytest.fixture
def listings():
    return [
        us("AAPL", "Apple Inc.", 320193),
        us("APLE", "Apple Hospitality REIT, Inc.", 1418121, "NYSE"),
        us("GOOGL", "Alphabet Inc.", 1652044),
        us("GOOG", "Alphabet Inc.", 1652044),
        us("MSFT", "Microsoft Corp", 789019),
        us("F", "Ford Motor Co", 37996, "NYSE"),
        us("FORD", "Forward Industries Inc", 38264),
        us("INFY", "Infosys Ltd", 1067491, "NYSE"),
        us("XYZ", "Xyz Holdings Corp", 1),
        us("XYZ-PA", "Xyz Holdings Corp", 1, "NYSE"),
        nse("INFY", "Infosys Limited", "INE009A01021"),
        nse("RELIANCE", "Reliance Industries Limited", "INE002A01018"),
        nse("RPOWER", "Reliance Power Limited", "INE614G01033"),
        us("RS", "Reliance, Inc.", 861884, "NYSE"),
        nse("TATAMOTORS", "Tata Motors Limited", "INE155A01022"),
        nse("TATASTEEL", "Tata Steel Limited", "INE081A01020"),
        nse("TCS", "Tata Consultancy Services Limited", "INE467B01029"),
        nse("TATAPOWER", "Tata Power Company Limited", "INE245A01021"),
        nse("EMBASSY", "Embassy Office Parks REIT", "INE041025011"),
    ]


def test_normalise_name_drops_legal_words():
    assert normalise_name("Apple Inc.") == "apple"
    assert normalise_name("Tata Consultancy Services Limited") == "tata consultancy services"
    assert normalise_name("AT&T Inc") == "at and t"


def test_apple_resolves_to_aapl(listings):
    result = resolve("apple", listings)
    assert result.status == "resolved" and result.listing.ticker == "AAPL"


def test_apple_reit_asks(listings):
    result = resolve("apple reit", listings)
    assert result.status == "ask_user"
    assert {c.ticker for c in result.candidates} >= {"APLE"}


def test_tata_asks(listings):
    result = resolve("tata", listings)
    assert result.status == "ask_user" and len(result.candidates) >= 2
    assert result.question.startswith("Did you mean")


def test_nse_suffix_ticker_resolves(listings):
    result = resolve("RELIANCE.NS", listings)
    assert result.status == "resolved" and result.listing.market == "IN"
    assert result.listing.yahoo_symbol == "RELIANCE.NS"


def test_lowercase_word_prefers_the_company_name(listings):
    # "FORD" is Forward Industries' ticker; someone typing "ford" means Ford Motor.
    assert resolve("ford", listings).listing.ticker == "F"
    # In capitals it is read as a ticker.
    assert resolve("FORD", listings).listing.ticker == "FORD"


def test_cross_listed_company_always_asks(listings):
    result = resolve("infosys", listings)
    assert result.status == "ask_user"
    assert {c.market for c in result.candidates} == {"US", "IN"}
    assert "Which one do you want analysed?" in result.question


def test_confirmed_choice_skips_the_cross_listing_question(listings):
    result = resolve("NSE:INFY", listings, confirmed=True)
    assert result.status == "resolved" and result.listing.market == "IN"


def test_share_classes_ask(listings):
    result = resolve("alphabet", listings)
    assert result.status == "ask_user" and "share class" in result.question


def test_preferred_shares_are_ignored_for_name_search(listings):
    assert resolve("xyz holdings", listings).listing.ticker == "XYZ"


def test_nonsense_is_not_found(listings):
    result = resolve("qwerty zzzz", listings)
    assert result.status == "not_found" and result.question


def test_coverage_rules(listings):
    infy_us = next(l for l in listings if l.ticker == "INFY" and l.market == "US")
    assert "20-F" in check_coverage(infy_us, {"annual_forms": ["20-F"], "sic": 7371})
    aple = next(l for l in listings if l.ticker == "APLE")
    assert "REIT" in check_coverage(aple, {"annual_forms": ["10-K"], "sic": 6798})
    assert check_coverage(listings[0], {"annual_forms": ["10-K"], "sic": 3571}) is None
    embassy = next(l for l in listings if l.ticker == "EMBASSY")
    assert "REIT" in check_coverage(embassy, None)


def test_parse_sec_and_nse_files():
    sec = parse_sec_tickers({
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"], [1, "Gone Corp", "GONE", None]],
    })
    assert [l.ticker for l in sec] == ["AAPL"]          # no exchange means not trading
    csv_text = ("SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
                "INFY,Infosys Limited,EQ,08-FEB-1995,5,1,INE009A01021,5\n")
    india = parse_nse_equity_csv(csv_text)
    assert india[0].ticker == "INFY" and india[0].isin == "INE009A01021" and india[0].currency == "INR"


# --- The model's full-name guess (added 5 October after the "reliance" live run) --------

def test_reliance_asks_with_the_model_guess_first(listings):
    # The bug found live: "reliance" alone exactly matches the US company Reliance, Inc.
    result = resolve_with_guess("reliance", "Reliance Industries Limited", listings)
    assert result.status == "ask_user"
    assert [c.ticker for c in result.candidates[:2]] == ["RELIANCE", "RS"]


def test_guess_agreeing_with_the_words_resolves(listings):
    result = resolve_with_guess("apple", "Apple Inc.", listings)
    assert result.status == "resolved" and result.listing.ticker == "AAPL"


def test_guess_resolves_a_description_with_no_name(listings):
    result = resolve_with_guess("the indian oil to telecom giant", "Reliance Industries Limited", listings)
    assert result.status == "resolved" and result.listing.ticker == "RELIANCE"


def test_ambiguous_words_ask_with_the_guess_first(listings):
    result = resolve_with_guess("tata", "Tata Motors Limited", listings)
    assert result.status == "ask_user" and result.candidates[0].ticker == "TATAMOTORS"
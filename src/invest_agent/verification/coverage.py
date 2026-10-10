"""
verification/coverage.py: the version 1 small-company rule (design 14.9).

What this file does:
    Once the price and the share count have passed the sanity checks, the company's
    market value is worked out (price x shares, in the listing currency). Below ₹500
    crore for Indian listings or $300 million for US listings, the request ends with a
    "not covered in version 1" reply: coverage of very small companies is too thin for a
    sound verdict. The limits are in coverage_rules.yaml.

    If the price or the share count is missing, the rule cannot be applied; the request
    carries on (completeness will already have withheld the amount) and this is flagged.
"""

from __future__ import annotations

from invest_agent.common.yaml_loader import load_yaml
from invest_agent.fx.convert import format_money
from invest_agent.verification.record_index import RecordIndex


def check_market_value(records: list[dict], listing_currency: str | None) -> dict:
    index = RecordIndex(records)
    price, shares = index.value("current_price"), index.value("shares_outstanding")
    if not price or not shares or not listing_currency:
        return {"covered": True, "market_value": None, "flag": "market_value_unknown"}

    market_value = price * shares
    limit = load_yaml("verification/coverage_rules.yaml")["min_market_value"].get(listing_currency)
    if limit is not None and market_value < limit:
        return {"covered": False, "market_value": market_value, "limit": limit,
                "reason": (f"its market value of about {format_money(market_value, listing_currency)} is below "
                           f"the {format_money(limit, listing_currency)} minimum for version 1, and coverage of "
                           "companies this small is too thin for a sound verdict")}
    return {"covered": True, "market_value": market_value, "limit": limit}

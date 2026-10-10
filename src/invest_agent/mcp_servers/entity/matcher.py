"""
mcp_servers/entity/matcher.py: decides which listed company the user means.

What this file does:
    Given the company reference pulled out of the user's message ("apple", "Infosys",
    "RELIANCE.NS", "tata") and the reference lists, it returns one of four outcomes:

      resolved     one clear match, with its ticker, exchange and currency
      ask_user     more than one plausible match, or a company listed on more than
                   one exchange; carries the question to ask and the candidates
      not_found    nothing close enough; carries a question asking for the name
      not_covered  set later by check_coverage(): 20-F and 40-F filers, REITs, InvITs

    The steps, in order (design 14.1):
      1. Exact ticker, including "RELIANCE.NS", "NSE:INFY" and "US:AAPL" forms.
      2. Exact company name after tidying ("Apple Inc." and "apple" both become "apple").
      3. Fuzzy match with the rapidfuzz library. Confident only when the best score is
         at least 90 out of 100 and at least 10 points ahead of the next company;
         otherwise the close candidates are offered as a question.
      4. Cross-listing check: if the same company is also listed in the other market
         (Infosys on NSE and as an ADR on NYSE), always ask which one.

    Everything here is plain code with no network calls, so it is fully unit-tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# rapidfuzz scores how similar two strings are, from 0 to 100.
#   fuzz.WRatio   a weighted score that copes with extra words and word order
#                 ("apple reit" against "apple hospitality reit")
#   fuzz.ratio    a strict character-by-character score, used for cross-listings
#   process.extract  scores one query against many choices and returns the best
from rapidfuzz import fuzz, process

from invest_agent.common.yaml_loader import load_yaml
from invest_agent.mcp_servers.entity.reference_data import Listing

# Words that describe the legal form of a company, not which company it is.
LEGAL_WORDS = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "plc", "llc", "lp", "sa", "ag", "nv", "the", "pvt", "private",
}

# Below this fuzzy score a candidate is too weak even to offer as an option.
ASK_FLOOR = 60
MAX_OPTIONS = 3

# "NSE:INFY", "BSE INFY", "US:AAPL"   and   "RELIANCE.NS", "INFY.BO"
_PREFIX_FORM = re.compile(r"^(NSE|BSE|US)\s*[:\s]\s*([A-Z0-9&\-.]+)$", re.IGNORECASE)
_SUFFIX_FORM = re.compile(r"^([A-Z0-9&\-]+)\.(NS|BO)$", re.IGNORECASE)
_TICKER_LIKE = re.compile(r"^[A-Z0-9&\-.]{1,12}$", re.IGNORECASE)


@dataclass
class MatchResult:
    status: str                                   # resolved, ask_user, not_found, not_covered
    listing: Listing | None = None
    candidates: list[Listing] = field(default_factory=list)
    question: str | None = None
    reason: str | None = None                     # why not covered

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "listing": self.listing.to_dict() if self.listing else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "question": self.question,
            "reason": self.reason,
        }


# --- Small helpers ---------------------------------------------------------------------

def normalise_name(name: str) -> str:
    """Lower-case, "&" to "and", punctuation removed, legal words dropped.
    "Apple Inc." -> "apple";  "Tata Consultancy Services Limited" -> "tata consultancy services"."""
    text = name.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)          # anything not a letter, digit or space
    words = [w for w in text.split() if w not in LEGAL_WORDS]
    return " ".join(words)


def parse_ticker_hint(query: str) -> tuple[str | None, str | None]:
    """Return (symbol, market) if the query is written like a ticker, else (None, None).
    market is "IN", "US", or None when the user did not say."""
    q = query.strip()
    if m := _SUFFIX_FORM.match(q):          # ":=" assigns and tests in one step
        return m.group(1).upper(), "IN"
    if m := _PREFIX_FORM.match(q):
        prefix = m.group(1).upper()
        return m.group(2).upper(), ("US" if prefix == "US" else "IN")
    if _TICKER_LIKE.match(q) and " " not in q:
        return q.upper(), None
    return None, None


def _describe(listing: Listing) -> str:
    return f"{listing.name} ({listing.ticker}, {listing.exchange})"


def _currency_word(listing: Listing) -> str:
    return "rupees" if listing.currency == "INR" else "dollars"


def _primary_listings(listings: list[Listing]) -> list[Listing]:
    """Drop preferred shares, warrants and units ("XYZ-PA") when the same company has a
    plain ticker, so a normal name search does not ask about them."""
    plain_keys = {l.company_key for l in listings if "-" not in l.ticker}
    return [l for l in listings if "-" not in l.ticker or l.company_key not in plain_keys]


def _ask(candidates: list[Listing], intro: str = "Did you mean") -> MatchResult:
    options = candidates[:MAX_OPTIONS]
    names = [_describe(c) for c in options]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f", or {names[-1]}"
    return MatchResult(status="ask_user", candidates=options, question=f"{intro} {joined}?")


# --- The main entry point -------------------------------------------------------------------

def resolve(query: str, listings: list[Listing], *, confirmed: bool = False) -> MatchResult:
    """Match a company reference against the reference lists.

    confirmed=True means the user has already chosen a specific listing (after a
    question), so the cross-listing question is not asked again.
    """
    cfg = load_yaml("mcp_servers/entity/entity_config.yaml")
    min_score = cfg["match"]["min_score"]
    min_lead = cfg["match"]["min_lead"]

    q = (query or "").strip()
    if not q:
        return MatchResult(status="not_found", question="Which company would you like me to look at?")

    pool = _primary_listings(listings)

    # Step 1: exact ticker. A query written in capitals ("INFY") or with an exchange
    # ("RELIANCE.NS", "NSE:INFY") is treated as a ticker straight away. A lower-case
    # word ("ford") is tried as a name first, because many tickers are also ordinary
    # words ("FORD" is Forward Industries, not Ford Motor).
    symbol, market = parse_ticker_hint(q)
    ticker_hits = [l for l in listings if symbol and l.ticker == symbol and (market is None or l.market == market)]
    ticker_first = bool(symbol) and (market is not None or q.isupper())
    if ticker_first and ticker_hits:
        return _from_ticker_hits(ticker_hits, listings, confirmed, cfg)

    # Step 2: exact name after tidying.
    nq = normalise_name(q)
    if nq:
        same_name = [l for l in pool if normalise_name(l.name) == nq]
        if len(same_name) == 1:
            return _finish(same_name[0], listings, confirmed, cfg)
        if len(same_name) > 1:
            if len({l.company_key for l in same_name}) == 1:
                return _ask(same_name, "This company has more than one share class. Did you mean")
            return _finish_many_markets(same_name, listings, confirmed, cfg)

    # Step 3: fuzzy match. process.extract returns (choice, score, index) tuples,
    # best first; we score each listing's tidied name against the tidied query.
    names = [normalise_name(l.name) for l in pool]
    scored = process.extract(nq or q.lower(), names, scorer=fuzz.WRatio, limit=25)

    # Keep the best score per company, so two share classes of one company do not
    # count as "two close candidates".
    best_per_company: dict[str, tuple[Listing, float]] = {}
    for _, score, i in scored:
        listing = pool[i]
        key = listing.company_key
        if key not in best_per_company or score > best_per_company[key][1]:
            best_per_company[key] = (listing, score)
    ranked = sorted(best_per_company.values(), key=lambda pair: pair[1], reverse=True)

    if ranked:
        top_listing, top_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        if top_score >= min_score and (top_score - runner_up) >= min_lead:
            return _finish(top_listing, listings, confirmed, cfg)

    # A lower-case word that is not a confident name match but is an exact ticker
    # ("infy") is taken as the ticker.
    if ticker_hits:
        return _from_ticker_hits(ticker_hits, listings, confirmed, cfg)

    if not ranked or ranked[0][1] < ASK_FLOOR:
        return MatchResult(
            status="not_found",
            question=f'I could not find a listed company matching "{q}". Could you give the full company name or its ticker?',
        )

    close = [l for l, s in ranked if s >= max(ASK_FLOOR, ranked[0][1] - 15)]
    return _ask(close)


def _from_ticker_hits(hits: list[Listing], listings: list[Listing], confirmed: bool, cfg: dict) -> MatchResult:
    if len(hits) == 1:
        return _finish(hits[0], listings, confirmed, cfg)
    return _ask(hits, "That ticker exists on more than one exchange. Did you mean")


# --- Using the model's full-name suggestion (added 5 October) ------------------------------------

def _same(a: Listing | None, b: Listing | None) -> bool:
    return a is not None and b is not None and (a.ticker, a.market) == (b.ticker, b.market)


def resolve_with_guess(query: str, guess: str | None, listings: list[Listing], *,
                       confirmed: bool = False) -> MatchResult:
    """Resolve the user's own words, and the model's guess at the full company name,
    and compare them. The model never decides on its own:

      both point to the same company        resolved ("apple" and "Apple Inc.")
      they point to different companies     ask, with the model's suggestion first
                                            ("reliance": Reliance Industries on NSE, or
                                            Reliance, Inc. on NYSE?)
      the words are ambiguous ("tata")      ask, with the model's suggestion first
      the words match nothing, the guess    resolved: this is how a description such as
      matches one company                   "the company Elon Musk runs" is handled
    """
    raw = resolve(query, listings, confirmed=confirmed)
    if not guess or normalise_name(guess) == normalise_name(query):
        return raw
    suggested = resolve(guess, listings, confirmed=confirmed)

    if suggested.status != "resolved":
        # The guess did not settle anything; it only helps when the words found nothing.
        return suggested if raw.status == "not_found" and suggested.status == "ask_user" else raw

    if raw.status == "resolved":
        if _same(raw.listing, suggested.listing):
            return raw
        return _ask([suggested.listing, raw.listing])
    if raw.status == "ask_user":
        others = [c for c in raw.candidates if not _same(c, suggested.listing)]
        return _ask([suggested.listing] + others)
    return suggested          # the words matched nothing; the guess did


# --- Cross-listing ---------------------------------------------------------------------------

def _other_market_listing(listing: Listing, listings: list[Listing], threshold: int) -> Listing | None:
    """The same company's listing in the other market, if there is one."""
    target = normalise_name(listing.name)
    best, best_score = None, 0.0
    for other in listings:
        if other.market == listing.market or "-" in other.ticker:
            continue
        score = fuzz.ratio(target, normalise_name(other.name))
        if score > best_score:
            best, best_score = other, score
    return best if best_score >= threshold else None


def _cross_listing_question(options: list[Listing]) -> str:
    name = options[0].name
    parts = [f"on {o.exchange} in {_currency_word(o)} ({o.ticker})" for o in options]
    return f"{name} is listed {' and '.join(parts)}. Which one do you want analysed?"


def _finish(listing: Listing, listings: list[Listing], confirmed: bool, cfg: dict) -> MatchResult:
    """A single match: resolved, unless the company is cross-listed and not yet confirmed."""
    if not confirmed and cfg["cross_listing"]["behaviour"] == "always_ask":
        other = _other_market_listing(listing, listings, cfg["cross_listing"]["name_match_score"])
        if other:
            options = [listing, other]
            return MatchResult(status="ask_user", candidates=options, question=_cross_listing_question(options))
    return MatchResult(status="resolved", listing=listing)


def _finish_many_markets(same_name: list[Listing], listings: list[Listing], confirmed: bool, cfg: dict) -> MatchResult:
    """Same tidied name in more than one market: that is a cross-listing question."""
    markets = {l.market for l in same_name}
    if len(markets) > 1 and not confirmed:
        options = sorted(same_name, key=lambda l: l.market != "IN")[:2]   # Indian listing first
        return MatchResult(status="ask_user", candidates=options, question=_cross_listing_question(options))
    return _ask(same_name)


# --- Version 1 coverage rules ------------------------------------------------------------------

INDIA_NOT_COVERED_WORDS = ("reit", "invit", "investment trust", "infrastructure trust")


def check_coverage(listing: Listing, sec_profile: dict | None) -> str | None:
    """Return the reason a resolved company is not covered in version 1, or None.

    US companies use the SEC profile from reference_data.fetch_sec_profile().
    Indian REITs and InvITs are caught by name for now; the design's Trendlyne
    industry check replaces this once Trendlyne is connected (step 5).
    """
    rules = load_yaml("verification/coverage_rules.yaml")["not_covered"]
    entity = load_yaml("mcp_servers/entity/entity_config.yaml")["coverage"]

    if listing.market == "US" and sec_profile:
        forms = set(sec_profile.get("annual_forms") or [])
        foreign = set(rules["sec_annual_forms"])
        if forms & foreign and "10-K" not in forms:
            return ("it files its annual report with SEC on Form "
                    f"{'/'.join(sorted(forms & foreign))} as a foreign company, which version 1 does not read")
        if sec_profile.get("sic") in entity["reit_sic_codes"]:
            return "it is a REIT, which is valued on funds from operations and is not covered in version 1"

    if listing.market == "IN" and rules.get("reits_and_invits"):
        lowered = listing.name.lower()
        if any(word in lowered for word in INDIA_NOT_COVERED_WORDS):
            return "it is a REIT or InvIT, which is not covered in version 1"
    return None
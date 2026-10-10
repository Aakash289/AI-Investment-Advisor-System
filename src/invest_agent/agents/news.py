"""
agents/news.py: the News and Sentiment Agent node (design 14.6).

What this file does:
    1. One MCP call to news_server's get_news: articles (Tavily), Indian disclosures and
       insider trades (Trendlyne), US 8-K filings (SEC), coverage (GDELT, when reachable).
    2. One Luna call labels every article and disclosure at once (the prompt in
       prompts/news_classification.txt): relevant or not, fact, opinion, speculation or
       company statement, event type, sentiment and materiality, whether it threatens
       the business itself (a strict test; only these can cap the verdict), and whether
       it repeats an earlier item's event, with a one-line reason in the item's own words. The answer is checked: ids the model invents are dropped,
       and items it skipped stay unlabelled and are not counted.
    3. Plain code (mcp_servers/news/aggregate.py) works out the net sentiment, the top
       three items and any serious negative fact.

    Every relevant item becomes a SourceRecord with its link (or SEC accession number),
    so a claim in the report can be traced to the article it came from. Only headlines
    and short snippets are kept, never article text.

    If the labelling call fails, the items are kept unlabelled and the news score is
    left missing rather than guessed.

    Label memory (A12, 6 October): the model can label the same article differently on
    two runs (one Reliance story was negative, then neutral, three minutes apart), which
    moved the news score with nothing new. So each item's label is saved for 7 days,
    keyed on the article itself (its link, or outlet and headline), and reused:
      every item already labelled   no model call at all, the saved labels are used
      some new items                one call labels them all (the model needs the whole
                                    list to spot repeats), then the saved labels win for
                                    the items it has seen before
    The same article therefore always counts the same way, and a repeat request often
    saves the model call. The same story can also come back from another outlet (Reuters
    one run, Business Line the next), so the labelling call is shown this company's
    stories from the last 7 days with their labels; a new article about one of them
    takes that story's label. "duplicate_of" is saved as the other article's key, since the
    ids (n1, n2...) depend on the order of each fetch.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from importlib import resources
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from invest_agent.cache.store import CacheStore
from invest_agent.common.models import structured_call
from invest_agent.common.records import make_record
from invest_agent.config import get_settings
from invest_agent.graph.state import GraphState
from invest_agent.mcp_servers.clients import call_tool
from invest_agent.mcp_servers.news.aggregate import summarise

logger = logging.getLogger(__name__)


class ItemLabel(BaseModel):
    id: str
    relevant: bool
    kind: Literal["fact_report", "opinion", "speculation", "company_statement"]
    event_type: Literal["earnings", "guidance", "management_change", "merger_acquisition", "litigation",
                        "regulatory", "product", "rating_change", "macro_or_sector", "other"]
    sentiment: Literal["positive", "neutral", "negative"]
    materiality: Literal["high", "medium", "low"]
    threatens_business: bool = False        # strict test in the prompt; only these can cap the verdict
    duplicate_of: str | None = None         # id of an earlier item about the same event
    reason: str


class Labels(BaseModel):
    items: list[ItemLabel]


NewsFn = Callable[[str, str, str, str | None], Awaitable[dict]]
LabelFn = Callable[[str, list[dict], dict], Awaitable[Labels]]


async def news_from_server(market: str, ticker: str, company_name: str, cik: str | None) -> dict:
    return await call_tool("news", "get_news", {"market": market, "ticker": ticker, "company_name": company_name,
                                                "cik": cik}, timeout=120)


def _prompt(state: dict) -> str:
    text = resources.files("invest_agent.agents").joinpath("prompts/news_classification.txt").read_text(encoding="utf-8")
    return text.format(company_name=state.get("company_name"), ticker=state.get("ticker"),
                       exchange=state.get("exchange"), sector=state.get("sector") or state.get("industry") or "unknown")


def format_items(items: list[dict]) -> str:
    return "\n\n".join(f"id: {i['id']}\noutlet: {i.get('outlet')}\npublished: {i.get('date')}\n"
                       f"headline: {i.get('title')}\nsnippet: {i.get('snippet', '')[:400]}" for i in items)


async def label_with_model(system: str, items: list[dict], state: dict) -> Labels:
    """The one Luna call for this node."""
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env; the News agent needs it.")
    # Low reasoning effort (speed, 7 October): labelling is sorting, not reasoning.
    return await structured_call(settings.model_retrieval, Labels,
                                 [SystemMessage(content=system), HumanMessage(content=format_items(items))])


def apply_labels(items: list[dict], labels: Labels) -> list[dict]:
    """Join the model's labels onto the items by id; invented ids are ignored."""
    by_id = {lab.id: lab.model_dump() for lab in labels.items}
    out = []
    for item in items:
        lab = by_id.get(item["id"])
        if lab and item.get("internal_transfer"):
            # Shares moved inside the promoter group: neither good nor bad news, whatever
            # the model read into it (Star Health, 6 October).
            lab = {**lab, "sentiment": "neutral", "materiality": "low", "threatens_business": False}
        out.append({**item, **{k: v for k, v in lab.items() if k != "id"}, "labelled": True} if lab
                   else {**item, "labelled": False})
    return out


# --- Label memory (A12) ------------------------------------------------------------------------

LABEL_NAMESPACE = "news_labels"
LABEL_TTL = timedelta(days=7)


def item_key(ticker: str, item: dict) -> str:
    """A stable key for one article: its link, or its outlet and headline."""
    basis = item.get("url") or f"{item.get('outlet')}|{item.get('title')}"
    return f"{ticker}:{hashlib.sha1(basis.encode('utf-8')).hexdigest()[:16]}"


STORY_FIELDS = ("relevant", "kind", "event_type", "sentiment", "materiality", "threatens_business")


def earlier_stories(ticker: str, cache: CacheStore, skip: set[str], limit: int = 20) -> dict[str, dict]:
    """Labels saved in the last 7 days for this company's other articles: {"p1": label}."""
    rows = cache.conn.execute(
        "SELECT key, value_json FROM cache WHERE namespace = ? AND key LIKE ? AND expires_at > ? "
        "ORDER BY created_at DESC LIMIT ?",
        (LABEL_NAMESPACE, f"{ticker}:%", datetime.now(timezone.utc).isoformat(timespec="seconds"), limit * 3),
    ).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        if row["key"] in skip:
            continue
        lab = json.loads(row["value_json"])
        if lab.get("title") and lab.get("relevant") and not lab.get("duplicate_of"):
            out[f"p{len(out) + 1}"] = lab
        if len(out) >= limit:
            break
    return out


def earlier_text(stories: dict[str, dict]) -> str:
    """The note added to the labelling prompt so a story keeps its label across outlets."""
    if not stories:
        return ""
    lines = [f"{pid}: {lab['title']} (labelled {lab['sentiment']}, {lab['materiality']})"
             for pid, lab in stories.items()]
    return ("\n\nStories about this company labelled in the last 7 days. If an item reports the same event as "
            "one of these (the same deal, lawsuit or announcement, even from another outlet or in other words), "
            "set duplicate_of to that story's id (p1, p2...) and give the item the same sentiment and "
            "materiality:\n" + "\n".join(lines))


async def labels_with_memory(ticker: str, items: list[dict], call: Callable[[str], Awaitable[Labels]],
                             cache: CacheStore) -> tuple[Labels, bool]:
    """Saved labels where they exist, one model call only when an item is new.
    call(extra_prompt) runs the model. Returns the labels and whether the model was called."""
    keys = {i["id"]: item_key(ticker, i) for i in items}
    titles = {i["id"]: i.get("title") for i in items}
    id_by_key = {k: i for i, k in keys.items()}
    saved = {i: cache.get(LABEL_NAMESPACE, k) for i, k in keys.items()}

    def from_saved(item_id: str) -> ItemLabel:
        lab = dict(saved[item_id])
        lab["duplicate_of"] = id_by_key.get(lab.get("duplicate_of"))      # key back to this fetch's id
        return ItemLabel(id=item_id, **{k: v for k, v in lab.items() if k in ItemLabel.model_fields and k != "id"})

    if all(saved.values()):
        return Labels(items=[from_saved(i) for i in keys]), False

    stories = earlier_stories(ticker, cache, skip=set(keys.values()))
    fresh = await call(earlier_text(stories))
    out = []
    for lab in fresh.items:
        if lab.id not in keys:
            continue                                             # an id the model invented
        if saved.get(lab.id):
            out.append(from_saved(lab.id))
            continue
        if lab.duplicate_of in stories:
            # The same story seen earlier from another outlet: it keeps that story's label,
            # and counts once here, since the earlier article is not in this list.
            lab = lab.model_copy(update={**{f: stories[lab.duplicate_of][f] for f in STORY_FIELDS},
                                         "duplicate_of": None})
        elif lab.duplicate_of and lab.duplicate_of not in keys:
            lab = lab.model_copy(update={"duplicate_of": None})
        to_save = {**lab.model_dump(), "title": titles[lab.id]}
        to_save["duplicate_of"] = keys.get(lab.duplicate_of) if lab.duplicate_of else None
        cache.set(LABEL_NAMESPACE, keys[lab.id], to_save, ttl=LABEL_TTL)
        out.append(lab)
    # Items the model skipped but that were labelled before keep their saved label.
    done = {lab.id for lab in out}
    out += [from_saved(i) for i in keys if i not in done and saved.get(i)]
    return Labels(items=out), True


# Speed (7 October): the labelling call's time grows with the number of items, and a busy
# company brings 25 to 35 (two Tavily searches, Trendlyne news and insider trades). The
# 15 kept are the official disclosures and insider trades first, then trusted outlets,
# newest first; the rest are left out of the news score.
MAX_LABELLED = 15


def for_labelling(items: list[dict]) -> list[dict]:
    """The MAX_LABELLED items most worth labelling, in their original order."""
    def rank(item: dict) -> tuple:
        official = str(item.get("origin", "")).startswith("trendlyne")
        return (not official, not item.get("trusted", False), _negated_date(item.get("date")))
    keep = {id(i) for i in sorted(items, key=rank)[:MAX_LABELLED]}
    return [i for i in items if id(i) in keep]


def _negated_date(value) -> str:
    """Sorts newest first: each digit of an ISO date flipped (2026-10-07 -> 7973-89-92)."""
    return "".join(str(9 - int(c)) if c.isdigit() else c for c in str(value or "0000-00-00")[:10])


def build_records(news: dict, ticker: str) -> list[dict]:
    records = []
    for item in news.get("items", []):
        if not item.get("relevant"):
            continue
        tier = 1 if item.get("origin") in ("trendlyne_insider",) else 3
        records.append(make_record(
            source=item.get("origin", "news"), tier=tier, entity=ticker, field="news_item", value=item["title"],
            as_of=item.get("date"), period=item["id"], source_ref=item.get("url") or item.get("outlet"),
            flags=[item.get("sentiment"), item.get("materiality"), item.get("kind")]))
    for f in news.get("filings", []):
        records.append(make_record(source="sec_edgar", tier=1, entity=ticker, field="8k_filing", value=f["title"],
                                   as_of=f["date"], period=f["accession"], accession_number=f["accession"],
                                   flags=["serious"] if f.get("serious") else None))
    if news.get("summary", {}).get("net_sentiment") is not None:
        records.append(make_record(source="computed", tier=3, entity=ticker, field="news_net_sentiment",
                                   value=news["summary"]["net_sentiment"], as_of=news.get("as_of"),
                                   source_ref=",".join(r["record_id"] for r in records if r["field"] == "news_item")))
    return records


def make_news_node(fetch: NewsFn = news_from_server, label: LabelFn = label_with_model, today: date | None = None,
                   cache_factory: Callable[[], CacheStore] | None = None):
    """cache_factory gives the label memory; None (tests) labels every time."""
    async def news(state: GraphState) -> dict:
        ticker, market = state.get("ticker"), state.get("market")
        if not ticker or not market:
            return {"news_records": []}
        try:
            raw = await fetch(market, ticker, state.get("company_name") or ticker, state.get("cik"))
        except Exception as err:
            logger.warning("News server failed: %s", err)
            return {"news_records": [], "news": {"items": [], "filings": [], "flags": ["news_failed"]}}

        items, flags = raw.get("items", []), list(raw.get("flags", []))
        if len(items) > MAX_LABELLED:
            items = for_labelling(items)
            flags.append(f"labelled_top_{MAX_LABELLED}")
        if items:
            try:
                system = _prompt(state)
                if cache_factory is None:
                    labels = await label(system, items, state)
                else:
                    labels, called = await labels_with_memory(ticker, items, lambda extra: label(system + extra, items, state),
                                                              cache_factory())
                    if not called:
                        flags.append("labels_from_memory")
                items = apply_labels(items, labels)
            except Exception as err:
                logger.warning("News labelling failed: %s", err)
                flags.append("labelling_failed")
        result = {**raw, "items": items, "flags": flags, "summary": summarise(items, today)}
        return {"news_records": build_records(result, ticker), "news": result}

    return news


news_node = make_news_node(cache_factory=CacheStore)

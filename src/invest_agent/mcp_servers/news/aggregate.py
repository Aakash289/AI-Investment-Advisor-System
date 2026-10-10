"""
mcp_servers/news/aggregate.py: deduplication before labelling, and the net sentiment after (design 14.6).

What this file does:
    Before the labelling call
      dedupe()      the same story is often carried by several outlets, and Tavily and
                    Trendlyne can both return it. Headlines that are near copies of each
                    other (rapidfuzz similarity 85 or more) are kept once, preferring the
                    trusted outlet and the earlier copy. Items get short ids (n1, n2...)
                    that the model must use in its answer.

    After the labelling call (agents/news.py)
      summarise()   drops items the model marked not relevant, then works out:
                    net sentiment from -1 to +1, each item weighted by materiality (high 3,
                    medium 2, low 1), kind (facts and disclosures 1, opinion, speculation
                    and company statements 0.5) and recency (last 7 days counts double);
                    the top three items, high materiality first; and whether any item is a
                    serious negative fact, which caps the verdict later (design 14.11).
                    Since 6 October "serious" needs the model's threatens_business mark,
                    not just high materiality, and items the model marks as repeating an
                    earlier item's event (duplicate_of) are counted once (A8, A11)

    The score itself (F3) is worked out in step 9 from these.
"""

from __future__ import annotations

from datetime import date, timedelta

from rapidfuzz import fuzz

MATERIALITY = {"high": 3, "medium": 2, "low": 1}
KIND = {"fact_report": 1.0, "opinion": 0.5, "speculation": 0.5, "company_statement": 0.5}
SENTIMENT = {"positive": 1, "neutral": 0, "negative": -1}
WIRES = ("prnewswire.com", "businesswire.com", "globenewswire.com")


def dedupe(items: list[dict], threshold: int = 85, limit: int = 25) -> list[dict]:
    kept: list[dict] = []
    ordered = sorted(items, key=lambda i: (not i.get("trusted"), i.get("date") or ""))
    for item in ordered:
        if not item.get("title"):
            continue
        if any(fuzz.token_set_ratio(item["title"].lower(), k["title"].lower()) >= threshold for k in kept):
            continue
        kept.append(item)
    kept.sort(key=lambda i: i.get("date") or "", reverse=True)     # newest first for the model
    kept = kept[:limit]
    for n, item in enumerate(kept, 1):
        item["id"] = f"n{n}"
        if any(w in (item.get("url") or "") for w in WIRES):
            item["press_release"] = True
    return kept


def weight(item: dict, today: date) -> float:
    w = MATERIALITY.get(item.get("materiality"), 1) * KIND.get(item.get("kind"), 0.5)
    try:
        recent = item.get("date") and date.fromisoformat(item["date"][:10]) >= today - timedelta(days=7)
    except ValueError:
        recent = False
    return w * (2 if recent else 1)


def summarise(labelled: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    # Not relevant, or the same event as an earlier item (the model says which): left out.
    ids = {i.get("id") for i in labelled}
    relevant = [i for i in labelled if i.get("relevant") and i.get("duplicate_of") not in ids - {i.get("id")}]
    total = sum(weight(i, today) for i in relevant)
    net = (sum(weight(i, today) * SENTIMENT.get(i.get("sentiment"), 0) for i in relevant) / total) if total else None
    top = sorted(relevant, key=lambda i: (MATERIALITY.get(i.get("materiality"), 1), weight(i, today)), reverse=True)[:3]
    # Serious: only a negative fact the model judged a threat to the business itself
    # (decided 6 October after routine lawsuits were marked serious, A8).
    serious = [i for i in relevant if i.get("threatens_business") and i.get("sentiment") == "negative"
               and i.get("kind") == "fact_report"]
    counts = {s: sum(1 for i in relevant if i.get("sentiment") == s) for s in SENTIMENT}
    return {"net_sentiment": round(net, 4) if net is not None else None, "relevant_count": len(relevant),
            "labelled_count": len(labelled), "counts": counts, "top_ids": [i["id"] for i in top],
            "serious_negative_ids": [i["id"] for i in serious]}

"""
analysis/news_score.py: F3, the news sub-score (design 14.18).

What this file does:
    The News agent has already labelled each relevant item and worked out the net
    sentiment, from -1 to +1, weighting each item by materiality (high 3, medium 2,
    low 1), kind (facts and disclosures 1, opinion, speculation and company statements
    0.5) and recency (last 7 days counts double). This turns it into a sub-score:

      sub-score       net sentiment x 2, so it runs from -2 to +2 like the others
      thin coverage   fewer than 3 relevant items halves it: two articles are not a trend
      no news         no relevant items means the sub-score is missing, not zero, and its
                      weight is shared out among the others

    GDELT's tone stays as context and is not added: it measures the same coverage.
"""

from __future__ import annotations

from invest_agent.analysis.scoring_common import finding
from invest_agent.common.yaml_loader import load_yaml


def news(news_state: dict | None) -> dict:
    cfg = load_yaml("analysis/scoring_bands.yaml")["news"]
    summary = (news_state or {}).get("summary") or {}
    net, count = summary.get("net_sentiment"), summary.get("relevant_count", 0)
    if net is None or not count:
        return {"score": None, "findings": [], "missing": ["news"]}
    score = net * 2
    thin = count < cfg["thin_coverage_items"]
    if thin:
        score /= 2
    counts = summary.get("counts") or {}
    text = (f"News over the last month: net sentiment {net:+.2f} from {count} relevant items "
            f"({counts.get('positive', 0)} positive, {counts.get('neutral', 0)} neutral, {counts.get('negative', 0)} negative)"
            + (", halved for thin coverage" if thin else ""))
    return {"score": round(score, 4), "missing": [],
            "findings": [finding("news", round(score, 2), text, {"net_sentiment": f"{net:+.2f}", "items": str(count)},
                                 ["computed:news_net_sentiment"])]}

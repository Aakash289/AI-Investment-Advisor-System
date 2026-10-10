"""
eval/spread_check.py: does the agent say Neutral for every company? (7 October 2026)

What this file does:
    Every live run so far (Reliance, Apple, Eternal, Star Health) came out Neutral. This
    check runs the full live agent on a spread of very different companies with one fixed
    profile, then reads the results from the decision log and shows where the verdicts land
    and why.

        uv run python -m invest_agent.eval.spread_check --limit 2     a trial with 2 companies
        uv run python -m invest_agent.eval.spread_check               all of them (about 35 minutes)
        uv run python -m invest_agent.eval.spread_check --report      reprint the results only
        uv run python -m invest_agent.eval.spread_check --report --all   every analysis ever logged
        uv run python -m invest_agent.eval.spread_check --redo Pfizer Alphabet   run those again

    Run: each company in eval/spread_companies.yaml is asked about with the same profile
    text, through main.run_turn exactly as the app does. If the agent asks which listing
    is meant, the check answers once and carries on. Each finished company's request id
    is saved in data/spread/spread_v<version>.json, so a run that stops can be started again
    and only the missing companies are run. Your saved profile is put back afterwards, so
    the check does not change it.

    Report, for the companies in the batch (or every logged analysis with --all):
      the table      verdict, confidence, weighted score and the five sub-scores per company
      the spread     how many of each verdict, and the weighted scores from lowest to highest
      the pull       each sub-score's average and how often it is near 0 or missing, and how
                     much each adds to the weighted score on average
      what-ifs       how the verdicts would split with the lines at +-0.5 instead of +-0.75,
                     and without economic fit and news (the two the US replay left out, where
                     verdicts did spread)
    It only reports: nothing in the scorecard is changed.

Costs: about $0.02 in model calls, 2 Tavily credits and up to about 7 Trendlyne calls per
company (Trendlyne's daily limit is 400), and about 90 seconds each.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from pathlib import Path
from statistics import mean, median

from invest_agent.analysis import scorecard
from invest_agent.common.logging_setup import setup_logging
from invest_agent.common.yaml_loader import load_yaml
from invest_agent.config import get_settings
from invest_agent.observability import decision_log
from invest_agent.profile.store import ProfileStore
from invest_agent.storage.db import connect

logger = logging.getLogger(__name__)

SUBS = ("business_quality", "valuation", "macro_fit", "news", "forecast")
SHORT = {"business_quality": "quality", "valuation": "value", "macro_fit": "economy", "news": "news",
         "forecast": "forecast"}
NEAR_ZERO = 0.25


def companies() -> dict:
    return load_yaml("eval/spread_companies.yaml")


def batch_path(version: int) -> Path:
    folder = Path(get_settings().database_path).parent / "spread"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"spread_v{version}.json"


# --- Running the companies ----------------------------------------------------------------------

async def run_all(limit: int | None = None) -> dict:
    """Ask about each company not yet in the batch file. Returns the batch {query: request_id}."""
    from invest_agent.main import run_turn  # imported here: it builds the graph

    cfg = companies()
    path = batch_path(cfg["version"])
    batch: dict = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    todo = [c for c in cfg["companies"] if c["query"] not in batch]
    if limit:
        todo = todo[:limit]
    store = ProfileStore()
    saved = store.load()                               # put back at the end
    try:
        for n, c in enumerate(todo, 1):
            message = f"Should I invest in {c['query']}? {cfg['profile_text']}"
            print(f"[{n}/{len(todo)}] {c['query']} ...", flush=True)
            try:
                result, carry = await run_turn(message, {}, quiet=True)
                if result.get("ended_with_question"):
                    reply = c.get("reply") or c["query"]
                    print(f"    the agent asked: {result.get('final_response', '')[:120]!r}; replying {reply!r}")
                    result, carry = await run_turn(reply, carry, quiet=True)
            except Exception as err:                    # noqa: BLE001 - one company must not stop the batch
                print(f"    failed: {type(err).__name__}: {err}")
                continue
            if result.get("ended_with_question") or not result.get("verdict"):
                print(f"    no analysis: {(result.get('final_response') or '')[:160]!r}")
                batch[c["query"]] = {"request_id": None, "note": (result.get("final_response") or "")[:300]}
            else:
                print(f"    {result.get('verdict')} ({result.get('confidence')}), "
                      f"score {((result.get('scorecard') or {}).get('score') or 0):+.2f}, "
                      f"{(result.get('run') or {}).get('latency_s')} s")
                batch[c["query"]] = {"request_id": (result.get("run") or {}).get("request_id")
                                     or result.get("request_id")}
            path.write_text(json.dumps(batch, indent=1), encoding="utf-8")
    finally:
        if saved:
            store.save(saved)
        else:
            store.clear()
    return batch


# --- Reading the results ------------------------------------------------------------------------

def rows_from_log(request_ids: list[str] | None) -> list[dict]:
    """One row per analysis: ticker, verdict, confidence, weighted score, sub-scores."""
    conn = connect()
    if request_ids is None:
        ids = [r["request_id"] for r in conn.execute("SELECT request_id FROM decision_log ORDER BY created_at")]
    else:
        ids = [i for i in request_ids if i]
    rows = []
    for rid in ids:
        e = decision_log.get(rid, conn)
        if not e:
            continue
        scores = (e["payload"].get("scores") or {})
        card = scores.get("scorecard") or {}
        rows.append({"ticker": e["ticker"], "market": (e["payload"].get("company") or {}).get("market"),
                     "verdict": e["verdict"], "confidence": e["confidence"], "score": card.get("score"),
                     "subs": scores.get("sub_scores") or {}, "weights": card.get("weights_used") or {},
                     "low_reliability": card.get("low_reliability") or [], "capped": card.get("capped"),
                     "config_version": e.get("config_version")})
    return rows


def _rescore(row: dict, drop: tuple[str, ...] = ()) -> float | None:
    subs = {k: v for k, v in row["subs"].items() if k not in drop}
    return scorecard.weighted(subs, [k for k in row["low_reliability"] if k not in drop])["score"]


def _split(scores: list[float | None], line: float, low_line: float | None = None) -> dict:
    low_line = -line if low_line is None else low_line
    out = {"favourable": 0, "neutral": 0, "unfavourable": 0}
    for s in scores:
        if s is None:
            continue
        out["favourable" if s >= line else "unfavourable" if s <= low_line else "neutral"] += 1
    return out


def report(rows: list[dict]) -> dict:
    scored = [r for r in rows if r["score"] is not None]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    pull = {}
    for k in SUBS:
        values = [r["subs"].get(k) for r in scored]
        present = [v for v in values if v is not None]
        contrib = [r["subs"][k] * r["weights"].get(k, 0) for r in scored if r["subs"].get(k) is not None]
        pull[k] = {"average": mean(present) if present else None,
                   "near_zero": sum(1 for v in present if abs(v) < NEAR_ZERO),
                   "missing": len(values) - len(present),
                   "average_contribution": mean(contrib) if contrib else None}
    t = scorecard.config()["verdict_thresholds"]
    line, low_line = t["favourable_above"], t["unfavourable_below"]
    return {
        "count": len(rows), "verdicts": counts,
        "scores": sorted(r["score"] for r in scored),
        "inside_neutral_band": sum(1 for r in scored if low_line < r["score"] < line),
        "pull": pull,
        "what_if": {
            "lines at +-0.5": _split([r["score"] for r in scored], 0.5),
            "without economic fit and news": _split([_rescore(r, ("macro_fit", "news")) for r in scored], line,
                                                    low_line),
            "both": _split([_rescore(r, ("macro_fit", "news")) for r in scored], 0.5),
        },
        "line": line, "low_line": low_line,
    }


def text(rows: list[dict], rep: dict) -> list[str]:
    def f(v):
        return "   -  " if v is None else f"{v:+.2f}"
    lines = [f"Verdict spread: {rep['count']} analyses", "",
             f"{'Company':<14}{'Verdict':<14}{'Conf.':<8}{'Score':>7}  "
             + "".join(f"{SHORT[k]:>9}" for k in SUBS)]
    for r in sorted(rows, key=lambda r: (r["score"] is None, -(r["score"] or 0))):
        lines.append(f"{(r['ticker'] or '?')[:13]:<14}{(r['verdict'] or '?')[:13]:<14}{(r['confidence'] or '')[:7]:<8}"
                     f"{f(r['score']):>7}  " + "".join(
                         f"{f(r['subs'].get(k)) + ('*' if k in r['low_reliability'] else ' '):>9}" for k in SUBS))
    lines += ["(* low reliability, counted at half weight)", ""]
    lines.append("Verdicts: " + ", ".join(f"{k} {v}" for k, v in sorted(rep["verdicts"].items())))
    if rep["scores"]:
        s = rep["scores"]
        lines.append(f"Weighted scores: lowest {s[0]:+.2f}, middle {median(s):+.2f}, highest {s[-1]:+.2f}; "
                     f"{rep['inside_neutral_band']} of {len(s)} inside the neutral band (between {rep['low_line']:+g} "
                     f"and {rep['line']:+g})")
    lines += ["", "What each sub-score does (average; near 0 means within 0.25; contribution = score x weight used):"]
    for k in SUBS:
        p = rep["pull"][k]
        lines.append(f"  {SHORT[k]:<9} average {f(p['average'])}, near 0 in {p['near_zero']}, missing in "
                     f"{p['missing']}, adds {f(p['average_contribution'])} to the weighted score on average")
    lines += ["", "What if (counts of favourable / neutral / unfavourable):"]
    for name, split in rep["what_if"].items():
        lines.append(f"  {name}: {split['favourable']} / {split['neutral']} / {split['unfavourable']}")
    return lines


def save_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "market", "verdict", "confidence", "score", *SUBS, "low_reliability", "config_version"])
        for r in rows:
            w.writerow([r["ticker"], r["market"], r["verdict"], r["confidence"], r["score"],
                        *[r["subs"].get(k) for k in SUBS], " ".join(r["low_reliability"]), r["config_version"]])


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    setup_logging()
    parser = argparse.ArgumentParser(description="Verdict spread check")
    parser.add_argument("--limit", type=int, help="run only this many companies (a trial)")
    parser.add_argument("--report", action="store_true", help="only print the results, run nothing")
    parser.add_argument("--all", action="store_true", help="with --report: every analysis in the decision log")
    parser.add_argument("--redo", nargs="+", metavar="NAME",
                        help="run these companies again (any part of the name, e.g. --redo Pfizer Alphabet)")
    args = parser.parse_args()

    version = companies()["version"]
    if args.redo:
        path = batch_path(version)
        batch = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        dropped = [q for q in batch if any(n.lower() in q.lower() for n in args.redo)]
        for q in dropped:
            del batch[q]
        path.write_text(json.dumps(batch, indent=1), encoding="utf-8")
        print("Running again: " + (", ".join(dropped) or "nothing matched"))
    if not args.report:
        asyncio.run(run_all(args.limit))
    path = batch_path(version)
    if args.all:
        rows = rows_from_log(None)
    else:
        batch = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        rows = rows_from_log([v.get("request_id") for v in batch.values()])
    if not rows:
        print("No analyses to report yet.")
        return
    print("\n".join(text(rows, report(rows))))
    out = path.with_suffix(".csv")
    save_csv(rows, out)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()

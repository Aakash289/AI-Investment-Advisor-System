"""
common/records.py: one way to create the SourceRecord that every retrieved number travels in.

What this file does:
    Every agent turns what it fetched into SourceRecord dicts (declared in
    graph/state.py). Building them by hand in every agent would lead to slightly
    different IDs and missing fields, so all agents call `make_record(...)` here.

    The record ID matters: Synthesis cites record IDs next to each number it writes,
    and the grounding guardrail looks the number up by that ID. So the ID is built
    from what the number is (source, entity, field, period), never from a random
    value, which means the same figure gets the same ID on every run and in the
    backtesting replay.
"""

from datetime import datetime, timezone
from typing import Any

from invest_agent.graph.state import SourceRecord, SourceTier


def utc_now_iso() -> str:
    """Current time in UTC as an ISO 8601 string, used for as_of and log times."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_record_id(source: str, entity: str, field: str, period: str | None = None) -> str:
    """Build a stable, readable ID such as "sec_edgar:AAPL:revenue:FY2024".

    Spaces are replaced so the ID survives being written inside a sentence.
    """
    parts = [source, entity, field] + ([period] if period else [])
    return ":".join(str(p).strip().replace(" ", "_") for p in parts)


def make_record(
    *,
    source: str,
    tier: SourceTier,
    entity: str,
    field: str,
    value: Any,
    as_of: str | None = None,
    period: str | None = None,
    **extra: Any,
) -> SourceRecord:
    """Create a SourceRecord with a stable ID.

    The leading * makes every argument keyword-only, so a call always reads like
    make_record(source="sec_edgar", tier=1, entity="AAPL", field="revenue", ...)
    and arguments cannot be swapped by position.

    extra: any optional SourceRecord keys (unit, fiscal_year, period_end,
    accession_number, source_ref, release_date, projected, flags).
    """
    if value is None:
        # Design rule (14.3): a missing figure stays missing. Callers record the gap
        # for the completeness scorer instead of creating an empty record.
        raise ValueError(f"Refusing to create a record with no value for {source}:{entity}:{field}")

    record: SourceRecord = {
        "record_id": make_record_id(source, entity, field, period),
        "field": field,
        "value": value,
        "source": source,
        "tier": tier,
        "as_of": as_of or utc_now_iso(),
        "entity": entity,
    }
    # Only copy the optional keys that were actually given, so the record never
    # carries keys with None values.
    record.update({k: v for k, v in extra.items() if v is not None})  # type: ignore[typeddict-item]
    return record

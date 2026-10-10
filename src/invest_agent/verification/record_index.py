"""
verification/record_index.py: quick lookups over the records the agents gathered.

What this file does:
    The verification checks keep asking the same questions of the records: what is the
    latest revenue, which fiscal years does net income cover, what is the current price.
    RecordIndex answers them, so each check reads like the rule it applies instead of a
    loop over a list.

    Fundamentals records have a fiscal year (one record per figure per year); price
    records have none (one record per figure, as of the last trading day).
"""

from __future__ import annotations


class RecordIndex:
    def __init__(self, records: list[dict]):
        self.records = records
        self.by_field: dict[str, list[dict]] = {}
        for r in records:
            self.by_field.setdefault(r["field"], []).append(r)
        for rows in self.by_field.values():
            rows.sort(key=lambda r: r.get("fiscal_year") or 0, reverse=True)     # newest first

    def has(self, field: str) -> bool:
        return field in self.by_field

    def latest(self, field: str) -> dict | None:
        rows = self.by_field.get(field)
        return rows[0] if rows else None

    def value(self, field: str, year: int | None = None) -> float | None:
        """The latest value, or the value for one fiscal year."""
        if year is None:
            r = self.latest(field)
            return None if r is None else r["value"]
        for r in self.by_field.get(field, []):
            if r.get("fiscal_year") == year:
                return r["value"]
        return None

    def years(self, field: str) -> list[int]:
        """Fiscal years held for a figure, newest first."""
        return [r["fiscal_year"] for r in self.by_field.get(field, []) if r.get("fiscal_year") is not None]

    def latest_year(self) -> int | None:
        """The latest fiscal year with revenue: the year the analysis treats as current."""
        years = self.years("revenue")
        return years[0] if years else None

"""
Unit test: every YAML config file in the package loads, and the values decided on
4 October 2026 are what the design documents say (Sections 14.1 to 14.14).

Run with: uv run pytest tests/unit/test_config_files.py -v
"""

from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[2] / "src" / "invest_agent"


def load(relative: str) -> dict:
    return yaml.safe_load((PKG / relative).read_text(encoding="utf-8"))


def test_every_yaml_file_parses():
    files = list(PKG.rglob("*.yaml"))
    assert files, "no config files found"
    for f in files:
        assert yaml.safe_load(f.read_text(encoding="utf-8")) is not None, f


def test_decided_values():
    entity = load("mcp_servers/entity/entity_config.yaml")
    assert entity["match"] == {"min_score": 90, "min_lead": 10}
    assert entity["cross_listing"]["behaviour"] == "always_ask"

    price = load("mcp_servers/market_data/price_config.yaml")
    assert price["beta"]["window_years"] == 2 and price["beta"]["frequency"] == "weekly"
    assert price["thinly_traded"]["INR"] == 50_000_000  # Rs 5 crore

    coverage = load("verification/coverage_rules.yaml")
    assert coverage["min_market_value"]["INR"] == 5_000_000_000  # Rs 500 crore
    assert "20-F" in coverage["not_covered"]["sec_annual_forms"]

    scorecard = load("analysis/scorecard_config.yaml")
    assert sum(scorecard["weights"].values()) == 100
    assert scorecard["suitability"]["high_volatility_annualised"] == 0.35

    news = load("mcp_servers/news/trusted_domains.yaml")
    assert "moneycontrol.com" in news["IN"]
    assert news["spike_rule"]["min_baseline_articles"] == 5

    intake = load("profile/intake_config.yaml")
    assert intake["existing_holdings"]["ask"] is False

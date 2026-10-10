"""
scripts/fx_probe.py: one-off check of the free FX sources before step 6 is built.

What this file does:
    Step 6 converts amounts between currencies (design 14.8). Before writing the code
    we look at what each source really sends back, the same way we probed Trendlyne
    before step 5. It asks:

      Frankfurter (free, no key)
        A. the latest USD to INR rate
        B. the same, pinned to FBIL (India's official rupee reference rate)
        C. the rate for a Sunday, to see which earlier date it falls back to
        D. twelve months of USD to INR, one value a month (for the currency risk note)
        E. the latest USD to EUR rate (a pair FBIL does not publish on its own)
        F. the FBIL and ECB entries from the provider list, to see how fresh each is
      yfinance
        G. the INR=X market rate, our last resort
      FRED (only if FRED_API_KEY is in .env)
        H. the last five DEXINUS values, rupees per dollar

    It prints a short summary of each answer and saves the full answers into
    tests/fixtures/fx/, so the unit tests can use the real format without the network.

    No secrets are printed: web addresses are never shown, and the FRED key is passed
    as a parameter, not written into anything that gets printed or saved.

Run with:
    uv run python scripts/fx_probe.py
"""

import json
from datetime import date, timedelta
from pathlib import Path

# Importing the package first switches on the rule that keeps secrets out of logs.
import invest_agent  # noqa: F401
import httpx

from invest_agent.config import get_settings

FRANKFURTER = "https://api.frankfurter.dev"
FIXTURES = Path("tests/fixtures/fx")
FIXTURES.mkdir(parents=True, exist_ok=True)


def last_sunday(today: date) -> date:
    """The most recent Sunday before today (weekday 6 is Sunday)."""
    return today - timedelta(days=(today.weekday() + 1) % 7 or 7)


def show(label: str, response: httpx.Response, save_as: str | None = None, max_chars: int = 400) -> None:
    """Print the status and the start of the answer, and save the full answer."""
    print(f"\n--- {label}: HTTP {response.status_code}")
    text = response.text
    print(text[:max_chars] + (" ..." if len(text) > max_chars else ""))
    if save_as and response.status_code == 200:
        (FIXTURES / save_as).write_text(text, encoding="utf-8")
        print(f"(saved to tests/fixtures/fx/{save_as})")


def main() -> None:
    today = date.today()
    sunday = last_sunday(today)
    year_ago = today - timedelta(days=365)

    # httpx.Client keeps one connection open for all the Frankfurter calls.
    with httpx.Client(base_url=FRANKFURTER, timeout=15) as client:
        show("A. latest USD/INR", client.get("/v2/rate/USD/INR"), "frankfurter_usd_inr_latest.json")
        show("B. latest USD/INR, FBIL only", client.get("/v2/rate/USD/INR", params={"providers": "FBIL"}),
             "frankfurter_usd_inr_fbil.json")
        show(f"C. USD/INR on Sunday {sunday}", client.get("/v2/rate/USD/INR", params={"date": sunday.isoformat()}),
             "frankfurter_usd_inr_sunday.json")
        show("D. 12 months USD/INR, monthly",
             client.get("/v2/rates", params={"base": "USD", "quotes": "INR", "from": year_ago.isoformat(),
                                             "to": today.isoformat(), "group": "month"}),
             "frankfurter_usd_inr_12m.json", max_chars=700)
        show("E. latest USD/EUR", client.get("/v2/rate/USD/EUR"), "frankfurter_usd_eur_latest.json")

        providers = client.get("/v2/providers")
        print(f"\n--- F. provider list: HTTP {providers.status_code}")
        if providers.status_code == 200:
            data = providers.json()
            rows = data if isinstance(data, list) else data.get("providers", data)
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and row.get("key") in ("FBIL", "ECB"):
                    print({k: row.get(k) for k in ("key", "name", "start_date", "end_date", "publishes_missed")})

    # yfinance: INR=X is the market rate for rupees per dollar.
    print("\n--- G. yfinance INR=X")
    try:
        import yfinance as yf
        hist = yf.Ticker("INR=X").history(period="5d")
        print(hist["Close"].tail(5).round(4).to_string())
    except Exception as exc:  # the probe should carry on whatever happens here
        print("yfinance failed:", type(exc).__name__, exc)

    # FRED: DEXINUS is rupees per dollar, published with a lag of about a week.
    print("\n--- H. FRED DEXINUS")
    key = get_settings().fred_api_key
    if not key:
        print("skipped: FRED_API_KEY is not in .env yet")
        return
    response = httpx.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={"series_id": "DEXINUS", "api_key": key, "file_type": "json",
                "sort_order": "desc", "limit": 5},
        timeout=15,
    )
    print(f"HTTP {response.status_code}")
    if response.status_code == 200:
        obs = response.json().get("observations", [])
        for o in obs:
            print(o.get("date"), o.get("value"))
        (FIXTURES / "fred_dexinus_latest.json").write_text(json.dumps({"observations": obs}, indent=1), encoding="utf-8")
        print("(saved to tests/fixtures/fx/fred_dexinus_latest.json)")


if __name__ == "__main__":
    main()

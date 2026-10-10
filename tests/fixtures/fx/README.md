# tests/fixtures/fx

Real answers from the free FX sources, saved by scripts/fx_probe.py on 5 October 2026, so the FX unit tests run without the network:

- frankfurter_usd_inr_latest.json: latest USD to INR (the blended official rate, dated today)
- frankfurter_usd_inr_fbil.json: the same pinned to FBIL, dated 28 September because FBIL's feed was behind
- frankfurter_usd_inr_sunday.json: the rate for a Sunday
- frankfurter_usd_inr_12m.json: one USD to INR rate a month for 12 months (the currency risk note)
- frankfurter_usd_eur_latest.json: latest USD to EUR
- fred_dexinus_latest.json: the last five DEXINUS values (rupees per dollar), about a week behind

They hold rates only, never a key or web address. Run the probe again to refresh them.

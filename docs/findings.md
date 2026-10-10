# What the testing found

The agent turns evidence into a verdict with a fixed scorecard. Before trusting that
scorecard, I wanted to know two things: does its verdict mean anything about what a
stock does next, and does it tell very different companies apart? This page sets out
how each was tested, what came back, and what changed because of it.

## 1. A replay of the past (US companies, 2022 to 2025)

### How it works

The agent cannot be scored on live verdicts until they are a year old (the first one
matures in October 2027). So the replay rebuilds past dates instead:

- **Dates:** the end of March in 2022, 2023, 2024 and 2025.
- **Companies:** on each date, the 50 largest US companies by market value on that date,
  chosen from about 100 candidates. Many of the candidates later fell (Intel, PayPal,
  Boeing, 3M, Walgreens), so the list is not picked with hindsight.
- **Only what was known then:** SEC figures exactly as they had been filed by that date
  (later restatements are left out), share prices cut at that date and corrected for
  later stock splits, and peer figures for the latest year fully reported by then.
- **The same scoring code as the live agent:** the live verification and scorecard, run
  unchanged. News, economic fit and geography are left out, as their history cannot be
  rebuilt for free; their weight is shared among the other sub-scores.
- **The test:** each verdict is scored on the stock's return over the next 12 months
  minus the S&P 500's return over the same days.

195 verdicts could be scored.

### What came back

| Verdict | Count | Return against the S&P 500 over the next 12 months |
|---|---|---|
| Favourable | 103 | +0.1% |
| Neutral | 81 | +1.3% |
| Unfavourable | 11 | -7.4% |

By weighted score:

| Weighted score | Stocks | Against the S&P 500 | Share that beat it |
|---|---|---|---|
| -0.75 or below | 11 | -7.4% | 36% |
| -0.75 to -0.50 | 4 | -4.8% | 50% |
| -0.50 to -0.25 | 6 | -20.6% | 17% |
| -0.25 to +0.25 | 18 | +3.3% | 39% |
| +0.25 to +0.50 | 30 | +3.2% | 50% |
| +0.50 to +0.75 | 26 | +0.8% | 50% |
| above +0.75 | 100 | +0.9% | 47% |

Which sub-scores separated good from bad (average return against the S&P 500 for stocks
scoring +1 or more / in between / -1 or less):

| Sub-score | High | Middle | Low |
|---|---|---|---|
| Business quality | +1.3% (111) | -1.4% (62) | -16.5% (3) |
| Valuation | -0.5% (57) | -2.7% (51) | +2.3% (65) |
| Forecast | -2.9% (60) | +1.1% (36) | -8.3% (26) |

### What it means

**The scorecard spots weak stocks, but it does not pick winners.**
- Every band below -0.25 trailed the market (21 stocks, about -11% on average).
- Everything above -0.25 did about as well as the market, and the highest scores did no
  better than the middle.
- Weak business quality and a poor forecast pointed the right way. Valuation, over these
  four years, barely mattered.
- Confidence came out in the right order (high 44% right, medium 39%, low 37%), but the
  gaps are small.

A first attempt used today's 50 largest companies on every date. That flattered the
stocks that grew (Nvidia, Broadcom, Eli Lilly, Meta) and made valuation look as if it
worked backwards (stocks scored expensive beat the market by 8 points). Choosing the
companies by their size on each date removed most of that tilt, which is why the list
is built that way.

### What changed because of it

- **The weights stayed.** A tuning check tried other weights and verdict lines on 70% of
  the verdicts and checked them on the other 30%. A change is only suggested if
  favourable picks then beat the rest on the 30% the check never saw. No setting did, so
  the problem is not the weights.
- **Only the unfavourable line moved,** from -0.75 to -0.5 (scorecard version 5). That is
  the smaller of the two steps the bands support, since the bands between -0.75 and -0.25
  hold only 10 stocks.
- **The report says it plainly.** Under the verdict and in its limits section: low scores
  trailed the index, favourable verdicts did only about as well as it, so read the verdict
  as a summary of the evidence, not a prediction of the share price.

### Cautions

- **It is closer to 4 tests than to 195.** Four dates in one market cycle, led by
  expensive AI and growth stocks.
- **Survivorship is reduced, not removed.** Companies taken over or delisted before today
  cannot be priced and are missing.
- **Today's peer list is used for every past date.**
- **Three sub-scores are left out:** news, economic fit and geography, which the live
  agent uses.

## 2. A spread check today (24 companies, India and US)

### Why

Every live run in the first days came out Neutral (Reliance, Apple, Eternal, Star
Health), which raised a fair question: does it say Neutral for everything?

### How it works

24 companies chosen to differ, run through the full live agent with one fixed profile
(5 lakhs, medium risk, 10 years, growth), so only the company changes:
- **Steady quality:** TCS, Coca-Cola, Johnson & Johnson.
- **Quality at very high prices:** Asian Paints, Nestle India, Nvidia, Microsoft.
- **Shrinking or loss-making:** Intel, Boeing, Vodafone Idea, Zee, Paytm.
- **Cheap cyclicals:** Coal India, ONGC, Tata Steel, Ford.
- **Banks and lenders:** HDFC Bank, Bajaj Finance.
- **The rest:** ITC, Sun Pharma, IRCTC, Pfizer, Alphabet, Target.

### What came back (10 October, scorecard version 6)

| Verdict | Companies |
|---|---|
| Favourable (9) | Nvidia, Coal India, ITC, Coca-Cola, Alphabet, IRCTC, Target, Sun Pharma, Microsoft |
| Neutral (10) | Johnson & Johnson, TCS, ONGC, Pfizer, Nestle India, Bajaj Finance, Asian Paints, HDFC Bank, Tata Steel, Paytm |
| Unfavourable (5) | Ford, Vodafone Idea, Zee, Boeing, Intel |

Each verdict can be read off its sub-scores:
- **Favourable:** strong businesses at fair or cheap prices.
- **Neutral, mostly:** good businesses whose high price cancels out their quality (Bajaj
  Finance, Asian Paints, HDFC Bank).
- **Unfavourable:** weak or loss-making businesses.

### What it found, and what was fixed

| Found | Fix |
|---|---|
| Pfizer and Johnson & Johnson had no business quality score: Pfizer reports no operating income line | Operating income rebuilt from pre-tax income plus interest, marked as rebuilt |
| Pfizer came out "high confidence" on 2 of 5 sub-scores, both zero | High confidence now needs at least 4 of the 5 |
| Alphabet had no share count (three share classes), so no valuation | Share count from the balance sheet, then net income over EPS, then Yahoo |
| Microsoft had no EBITDA: it reports depreciation and amortisation as two lines | The two lines are added together when there is no combined figure |
| Weak companies at -0.5 to -0.6 stayed Neutral | The replay's score bands supported moving the unfavourable line to -0.5 |
| Microsoft flipped between Neutral and Favourable as its price moved: its P/E sat exactly on a band edge | Valuation and forecast scores now slide between bands (version 6), and the answer says when a score is within 0.1 of a line |
| ITC (4 tobacco peers) and Nestle India had no valuation | Indian peers widen to the sector when the industry has fewer than 8; medians of 3 or 4 peers allowed; both count at half weight. Nestle also needed a search by name, as its code was not found |

Valuation is still missing for 3 of the 24:
- **Ford:** its US peers mostly file abroad or make losses.
- **Vodafone Idea and Zee:** both make losses, so they have no usable P/E.

## In one line

The agent is a careful way to put the evidence about one stock in front of you, and it
is better at warning you off a weak stock than at finding the next winner. That is how
its answers and reports describe it.

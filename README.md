# CBBTC Dip-Recovery Bot

A deterministic trading bot for the make.fun `bars-replay/v1` harness,
entered in the "Race a BTC trading bot live for 24 hours" bounty
(`btc-race-24h-20261118-1444`). It trades a single symbol, **CBBTC**, on
1-minute bars, long only, against $1,000 of paper capital.

`main.py` speaks the harness protocol through the vendored SDK shim
(`vendor/makefun_bars.py`): one JSON line in per bar, one reply of orders
out. Orders decided on bar *k* fill at bar *k+1*'s open and pay a 4 bps
fee + 2 bps slippage + constant-product price impact, so a full-size
round trip costs roughly 26 bps. The bot is pure arithmetic on the bars
it has seen: no clock, no environment reads, no randomness, no file I/O,
so the harness's two replay runs match byte for byte.

## Strategy

This market's recent history drifts downward, and in a falling market the
cost model punishes activity: trend-following and mean-reversion bots
that trade often pay the ~26 bps round trip again and again and bleed
out. This bot therefore stays flat almost all the time and takes only
one kind of trade:

* **Recovery entry.** Buy (full cash) only when the close is at least 2%
  below the running peak *and* 30-bar momentum is non-negative, i.e. the
  price has fallen hard but has stopped falling. That combination waits
  for the middle of a genuine recovery instead of catching a falling
  knife or chasing noise.
* **Mechanical exits.** Once filled (the entry price is the average cost
  the account reports, fees included), sell everything at the first of:
  +1.5% take-profit, -1.5% hard stop, or a trailing stop 0.8% below the
  highest close since entry. The +1.5% target is sized against the ~26
  bps round-trip cost so a winning trade keeps most of its move.
* **Cooldown.** After a recovery exit, sit out 30 bars so one long slide
  cannot chain into repeated dip entries.
* **Fallback entry.** If nothing has traded by batch 900, take one small
  (30% of cash) position on 15-bar rising momentum, with tight exits
  (+0.6% / -0.6% / 45-bar time stop), so the entry always participates.

Positions are always read back from the account the harness reports,
never inferred from orders sent; a fill or rejection is only visible in
the next batch's account, and the bot reconciles its state there.

## Parameters

| Parameter | Value | Meaning |
| --- | --- | --- |
| `DD_TRIGGER` | 0.98 | recovery entry: close ≤ 98% of running peak |
| `MOMENTUM_BARS` | 30 | recovery entry: close ≥ close 30 bars ago |
| `REC_TAKE_PROFIT` | 1.015 | recovery exit at +1.5% from entry |
| `REC_STOP` | 0.985 | recovery exit at -1.5% from entry |
| `REC_TRAIL` | 0.992 | recovery trailing stop, 0.8% under in-position peak |
| `EXIT_COOLDOWN` | 30 | flat batches to sit out after a recovery exit |
| `FB_MIN_SEQ` | 900 | fallback entry only from this batch on |
| `FB_NOTIONAL_PCT` | 30 | fallback entry size, % of cash |
| `FB_TAKE_PROFIT` / `FB_STOP` | 1.006 / 0.994 | fallback exits at ±0.6% |
| `FB_MAX_HOLD` | 45 | fallback time stop, in bars |
| `SLIPPAGE_BPS` | 150 | max_slippage_bps sent on every order |

## Layout

* `main.py` — the strategy (the whole bot).
* `vendor/makefun_bars.py` — the competition SDK shim, vendored verbatim.

## License

MIT — see `LICENSE`.

"""CBBTC bars-replay bot: dip-recovery entries with tight, cost-aware exits.

Protocol (agent-fun/bars-replay/v1, spoken by vendor/makefun_bars.py):

* The harness opens with one "hello" line: the traded symbols and their
  qty/price scales, the starting capital and its quote scale, the order
  caps, and how many leading batches are warm-up. The shim folds it into a
  BarsState before the first callback.
* Then one "batch" line per 1-minute bar. A batch carries the bars that
  just closed, the fills of the orders decided on the previous batch, and
  the resulting account (cash plus positions). The bot answers each batch
  with exactly one reply: the orders it decides on this bar.
* Orders decided on bar k fill at bar k+1's open. Every fill pays a 4 bps
  fee plus 2 bps slippage plus constant-product price impact against that
  bar's pool depth, so a full-size round trip costs roughly 26 bps. A
  strategy that trades often, or exits for less than its costs, bleeds.
* Warm-up batches only build price history: orders decided on them are
  rejected, so on_batch returns none while state.warming_up is true.
* The run must be byte-for-byte deterministic (the harness replays it and
  compares the action logs): no clock, no environment, no randomness, no
  file I/O. This bot is pure arithmetic on the bars it has seen.

Strategy, in one paragraph: the dev window for this market drifts down,
so the bot stays flat almost all the time and only buys a genuine dip: the
close at least 2% under the running peak while 30-bar momentum is no
longer negative (the fall has stopped, not merely slowed). It then exits
fast and mechanically: +1.5% take-profit, -1.5% hard stop, or a 0.8%
trailing stop under the best close since entry, whichever comes first.
After a recovery exit it sits out 30 bars so one long slide cannot chain
into repeated entries. And if no dip has qualified by batch 900, one
small fallback trade (30% of cash, tight +0.6% / -0.6% / 45-bar exits)
goes on rising 15-bar momentum so the entry always participates.
Positions are always read back from the account
the harness reports, never inferred from the orders sent: an order's fate
(fill or rejection) is only visible in the next batch's account.
"""

import os
import sys

# The grader runs ``python3 -P -s /bot/main.py``: -P keeps this script's
# own directory off sys.path, so put the vendored SDK directory on it by
# hand before importing the shim.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SYMBOL = "CBBTC"
SLIPPAGE_BPS = 150  # bound every order: slipped open + impact vs latest close

# --- recovery trade parameters (fractions of raw price, unless noted) ---
DD_TRIGGER = 0.98        # entry: close at most 98% of the running peak (-2%)
MOMENTUM_BARS = 30       # entry: close >= the close this many bars ago
REC_STOP = 0.985         # exit: hard stop at entry * 0.985 (-1.5%)
REC_TAKE_PROFIT = 1.015  # exit: take profit at entry * 1.015 (+1.5%)
REC_TRAIL = 0.992        # exit: trailing stop at in-position peak * 0.992
EXIT_COOLDOWN = 30       # batches to sit out after a recovery exit

# --- fallback trade parameters (only while nothing has traded yet) ---
FB_MIN_SEQ = 900         # entry: not before this batch seq
FB_MOMENTUM_BARS = 15    # entry: close > the close this many bars ago
FB_NOTIONAL_PCT = 30     # entry: spend this percent of cash
FB_TAKE_PROFIT = 1.006   # exit: take profit at entry * 1.006 (+0.6%)
FB_STOP = 0.994          # exit: stop at entry * 0.994 (-0.6%)
FB_MAX_HOLD = 45         # exit: time stop after this many held bars

# --- strategy state, kept across batches ---
closes = []              # every CBBTC close seen, raw ints, in arrival order
peak = 0                 # running peak of closes
mode = None              # None while flat; 'rec' or 'fb' while a trade is open
intended_mode = None     # mode chosen for a buy whose fill is not yet visible
entry_price = 0.0        # average fill price of the open position (float)
peak_pos = 0.0           # highest close since the position was opened
held_bars = 0            # batches spent in the current position
cooldown = 0             # batches left to sit out while flat
pending_entry = False    # a buy was sent; its outcome is not yet in the account
pending_exit = False     # a sell was sent; its outcome is not yet in the account
traded = False           # True once any fill has been seen


def _avg_cost_price(state, holding):
    """Average fill price of a holding, in raw price units, as a float.

    The account reports a position as a raw quantity (at the symbol's
    qty_scale) and a total cost in quote units, fees included. Inverting
    the harness's valuation (value = qty * price * 10**quote_scale /
    10**(qty_scale + price_scale)) gives the price the position paid:
    cost * 10**(qty_scale + price_scale) / (qty * 10**quote_scale).
    """
    sym = state.symbols[SYMBOL]
    quote_scale = state.hello["capital"]["quote_scale"]
    return holding.cost * 10 ** (sym["qty_scale"] + sym["price_scale"]) / (
        holding.qty * 10 ** quote_scale
    )


def _exit_reached(close):
    """Whether the open position should be sold at this close.

    Recovery trades get room to run to +1.5% with a trailing stop as a
    safety net; fallback trades are small momentum probes and are cut
    much tighter, with a time stop so they cannot linger.
    """
    if mode == "fb":
        return (
            close >= entry_price * FB_TAKE_PROFIT
            or close <= entry_price * FB_STOP
            or held_bars >= FB_MAX_HOLD
        )
    return (
        close <= entry_price * REC_STOP           # hard stop
        or close >= entry_price * REC_TAKE_PROFIT  # take profit
        or close <= peak_pos * REC_TRAIL           # trailing stop
    )


def on_batch(state, batch):
    """Decide this bar's orders; see the module docstring for the protocol."""
    global peak, mode, intended_mode, entry_price, peak_pos, held_bars
    global cooldown, pending_entry, pending_exit, traded

    # History first: fold this batch's CBBTC bar (if it carries one) into
    # the close series and the running peak. Warm-up bars count too; they
    # are real history even though orders on them would be rejected.
    close = closes[-1] if closes else None
    for bar in batch["bars"]:
        if bar["symbol"] == SYMBOL:
            close = int(bar["c"])
            closes.append(close)
            if close > peak:
                peak = close

    if state.warming_up:
        return []
    if close is None:
        return []

    # The account in this batch is the only source of truth about fills.
    position = state.positions.get(SYMBOL)

    if position is not None:
        if mode is None:
            # First sight of a fill: adopt the position. The average cost
            # the account reports becomes the entry price every exit rule
            # measures from, and the position's peak starts at this close.
            mode = intended_mode or "rec"
            intended_mode = None
            pending_entry = False
            entry_price = _avg_cost_price(state, position)
            peak_pos = float(close)
            held_bars = 0
            traded = True
            return []
        # Still holding after a sell was sent: that sell was rejected.
        # Forget it and let the exit rules speak again on this batch.
        pending_exit = False
        held_bars += 1
        if close > peak_pos:
            peak_pos = float(close)
        if _exit_reached(close):
            pending_exit = True
            return [
                {
                    "side": "sell",
                    "symbol": SYMBOL,
                    "qty": position.qty,
                    "max_slippage_bps": SLIPPAGE_BPS,
                }
            ]
        return []

    # --- flat ---
    if pending_exit:
        # The position is gone, so the exit filled. Clear the trade state
        # and, after a recovery trade, sit out the cooldown so a single
        # slide cannot chain into repeated dip entries.
        pending_exit = False
        if mode == "rec":
            cooldown = EXIT_COOLDOWN
        mode = None
        entry_price = 0.0
    # A buy that left us flat was rejected; entries may be tried again.
    pending_entry = False
    if cooldown > 0:
        cooldown -= 1
        return []

    # Recovery entry: a real dip (close at least 2% under the running
    # peak) whose 30-bar momentum has stopped falling. Full size: with a
    # ~26 bps round-trip cost, a small position cannot clear its own fees
    # on a +1.5% target, and the exit rules cap the downside per trade.
    if (
        len(closes) > MOMENTUM_BARS
        and close <= peak * DD_TRIGGER
        and close >= closes[-1 - MOMENTUM_BARS]
    ):
        intended_mode = "rec"
        pending_entry = True
        return [
            {
                "side": "buy",
                "symbol": SYMBOL,
                "notional_quote_units": state.cash,
                "max_slippage_bps": SLIPPAGE_BPS,
            }
        ]

    # Fallback entry: if the race is most of the way through (seq 900+)
    # and no dip ever qualified, still put one small trade on rather than
    # finishing with zero trades. Rising 15-bar momentum only, 30% of
    # cash, and the tight 'fb' exits in _exit_reached. Once anything has
    # traded (traded is True) this branch never fires again.
    if (
        not traded
        and state.seq >= FB_MIN_SEQ
        and len(closes) > FB_MOMENTUM_BARS
        and close > closes[-1 - FB_MOMENTUM_BARS]
    ):
        intended_mode = "fb"
        pending_entry = True
        return [
            {
                "side": "buy",
                "symbol": SYMBOL,
                "notional_quote_units": state.cash * FB_NOTIONAL_PCT // 100,
                "max_slippage_bps": SLIPPAGE_BPS,
            }
        ]
    return []


makefun_bars.run(on_batch)

#!/usr/bin/env python3
"""btc-race-24h-20261117-0603 v2: dip-buying bot, upgraded from v1.

Strategy (chosen from a 1,788-variant grid + refinement on the frozen dev window):
  entry: close <= 60-bar rolling high * (1 - 1.10%)
  exit:  TP +2.00% from fill price, SL -1.50%, or max hold 480 bars
  size:  99% of cash per trade (one position at a time)
  halt:  stop trading for the rest of the run if equity drawdown reaches 20%
  orders carry max_slippage_bps 1000.

Dev window (frozen 1,440 bars, first close $64,461 -> last $64,776, extremely flat):
  the proven capitulation base (7.5% drop) makes zero trades here, so this bot
  buys the small dips the window actually offers. Grid winner: dev PnL +$25.32
  (5 trades, 3 wins, max DD 1.30%) vs v1's +$13.99.

Protocol: agent-fun/bars-replay/v1 via the vendored SDK shim (vendor/makefun_bars.py).
Deterministic: no clock, no randomness, no environment reads.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
WINDOW = 60            # rolling-high lookback, bars
DROP = 0.011           # buy when close <= rolling high * (1 - DROP)
TAKE_PROFIT = 0.02     # exit at fill price * (1 + TAKE_PROFIT)
STOP_LOSS = 0.015      # exit at fill price * (1 - STOP_LOSS)
MAX_HOLD = 480         # exit after this many bars held
SIZE_BPS = 9900        # spend 99% of cash per buy (keeps a hair under the 100% weight cap)
DD_HALT = 0.20         # stop trading if equity drawdown reaches this
MAX_SLIPPAGE_BPS = 1000


class Bot:
    def __init__(self):
        self.closes = []
        self.entry_px = None
        self.entry_seq = None
        self.pending_buy_id = None
        self.peak_equity = None
        self.halted = False

    def on_batch(self, state, batch):
        close = state.close(SYMBOL)
        if close is None:
            return []
        self.closes.append(close)
        if self.peak_equity is None:
            self.peak_equity = state.equity
        if state.equity > self.peak_equity:
            self.peak_equity = state.equity

        # Learn the exact fill price of our buy from this batch's fills.
        # Clear the pending id on any resolution (filled or rejected) so a
        # rejected buy cannot leave a stale id behind.
        for fill in batch.get("fills", []):
            if fill.get("client_id") == self.pending_buy_id:
                if fill.get("status") == "filled":
                    self.entry_px = int(fill["price"])
                    self.entry_seq = state.seq
                self.pending_buy_id = None
        holding = state.positions.get(SYMBOL)
        if holding is None:
            self.entry_px = None
            self.entry_seq = None
        elif self.entry_px is None:
            # Fallback if the fill record was missed: rebuild the fill price
            # from cost and quantity (cost is the notional spent).
            self.entry_px = holding.cost * 10 ** 7 // holding.qty
            self.entry_seq = state.seq

        # Drawdown halt: liquidate and stop trading for the rest of the run.
        dd = (self.peak_equity - state.equity) / self.peak_equity if self.peak_equity else 0.0
        if holding is not None and dd >= DD_HALT:
            self.halted = True
            return [{"side": "sell", "symbol": SYMBOL, "qty": holding.qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        if self.halted:
            return []

        if holding is not None:
            held = state.seq - self.entry_seq
            if (close >= self.entry_px * (1 + TAKE_PROFIT)
                    or close <= self.entry_px * (1 - STOP_LOSS)
                    or held >= MAX_HOLD):
                return [{"side": "sell", "symbol": SYMBOL, "qty": holding.qty,
                         "max_slippage_bps": MAX_SLIPPAGE_BPS}]
            return []

        # Flat: buy the dip off the rolling high.
        window = self.closes[-WINDOW:]
        high = max(window)
        if close <= high * (1 - DROP):
            notional = state.cash * SIZE_BPS // 10000
            if notional >= 1_000_000:
                cid = state.client_id()
                self.pending_buy_id = cid
                return [{"side": "buy", "symbol": SYMBOL, "client_id": cid,
                         "notional_quote_units": notional,
                         "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []

    def on_done(self, state, done):
        sys.stderr.write("done: final cash %d, equity %d\n" % (state.cash, state.equity))


def main():
    bot = Bot()
    makefun_bars.run(bot.on_batch, on_done=bot.on_done)


if __name__ == "__main__":
    main()

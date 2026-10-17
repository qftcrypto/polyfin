"""Trader behaviour.  Git-tracked on purpose: a run is reconstructable from the repo."""

LOOP_EVERY = 30              # seconds between decision cycles

# -- entry rule ----------------------------------------------------------------
MIN_EDGE = 0.05              # default entry edge (model_p - ask - fee); arms override
MIN_PRICE, MAX_PRICE = 0.05, 0.97   # never buy lottery tickets or pennies-for-dollars
MIN_TAU_S = 120              # no entries in the last 2 minutes (1m bars lag)
MAX_TAU_H = 36               # the backtest scored the last 36h only
NEAR_TIE_BP = 3.0            # inside 30 min, skip if |ln S/ref| < 3bp: Pyth may differ
NEAR_TIE_TAU_S = 1800
# No edge cap (removed 2026-10-02, user decision): research/model_check.py found gaps >=
# 0.25 the model's best band once bad prints are cleaned at load (data.despike); the
# move guard below still catches implausible prices.
MAX_MOVE_DAILY_SD = 4.0      # skip if |ln S/ref| > 4 x the asset's daily sd: bad data, not a move
NOFILL_COOLDOWN_S = 600      # after a miss, wait before trying the same market again

# Two entry slots per market, each allowed one position.  "early" entries are the
# overnight / next-day bets near 0.5 (bigger payout, one correlated market bet);
# "late" entries fall inside the last LATE_WINDOW_H, where the model is most
# accurate (favourites at 0.70-0.85, high win rate).  Separate slots so the early
# entry no longer blocks the late one, and the report can judge each on its own.
LATE_WINDOW_H = 3.0
REPEAT = {"spacing_s": 15 * 60, "max_buys": 10, "market_cap_usd": 30.0}

# Arms: rule variants traded side by side on the same model prices and books.
# Each arm has its own positions and risk limits.  `base` is the reference rule;
# live trades only `base`, early slot (2026-09-30: the late-slot sharpening refit
# swung 1.5 -> 0.75 on one day, and late favourites lost money on real books).
# The e10/e15 paper arms test a higher entry threshold (research/zones.py: on
# real book prices edge 0.05-0.10 broke even, 0.10+ was positive - 2 days only).
ARMS = {
    "paper": {
        # Retired 2026-10-08 (operator): base, confirm, e10, e15, ladder, ladder2, rep_flip and
        # late_blend.  Their records stay in trade.orders (the 10/22 check reads them); the
        # engine still supports rungs, confirm_s and blend.
        # repeat buying (research/repeat.py, research/hedge.py): another $3 every
        # time an edge >= min_edge persists, >= 15 min apart, <= 10 buys and <= $30
        # cost per market.  `flip` = edge the OTHER side (vs the latest buy) needs
        # before switching to it; None = never switch.  A flip therefore always
        # needs the model's agreement, never the market move alone.
        "rep_hold": {"min_edge": 0.05, "slots": {"early"}, "repeat": REPEAT, "flip": None},
        "rep_flip10": {"min_edge": 0.05, "slots": {"early"}, "repeat": REPEAT, "flip": 0.10},
        # Late slot, last hour only (2026-10-02): late, the model is worse than the market
        # 1-3h out but better in the last hour.
        "late_1h": {"min_edge": 0.05, "slots": {"late"}, "max_tau_h": 1.0},
        # two buys (edge 0.05, then 0.10) on the favourite side only (ask >= 0.50), 2026-10-08:
        # real-book backtest +5.6%, 6/7 days, +$1.90 without the best 5 markets; forward fills
        # at >= 0.50 so far about breakeven (live -8.4%, paper +0.8%) - measured here cleanly
        "fav50": {"min_edge": 0.05, "slots": {"early"}, "rungs": [0.05, 0.10],
                  "min_price": 0.50},
        # the same two buys with a 0.15 price floor (research/dust.py, 2026-10-06: asks
        # 0.05-0.15 lost on both price sources - the model overstates long shots there)
        "min15": {"min_edge": 0.05, "slots": {"early"}, "rungs": [0.05, 0.10],
                  "min_price": 0.15},
        # strikes only, with the strikes-sharpened model (2026-10-08, research/strike_repeat.py:
        # real books, 7 days, ~+14-15% per $ at any repeat rate, 6/7 days positive)
        "strike_rep": {"min_edge": 0.05, "slots": {"early"}, "kinds": {"strikes"},
                       "repeat": REPEAT, "flip": None},
        # paper twin of the live rule: 2 buys at edge >= 0.08, 15 min apart, same side,
        # no new entries 18:00-06:00 ET
        "strike2": {"min_edge": 0.08, "slots": {"early"}, "kinds": {"strikes"},
                    "quiet_hours": (18, 6),
                    "repeat": {"spacing_s": 15 * 60, "max_buys": 2, "market_cap_usd": 10.0},
                    "flip": None},
        # the live rule before 2026-10-18 (entries at any hour), to measure the pause
        "strike2_allday": {"min_edge": 0.08, "slots": {"early"}, "kinds": {"strikes"},
                           "repeat": {"spacing_s": 15 * 60, "max_buys": 2, "market_cap_usd": 10.0},
                           "flip": None},
        # strategy v2 (2026-10-18): the live rule (pause included) + day worst-case cap -$30 +
        # tail hedges (hedges may fire at any hour - they only reduce risk)
        "strike2_v2": {"min_edge": 0.08, "slots": {"early"}, "kinds": {"strikes"},
                       "quiet_hours": (18, 6), "worst_cap": 30.0,
                       "hedge": {"trigger": -20.0, "min_edge": -0.05, "max_per_day": 2, "shares": 5},
                       "repeat": {"spacing_s": 15 * 60, "max_buys": 2, "market_cap_usd": 10.0},
                       "flip": None},
        # the live rule + portfolio view (2026-10-17, research/strike_portfolio.py): skip a buy
        # that would put the day's ladder worst case below -$30; when the worst case is below
        # -$20, hedge with 5 shares of the strike/side that most raises it (edge >= -0.05),
        # <= 2 hedges/day (recorded as strike2_hedge+h).  Books +$68 vs +$64 (worst day -$13 vs
        # -$18), forward +$81 vs +$79 (-$11 vs -$14) for the hedge alone; the cap was ~free forward.
        "strike2_hedge": {"min_edge": 0.08, "slots": {"early"}, "kinds": {"strikes"},
                          "worst_cap": 30.0,
                          "hedge": {"trigger": -20.0, "min_edge": -0.05, "max_per_day": 2, "shares": 5},
                          "repeat": {"spacing_s": 15 * 60, "max_buys": 2, "market_cap_usd": 10.0},
                          "flip": None},
        # the live rule on the OLD strikes model, FROZEN at what live used to 10/16 (regime
        # scaling gamma 0.5, strike_b 1.3; stage2.REGIME_FROZEN), to measure the change
        "strike2_regime": {"min_edge": 0.08, "slots": {"early"}, "kinds": {"strikes"},
                           "model": "regime",
                           "repeat": {"spacing_s": 15 * 60, "max_buys": 2, "market_cap_usd": 10.0},
                           "flip": None},
        # the live rule before 2026-10-17 (edge 0.05), kept to measure the change
        "strike2_05": {"min_edge": 0.05, "slots": {"early"}, "kinds": {"strikes"},
                    "repeat": {"spacing_s": 15 * 60, "max_buys": 2, "market_cap_usd": 10.0},
                    "flip": None},
        # strike2 + centre agreement (2026-10-14, research/wti_width.py): trade a strike ladder
        # only while the model's and the market's 0.5 points are within $0.50 - skips the
        # directional days where the market's centre was right (10/12).  0.50 was the
        # cutoff fixed before the sweep (books 0.25-1.50 all beat no filter, forward fragile)
        "strike2_agree": {"min_edge": 0.05, "slots": {"early"}, "kinds": {"strikes"},
                          "centre_tol": 0.50,
                          "repeat": {"spacing_s": 15 * 60, "max_buys": 2, "market_cap_usd": 10.0},
                          "flip": None},
        # strike2 on the PERP-NOWCAST model (2026-10-17, research/perp_value.py): CL=F's stale
        # Yahoo bar moved by the Hyperliquid perp's return since.  Forward 10/12-10/16 on the
        # bot's own books: Brier better 4/5 days (0.1122 -> 0.1111), strike2 +$4 -> +$55.
        "strike2_perp": {"min_edge": 0.05, "slots": {"early"}, "kinds": {"strikes"}, "model": "perp",
                         "repeat": {"spacing_s": 15 * 60, "max_buys": 2, "market_cap_usd": 10.0},
                         "flip": None},
        # strike_rep at edge >= 0.08 (2026-10-17, research/strike_threshold.py): the one cell
        # positive without its best 5 markets on both books (+$16) and forward signals (+$26)
        "strike_rep08": {"min_edge": 0.08, "slots": {"early"}, "kinds": {"strikes"},
                         "repeat": REPEAT, "flip": None},
        # retired 2026-10-17: spymeta2 (SPY/META strikes: 0/3 days, -47%; SPY forward Brier
        # worse than the market).  Arms may still name `series` with min_mkt_volume.
        # up/down only, edge in [0.05, 0.10): the one band that came out ~breakeven-positive
        # (research/ud_threshold.py, 2026-10-08) - kept on record as an option, not a rule
        "ud_band": {"min_edge": 0.05, "max_edge": 0.10, "slots": {"early"}, "kinds": {"updown"}},
    },
    "live": {
        # 2026-10-08 (user): strikes only, 2 buys, 15 min apart, same side, with the
        # strikes-sharpened model (research/strike_repeat.py).  From 2026-10-17 the strikes
        # model has no volatility-regime scaling (stage2.STRIKE_GAMMA = 0, strike_b ~1.20;
        # research/strike_regime.py); strike2_regime keeps the old model on paper.  Edge 0.05 -> 0.08 on
        # 2026-10-17 (user; research/strike_threshold.py: 0.08 beat 0.05 on books +11.1% vs
        # +4.5% and forward +33.3% vs +7.3% - overlapping days, picked from a sweep).
        # From 2026-10-18 (user): no new entries 18:00-06:00 ET (research/strike_strategy.py:
        # evening/overnight entries lost on books and forward; skipping them kept books P&L
        # ~equal with worst day -$5.74 vs -$17.74, forward +$90 vs +$78).
        "strike2": {"min_edge": 0.08, "slots": {"early"}, "kinds": {"strikes"},
                    "quiet_hours": (18, 6),
                    "repeat": {"spacing_s": 15 * 60, "max_buys": 2, "market_cap_usd": 10.0},
                    "flip": None},
    },
}

# -- market scope ---------------------------------------------------------------
MIN_SERIES_VOLUME = 500      # median USD volume of the series' last week of markets
EXCLUDE_SERIES: set[str] = set()

# -- sizing ----------------------------------------------------------------------
BUDGET_USD = 3.0             # target notional per order
MIN_SHARES = 5               # venue minimum (orderMinSize) - at 0.90 that is $4.50
MAX_ORDER_USD = 5.0          # hard cap: skip rather than exceed
FEE_RATE = 0.04              # finance taker fee: rate * p * (1 - p) per share
MAX_SLIP = 0.01              # never pay more than best ask + 1c (user rule, 2026-10-01)

# -- risk limits (counted from the database, so a restart cannot reset them) ----
# Per mode.  Paper has none (2026-10-01): caps only distort a paper arm's sample,
# and left paper/base unable to mirror live once its daily cap was hit.
LIMITS = {
    # The wallet balance is the budget limit (checked before every live order).
    # No exposure or order-count caps (user, 2026-10-02: redundant with the balance).
    "live": {},                        # daily loss stop removed too (user, 2026-10-02)
    "paper": None,
}
STOP_AFTER_FILLS_LIVE = None # live only: pause after this many fills (None = off,
                             # user decision 2026-09-30: pause on request instead)

# -- data freshness ---------------------------------------------------------------
MAX_BARS_AGE_S = 15 * 60     # newest bar of any symbol; older = recorder down

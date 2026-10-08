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
        "base": {"min_edge": 0.05, "slots": {"early", "late"}},
        "e10": {"min_edge": 0.10, "slots": {"early"}},
        "e15": {"min_edge": 0.15, "slots": {"early"}},
        # scale-in: leg 1 at 0.05 on the better side, then add on THAT side when
        # the edge widens to 0.10, then 0.15 (research/ladder.py: add-on legs
        # paid at least as well as the first, 2026-10-01)
        "ladder": {"min_edge": 0.05, "slots": {"early"}, "rungs": [0.05, 0.10, 0.15]},
        # repeat buying (research/repeat.py, research/hedge.py): another $3 every
        # time an edge >= min_edge persists, >= 15 min apart, <= 10 buys and <= $30
        # cost per market.  `flip` = edge the OTHER side (vs the latest buy) needs
        # before switching to it; None = never switch.  A flip therefore always
        # needs the model's agreement, never the market move alone.
        "rep_hold": {"min_edge": 0.05, "slots": {"early"}, "repeat": REPEAT, "flip": None},
        "rep_flip": {"min_edge": 0.05, "slots": {"early"}, "repeat": REPEAT, "flip": 0.05},
        "rep_flip10": {"min_edge": 0.05, "slots": {"early"}, "repeat": REPEAT, "flip": 0.10},
        # live ladder2 + confirmation: a signal must still hold >= confirm_s after first seen,
        # on a later cycle with fresher bars (recorder fetches every 120s).  research/race.py:
        # the bought side fell ~1.6-2.4c in the minute after entry (stale inputs).
        "confirm": {"min_edge": 0.05, "slots": {"early"}, "rungs": [0.05, 0.10],
                    "confirm_s": 120},
        # Late-slot variants (2026-10-02).  Late, the model is worse than the market 1-3h
        # out and overconfident (disagreements: model 0.641, market 0.501, realized 0.532)
        # but better in the last hour.  Compared with base's late slot.
        "late_1h": {"min_edge": 0.05, "slots": {"late"}, "max_tau_h": 1.0},
        # logit P = w_model logit(model) + w_market logit(market mid): weights fitted on
        # late points of 9 days (best late Brier 0.0752 vs model 0.0787 / market 0.0763)
        "late_blend": {"min_edge": 0.05, "slots": {"late"}, "blend": (0.19, 0.92)},
        # live's ladder2 with a 0.15 price floor (research/dust.py, 2026-10-06: asks 0.05-0.15
        # lost on both price sources - the model overstates long shots there)
        # shadow of the live rule, so it keeps being measured while live is off
        "ladder2": {"min_edge": 0.05, "slots": {"early"}, "rungs": [0.05, 0.10]},
        # live's ladder2 on the favourite side only (ask >= 0.50), 2026-10-08: real-book
        # backtest +5.6%, 6/7 days, +$1.90 without the best 5 markets; forward fills at
        # >= 0.50 so far about breakeven (live -8.4%, paper +0.8%) - measured here cleanly
        "fav50": {"min_edge": 0.05, "slots": {"early"}, "rungs": [0.05, 0.10],
                  "min_price": 0.50},
        "min15": {"min_edge": 0.05, "slots": {"early"}, "rungs": [0.05, 0.10],
                  "min_price": 0.15},
    },
    "live": {
        # 2026-10-02: base -> two-buy ladder (research/ladder.py, 8 days; the second
        # buy paid at least as well as the first on every price source)
        "ladder2": {"min_edge": 0.05, "slots": {"early"}, "rungs": [0.05, 0.10]},
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

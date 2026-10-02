"""Trader behaviour.  Git-tracked on purpose: a run is reconstructable from the repo."""

LOOP_EVERY = 30              # seconds between decision cycles

# -- entry rule ----------------------------------------------------------------
MIN_EDGE = 0.05              # default entry edge (model_p - ask - fee); arms override
MIN_PRICE, MAX_PRICE = 0.05, 0.97   # never buy lottery tickets or pennies-for-dollars
MIN_TAU_S = 120              # no entries in the last 2 minutes (1m bars lag)
MAX_TAU_H = 36               # the backtest scored the last 36h only
NEAR_TIE_BP = 3.0            # inside 30 min, skip if |ln S/ref| < 3bp: Pyth may differ
NEAR_TIE_TAU_S = 1800
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
    "live": {
        "open_usd": 100.0,                               # unsettled cost, all slots
        # per slot, so early entries cannot use up the budget before the late window
        "orders_per_day_slot": {"early": 20, "late": 20},  # possibly-filled, per ET day
        "open_usd_slot": {"early": 60.0, "late": 40.0},
        "daily_loss": 100.0,                             # realized, per ET day (was 30)
    },
    "paper": None,
}
STOP_AFTER_FILLS_LIVE = None # live only: pause after this many fills (None = off,
                             # user decision 2026-09-30: pause on request instead)

# -- data freshness ---------------------------------------------------------------
MAX_BARS_AGE_S = 15 * 60     # newest bar of any symbol; older = recorder down

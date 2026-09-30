"""Trader behaviour.  Git-tracked on purpose: a run is reconstructable from the repo."""

LOOP_EVERY = 30              # seconds between decision cycles

# -- entry rule ----------------------------------------------------------------
MIN_EDGE = 0.05              # model_p - ask - fee per share, to enter
MIN_PRICE, MAX_PRICE = 0.05, 0.97   # never buy lottery tickets or pennies-for-dollars
MIN_TAU_S = 120              # no entries in the last 2 minutes (1m bars lag)
MAX_TAU_H = 36               # the backtest scored the last 36h only
NEAR_TIE_BP = 3.0            # inside 30 min, skip if |ln S/ref| < 3bp: Pyth may differ
NEAR_TIE_TAU_S = 1800
NOFILL_COOLDOWN_S = 600      # after a miss, wait before trying the same market again

# -- market scope ---------------------------------------------------------------
MIN_SERIES_VOLUME = 500      # median USD volume of the series' last week of markets
EXCLUDE_SERIES: set[str] = set()

# -- sizing ----------------------------------------------------------------------
BUDGET_USD = 3.0             # target notional per order
MIN_SHARES = 5               # venue minimum (orderMinSize) - at 0.90 that is $4.50
MAX_ORDER_USD = 5.0          # hard cap: skip rather than exceed
FEE_RATE = 0.04              # finance taker fee: rate * p * (1 - p) per share

# -- risk limits (counted from the database, so a restart cannot reset them) ----
MAX_ORDERS_PER_DAY = 30      # filled or possibly-filled entries per ET day
MAX_OPEN_USD = 100.0         # cost of unsettled positions
MAX_DAILY_LOSS_USD = 30.0    # realized loss per ET day; hit -> no new entries today
STOP_AFTER_FILLS_LIVE = 20   # live only: pause after this many fills, for review

# -- data freshness ---------------------------------------------------------------
MAX_BARS_AGE_S = 15 * 60     # newest bar of any symbol; older = recorder down

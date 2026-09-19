# Data schema

**Units, first and foremost: every implied volatility in this repo is an annualized
DECIMAL.** `0.2053` means 20.53%. Never percent points. IV Rank and IV Percentile are
only ever printed by `ivdb ivr` (0–100) and are never stored. Strikes and prices are in
dollars (index points for SPX/NDX/RUT/VIX).

Source: CBOE delayed quotes, `https://cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json`
(indexes carry a leading underscore: `_SPX`). CBOE's per-contract `iv` is already a
decimal; its per-underlying `iv30` is in percent points and is divided by 100 on ingest.

## Conventions

- UTF-8, LF line endings, header row, one file per symbol.
- Dates are ISO `YYYY-MM-DD`. `date` is the **trading session** the snapshot describes,
  taken from the underlying's `last_trade_time`, not from when the job ran.
- Blank cell = not available. Never `0`, `NaN`, or `null` as a stand-in.
- Floats are written with at most 6 decimals, trailing zeros trimmed.
- Files are sorted by their key. Re-collecting a date replaces that date's rows
  (idempotent upsert), so re-running never duplicates.
- A symbol that fails its sanity checks on a given day gets **no row** for that day.
  Missing beats plausible-looking garbage.

## `tickers.csv`

| column | type | notes |
|---|---|---|
| symbol | str | canonical name used for file names, e.g. `SPX`, `BRK.B` |
| cboe_symbol | str | as CBOE spells it: `SPY`, `_SPX`, `BRK.B` |
| kind | enum | `equity`, `etf`, `index` |
| active | 0/1 | inactive symbols are skipped; their history is kept |
| tier | enum | `active` (full chain) or `surveillance` (summary only). Optional column; a file without it reads as all-active |

## `data/daily/{SYMBOL}.csv` — key: `date`

| column | type | unit / meaning |
|---|---|---|
| date | date | trading session |
| spot | float | price used for ATM selection: CBOE `close` if > 0, else `current_price` |
| last | float | CBOE `current_price` (may include after-hours prints) |
| close | float | CBOE `close` |
| prev_close | float | CBOE `prev_day_close` |
| volume | int | underlying share/contract volume |
| cboe_iv30 | float | CBOE's own 30-day IV index, decimal |
| iv30_atm | float | constant-maturity 30-DTE ATM IV, decimal (see below) |
| iv60_atm | float | constant-maturity 60-DTE ATM IV, decimal |
| iv90_atm | float | constant-maturity 90-DTE ATM IV, decimal |
| put25_iv_30 | float | 25-delta put IV at the expiration nearest 30 DTE, decimal |
| call25_iv_30 | float | 25-delta call IV at that same expiration, decimal |
| skew_25d_30 | float | `put25_iv_30 − call25_iv_30`, decimal (positive = puts richer) |
| front_expiry | date | nearest expiration (DTE ≥ 1) with a computable ATM IV |
| front_dte | int | calendar days to `front_expiry` |
| front_atm_iv | float | ATM IV at `front_expiry`, decimal |
| n_expirations | int | distinct expirations in the chain |
| n_contracts | int | contracts in the chain |
| total_oi | int | open interest summed over the whole chain |
| source_ts | str | CBOE payload `timestamp` (snapshot generation time, ET) |

## `data/term/{SYMBOL}.csv` — key: `(date, expiry)`

One row per stored expiration. Stored expirations = the nearest 8 with DTE ≥ 1, plus
every standard monthly (third Friday) out to 120 DTE.

| column | type | unit / meaning |
|---|---|---|
| date | date | trading session |
| expiry | date | option expiration |
| dte | int | calendar days `expiry − date` |
| atm_strike | float | strike nearest spot among usable contracts |
| atm_iv | float | decimal, see ATM rule |
| atm_n | int | contracts behind `atm_iv` (2–4); with `n_usable` this is the liquidity signal |
| put25_iv | float | IV of the put whose |delta| is nearest 0.25 (within 0.10), decimal |
| put25_strike | float | its strike |
| call25_iv | float | IV of the call whose delta is nearest 0.25 (within 0.10), decimal |
| call25_strike | float | its strike |
| oi | int | open interest summed over all contracts at this expiration |
| n_usable | int | contracts that passed the usability filter |

## `data/surveillance/{SYMBOL}.csv` — key: `date`

One row per session for tickers on the `surveillance` tier. These are fetched with a
suffix HTTP Range request that pulls only the last few KB of the chain file, where the
per-underlying summary lives, so a surveillance ticker costs about 60 bytes of storage and
one small request a day instead of a multi-megabyte download.

The point is the cold start. A ticker nobody traded last year has no IV history when it
suddenly matters, and IV Rank needs months of it. Watching a wide universe cheaply from the
start means a ticker promoted to `active` already has history banked.

| column | type | unit / meaning |
|---|---|---|
| date | date | trading session |
| spot | float | `close` if > 0, else `current_price` |
| close | float | CBOE `close` |
| prev_close | float | CBOE `prev_day_close` |
| volume | int | underlying volume |
| cboe_iv30 | float | CBOE's own 30-day IV index, decimal |

No ATM, skew or term-structure columns: those need the full chain. `cboe_iv30` is enough to
rank against, because it tracked the computed `iv30_atm` to a median of 0.7% across 146
tickers on 2026-09-08. `ivdb ivr` reads this file automatically for surveillance-only
symbols and labels the source in its output.

**Promoting a ticker**: change its `tier` to `active` in `tickers.csv`. The banked
`data/surveillance/` history stays where it is and `ivdb ivr` keeps using it until
`data/daily/` has more rows. Historical skew and term structure begin at promotion.

## `data/runs.csv` — append-only run log

| column | meaning |
|---|---|
| run_ts_utc | when the job ran |
| trade_date | session the run refers to |
| status | `ok`, `closed` (weekend/holiday, nothing written), `fail` (quality gate; nothing written) |
| attempted / succeeded | active-tier ticker counts |
| surv_attempted / surv_succeeded | surveillance-tier ticker counts |
| duration_s | wall time |
| cboe_ts | CBOE snapshot timestamp of the probe symbol |
| note | failed symbols or gate reasons |

## Algorithms

**Usable contract**: two-sided market (`bid > 0`, `ask ≥ bid > 0`) whose spread is no
wider than `max(0.10, 0.5 × mid)`, and IV finite with `0.01 < iv < 5.0`. CBOE's after-close
snapshots contain placeholder quotes such as bid 0.00 / ask 4.80 whose IVs are meaningless;
the spread rule removes them.

**ATM IV per expiration**: for calls, interpolate IV linearly to spot between the usable
strikes just below and just above it; do the same for puts; `atm_iv` is the mean of the two
sides, and at least two contracts must contribute (`atm_n`). Only strikes within
`max(3% of spot, one strike step)` of spot may be used, so a thin expiration cannot bridge
distant strikes and inherit their skew. Interpolating to spot matters for low-vol names with
coarse strikes (HYG, LQD), where the nearest OTM strike already carries visible skew.

**Reliable expiration**: `atm_n ≥ 3` and `n_usable ≥ 6`. Only reliable expirations drive the
constant-maturity curve and the `front_*` columns; illiquid weeklies on sector ETFs would
otherwise dominate the 30-day point. If no expiration is reliable, all with an ATM IV are used.

**Constant-maturity IV (30/60/90)**: find the reliable expirations bracketing the target DTE;
interpolate linearly in *total variance* (`σ²·t`, t in calendar days) and convert back:
`iv = sqrt(var / T)`. If all expirations lie on one side of the target, use the nearest one
unchanged.

**Skew**: the expiration nearest 30 DTE that has both a 25-delta put and call IV.

**IV Rank** (computed on read): `(current − min) / (max − min) × 100` over the last 252
rows of the chosen metric, at least 60 rows required. **IV Percentile**: share of prior
rows strictly below the current value, × 100.

## Known caveats

- **CBOE serves chain files it has stopped updating.** Each symbol's file refreshes on its
  own schedule, and some have not moved in months: sampled on 2026-09-08, `BK` was four
  months behind, `MMC` eight months, `DFS` sixteen, and `K` returned a `2000-01-01`
  placeholder. A stale file is not an error response, it is a well-formed chain carrying old
  prices, which is why every fetch compares the payload's own `last_trade_time` against the
  session before writing anything. Verified the same day that this is a property of the
  files themselves, not of the Range requests: tail and full fetches return identical
  `last_trade_time` values. Symbols more than a few sessions behind should be set
  `active=0`; `ivdb collect` groups stale failures by their exact date so they are easy to
  spot.
- **VIX options are priced off VIX futures, not the spot index.** `atm_*` and `skew_*`
  for `VIX` are measured against spot and so mix maturities with very different forwards;
  weekly VIX expirations in particular show much higher "ATM" IV than monthlies. Use
  `cboe_iv30` for VIX, and treat its term rows as raw material rather than a clean curve.
- Snapshots taken after the session roll (early morning) can show `close == prev_close`;
  the scheduled run at 22:17 UTC lands before that roll.

## Quality gates

Per ticker, **row excluded** for the day: fetch error, chain's last trade not on the
session date (CBOE sometimes serves a day-old file, e.g. EFA on 2026-09-04), spot ≤ 0.

Per ticker, **row kept but every ATM-derived column blank** (`iv30/60/90_atm`, 25Δ values,
skew, `front_*`): no reliable expiration within 7–60 DTE to anchor the 30-day point (all
quotes pulled after the close, typical of thin sector-ETF weeklies), or `iv30_atm` missing or
outside (0.03, 4.0). `spot`, `close`, `volume` and `cboe_iv30` are still recorded, and the
term rows are written with their `atm_n`/`n_usable` so a consumer can judge them.

Surveillance tickers are excluded for the day on a fetch error, `spot` <= 0, or a
`cboe_iv30` outside (0.03, 4.0). A summary whose own session is up to 5 calendar days
behind the one being collected is **kept and stored under its own date**: CBOE regenerates a
quiet name's file only a few times a day (measured 2026-09-10: 348 of 784 surveillance files
still carried the previous close at 00:48 UTC), so the next run fills the missing session
instead of leaving a gap. `runs.csv` reports these as `surveillance lagged (N)`. Anything
older is a dead symbol and is excluded as a stale chain.

Per run (exit 1, **nothing written**, GitHub emails the owner): fewer than 90% of active tickers
produced a row; more than 50% of rows have blank ATM columns (a quote-format change or filter
regression); any populated `iv30_atm`/`iv60_atm` outside (0.03, 4.0); a ticker's `iv30_atm`
moved more than 3× against its 20-day median while CBOE's `iv30` did not move more than
1.5× (parser regression); post-write duplicate or unsorted keys.

Fewer than 50% of surveillance tickers succeeding is logged as a quality error and recorded
in the run note, but it is **not fatal**: the active tier is written regardless, because CBOE
only serves today's snapshot and a skipped day can never be re-collected.

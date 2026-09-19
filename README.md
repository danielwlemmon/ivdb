# ivdb

A daily implied-volatility collector. GitHub Actions reads CBOE's free delayed option
quotes after the US close, computes per-ticker IV metrics, and commits them as CSV to a
**separate data repository**. The goal is to own enough IV history to compute IV Rank and
IV Percentile yourself, with spot, ATM term structure and 25-delta skew per day.

This repository holds code only. No collected data is committed here.

No API keys, no accounts, no servers. **Units:** every IV is an annualized decimal
(`0.2053` = 20.53%). See [docs/schema.md](docs/schema.md) for the data contract.

## Two tiers

| Tier | Fetched | Stored per day | Gives you |
|---|---|---|---|
| `active` | Full option chain (several MB) | `data/daily/` + `data/term/` | ATM IV, 25Δ skew, term structure |
| `surveillance` | Last few KB via an HTTP Range request | `data/surveillance/`, ~60 bytes | CBOE's own `cboe_iv30`, spot, volume |

Surveillance exists to beat the cold start: IV Rank needs months of history, so a ticker
added the week it becomes interesting is useless for months. Watching a wide universe at 60
bytes a day means the history is already there when you promote a name.

## How it runs

`.github/workflows/collect.yml`, Monday–Friday at 22:17 UTC:

1. Check out this repo (code) and the data repo (into `iv-data/`, using a token).
2. Probe `_SPX` for the session date. Weekend or holiday: log `closed`, write nothing.
3. Fetch every `active` chain, then every `surveillance` summary (concurrency 2, globally
   paced, retried with backoff; about 35 minutes).
4. Compute per-expiration ATM IV, 25Δ IVs, constant-maturity 30/60/90-day IV and skew.
5. Quality gates. An active-tier failure exits non-zero, writes nothing, and GitHub emails
   the owner. Surveillance problems are logged and never block the active tier.
6. Upsert the CSVs and push them to the **data repo**. Append `runs.csv` **here**: it has no
   market data, and the daily commit keeps GitHub from disabling the schedule after 60 idle days.

### Setup for your own copy

1. Create a private data repo containing `tickers.csv`
   (`symbol,cboe_symbol,kind,active,tier`; indexes use CBOE's underscore form:
   `SPX,_SPX,index,1,active`).
2. In this repo: variable `IV_DATA_REPO` = `owner/name` of the data repo; secret
   `IV_DATA_TOKEN` = a fine-grained personal access token limited to that one repository
   with **Contents: read and write**.
3. Change the `github.repository_owner` guard in `collect.yml` to your account.

## Local usage

```bash
uv sync
uv run ivdb collect --tickers tickers.example.csv --dry-run    # fetch + compute + gate, write nothing
uv run ivdb collect --data-dir ../iv-data/data --tickers ../iv-data/tickers.csv --runs-path runs.csv
uv run ivdb ivr --data-dir ../iv-data/data --symbols SPY,QQQ   # IV Rank / Percentile once >= 120 rows exist
uv run ivdb validate --data-dir ../iv-data/data
uv run pytest -q
```

Offline: `uv run ivdb collect --tickers tickers.example.csv --fixture-dir tests/fixtures --probe-symbol SPY --symbols SPY,MU --now 2026-09-04T18:17:00 --dry-run`.

`tickers.example.csv` shows the format. The real list lives in the data repo.

Dispatching the workflow during market hours exits with code 2 ("too early") on purpose.

## Things CBOE's endpoint does that you should know

- Above roughly 40 requests a minute the CDN answers HTTP 429. 2.5 s between request starts
  is faster overall than 1.5 s, because it never trips the limit.
- After-close snapshots carry pulled quotes (bid 0.00 / ask 4.80) on illiquid weeklies. The
  usability filter drops them.
- Some symbol files stop updating for months while still returning a well-formed chain, so
  every fetch compares the payload's own `last_trade_time` with the session before writing.
- Only today's snapshot is served. A skipped day cannot be re-collected.

## IV Rank accuracy against row count

Measured against the mature 252-row answer over ~16,800 observations of real IV history:

| rows | mean abs error | agrees on IVR > 50 |
|---|---|---|
| 60 | 14.3 | 81.8% |
| 120 | 8.8 | 87.7% |
| 180 | 4.7 | 92.1% |

`ivdb ivr` refuses to rank below 120 rows and labels anything below 180 `provisional`.

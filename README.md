# TradeQuery

Ask plain-English questions about your MT5 trade history and get a verified table and a short explanation back.

> **Status:** data layer working. A live MT5 account syncs into a per-account SQLite database, MT5 HTML reports import into the same format, and the two are cross-checked deal by deal. Metrics, detectors and the question-answering layer come next.

## The idea

Code computes every number (win rate, drawdown, swap, R-multiples, streaks, lots open). The language model only turns a question into a structured query and explains the result, and a checker confirms every number in the explanation matches the result table.

## How it runs

Two containers share one `./data` folder:

| Service | What it does |
|---|---|
| `app` | Linux Python: tests, demo, report import, cross-check and analysis of stored accounts (later: API and UI). |
| `mt5` | Debian + Wine + the MT5 terminal + **Windows** Python. Its only job is to log in with the investor password and write the account history to `./data`. |

The `MetaTrader5` Python package exists only for Windows, so the sync runs inside Wine. Everything after the sync is ordinary Linux code and never needs MT5.

Each account gets one SQLite file, `data/accounts/<login>.db`, with these tables: `accounts`, `symbols`, `deals` (the source of truth, upserted by ticket), and `cash_flows` and `trades` (rebuilt from deals on every sync or import).

## Setup (Windows + WSL2)

1. Install WSL2 with Ubuntu, and Docker (Docker Desktop with WSL integration, or docker-ce inside WSL). `docker compose version` must work in the WSL shell.
2. Keep the project in the **WSL filesystem** (for example `~/tradequery`), not under `/mnt/c`. Docker and Wine are much faster there.
3. Create the data folder yourself before the first Docker run. Otherwise Docker creates it owned by root and nothing can write to it.
   ```bash
   cd ~/tradequery
   mkdir -p data
   ```
4. Run the tests and the demo. Neither needs MT5.
   ```bash
   make test
   make demo
   ```

Every `make` target rebuilds its image first. The rebuild is cached, so it takes about a second when nothing changed, and code changes are never silently skipped.

## Connect a live account

```bash
cp .env.example .env      # fill in MT5_LOGIN, MT5_INVESTOR_PASSWORD, MT5_SERVER
make mt5-setup            # once: Wine prefix, Windows Python, MT5 terminal (10-20 min)
make inspect              # logs in, prints the account and a history summary, changes nothing
make sync                 # saves the history to data/accounts/<login>.db (later runs: only what is new)
make show                 # analyse the stored account (no MT5 needed)
docker compose run --rm app metrics   # win rate, profit factor, streaks, drawdown, ... (--symbol, --magic, --since, --until)
```

- **Use the investor (read-only) password**, never the master password. `inspect` prints "read-only (investor)" or warns if the login can trade.
- `MT5_SERVER` must match the broker's server name exactly, spaces included (for example `Broker-Live 3`).
- **Use your broker's installer.** Set `MT5_INSTALLER_URL` in `.env` to your broker's MT5 installer before `make mt5-setup`. It installs the broker's release build together with its server list. Without it the generic MetaQuotes installer is used. `.env.example` shows the format.
- After the first sync, `make sync` re-reads only from a day before the newest stored deal and updates by ticket, so nothing duplicates. To re-read everything, run `docker compose run --rm mt5 download --full`.
- Brokers keep a limited history. If it starts after the account opened, `sync` infers the opening balance (MT5 balance minus every stored deal) and says so.

## Import a report (no terminal needed)

In the MT5 terminal: Toolbox → History → right-click → *All History* → *Report* → *HTML*. Save the file into `data/`, then:

```bash
make import FILE=data/ReportHistory-<login>.html
```

The importer only accepts the report if every deal reproduces the report's own Balance column, so a misread number fails loudly. A report has no magic numbers and only partial close reasons. An import never overwrites deals already stored by a sync.

## Cross-check a report against the sync

```bash
docker compose run --rm app compare /data/ReportHistory-<login>.html
```

This compares every deal and every closed trade in the report's time span with the stored history, field by field. Both sources of the same account must agree.

## Data and secrets

- Real trade data lives in `data/` (git-ignored). Only synthetic samples are ever committed.
- Credentials live in `.env` (git-ignored) and are never printed, saved to data files, or passed to the language model. To log in, the `mt5` container writes a startup file into its own `/tmp`, readable only by its user, and deletes it as soon as the terminal is logged in.

## MT5 under Wine: what the setup works around

These are pinned or handled in `docker/mt5/`. They are worth knowing if you change versions.

| Symptom | Cause | Handled by |
|---|---|---|
| `wineboot` hangs forever | Wine asks to install Mono/Gecko in a dialog nobody can click | `WINEDLLOVERRIDES="mscoree=;mshtml="` while creating the prefix |
| "A debugger has been found running in your system" | Current MT5 builds reject Wine 10 and Wine 11.0 | WineHQ **devel 11.10** pinned in the Dockerfile |
| `import MetaTrader5` crashes (`ucrtbase.dll.crealf`) | numpy 2.x calls a function Wine lacks | `numpy<2` for the Windows install (`pyproject.toml`) |
| `initialize()` fails with `IPC timeout` (-10005) | The library deadlocks against a terminal that is not logged in yet | The terminal is started already logged in, then Python attaches |
| "No deals found" right after the first login | The terminal is still downloading history | `fetch_deals` retries an empty history for up to a minute |

To look inside the Wine container, run `make shell-mt5`. `make clean-mt5` deletes the Wine volume, after which `make mt5-setup` must run again.

## Known limits

- The sync needs the MT5 terminal, so it runs locally (Wine in Docker), not on a server.
- Brokers keep a limited deal and price history. Answers that reach back that far will say so.
- The report importer is built for hedging accounts, where a position's id is its opening order's ticket.
- Planned: balance drawdown covers closed trades only. Equity drawdown from M1 bars will be an approximation.

## Layout

```
src/tradequery/
  models.py       Deal, CashFlow, Trade
  timeutil.py     broker server time <-> UTC <-> local (daylight-saving safe)
  settings.py     settings.toml + MT5 credentials
  trades.py       deals -> closed trades -> derived fields, balance curve, balance check
  synthetic.py    fake account with planted facts (tests, demo, public repo)
  db.py           one SQLite database per account: schema, save/load, read-only access
  mt5.py          read from the MT5 terminal (runs only inside Wine)
  importers/      MT5 HTML report importer
  crosscheck.py   report vs stored history, deal by deal and trade by trade
  metrics.py      named metrics with definitions: win rate, profit factor, R, streaks, drawdown, ...
  cli.py          demo | inspect | download | import | compare | metrics | show
docker/mt5/       Wine image and its setup script
tests/            unit tests (synthetic data and a made-up report; no real accounts)
```

## License

MIT

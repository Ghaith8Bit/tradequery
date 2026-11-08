# TradeQuery

Ask plain-English questions about your MT5 trade history and get a verified table and a short explanation back.

> Status: Phase 1 (foundation), rebuilt for WSL + Docker. Not usable for questions yet.

## The idea

Code computes every number (win rate, drawdown, swap, R-multiples, streaks, lots open). The language model only turns a question into a structured query and explains the result, and a checker confirms every number in the explanation matches the result table.

## How it runs

Two containers, one shared `./data` folder:

| Service | What it does |
|---|---|
| `app` | Linux Python. Tests, demo, analysis of downloaded data (later: API and UI). |
| `mt5` | Debian + Wine + the MT5 terminal + **Windows** Python. Its only job is to log in with the investor password and write the account history to `./data`. |

The `MetaTrader5` Python package only exists for Windows, so the sync runs inside Wine. Everything after the sync is ordinary Linux code and never needs MT5.

## Setup (WSL2)

1. Windows: WSL2 with Ubuntu, and Docker (Docker Desktop with WSL integration on, or docker-ce inside WSL). `docker compose version` must work in the WSL shell.
2. **Copy the project into the WSL filesystem**, not `/mnt/c` (Docker and Wine are much faster there):
   ```bash
   cp -r /mnt/c/Users/ghait/Desktop/TradeQuery/tradequery-v2 ~/tradequery
   cd ~/tradequery
   mv Makefile.txt Makefile                                   # these two names are blocked when
   mkdir -p .github/workflows && mv ci.yml.txt .github/workflows/ci.yml   # copied from Claude; rename here
   git init
   ```
3. Run the tests (no MT5 needed) and the demo:
   ```bash
   make test
   make demo
   ```
4. Connect your account:
   ```bash
   cp .env.example .env      # fill in login, INVESTOR password, server
   make mt5-setup            # once: installs Windows Python + MT5 into a Docker volume
   make inspect              # logs in, prints account + history summary, changes nothing
   make sync                 # saves history to ./data/accounts/<login>/
   make show                 # analyse the saved account (no MT5)
   ```

If `make inspect` cannot log in, run `make shell-mt5` and check the Wine side; the exact server name (`MT5_SERVER`) is the usual culprit.

## Data and secrets

- Real trade data lives in `data/` (git-ignored). Only synthetic samples are ever committed.
- Credentials live in `.env` (git-ignored). Use the investor (read-only) password, never the master password. `inspect` warns if the login is allowed to trade.

## Layout

```
src/tradequery/
  models.py     Deal, CashFlow, Trade
  timeutil.py   broker server time <-> UTC <-> local (daylight-saving safe)
  settings.py   settings.toml + MT5 credentials
  trades.py     deals -> closed trades -> derived fields, balance curve, balance check
  synthetic.py  fake account with planted facts (tests, demo, public repo)
  store.py      save/load deals, symbols, account facts
  mt5.py        read from the MT5 terminal (runs only inside Wine)
  cli.py        demo | inspect | download | show
docker/mt5/     Wine image and its setup script
tests/
```

## License

MIT

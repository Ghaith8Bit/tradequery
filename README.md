# TradeQuery

Ask plain-English questions about your MT5 trade history and get a verified table and a short explanation back.

<!-- Screenshot: save one as docs/screenshot.png (http://localhost:8000 with the sample account) and uncomment:
![TradeQuery answering a question](docs/screenshot.png) -->

> **v1.0.0.** Live MT5 sync and report import, named metrics, a price path from one-minute bars, risk detectors with
> evidence, martingale and grid sequences, news overlap, plain-English questions (Gemini) with a number checker, a web
> page, and an 80-question eval suite. The story behind it: [docs/case-study.md](docs/case-study.md).

## The problem

MT5 shows every deal but answers nothing. "Which hour loses me the most on gold?", "How deep did my martingale baskets
go before they closed green?", "Did news cause my worst drawdown?" take spreadsheets, and spreadsheets quietly treat a
withdrawal as a loss or the broker's server clock as UTC. A language model can read the question, but it cannot be
trusted with the arithmetic.

## How it works

**Code computes every number; the model only translates.**

```
question --model, call 1--> QuerySpec (typed JSON, never SQL)
         --> whitelist check --> parameterized SQL on a read-only per-account SQLite database --> result table
         --model, call 2, sees only the table--> short answer --> every number checked against the table --> shown
```

- Every metric (win rate, profit factor, R, streaks, drawdown, lots open, swap, ...) comes from tested code with a written definition.
- Each answer carries an **interpretation line** (account, filters, time range, timezone) and the spec and SQL behind it.
- An answer with a number that is not in the table is retried once, then shown as the table alone.
- Two containers share `./data`: `app` (Linux Python: analysis, questions, the web page) and `mt5` (Debian, Wine, the MT5 terminal and **Windows** Python, only for the sync, since the `MetaTrader5` package exists only for Windows). One SQLite file per account: `data/accounts/<login>.db`.

## Quick start (no MT5 needed)

Windows with WSL2 (Ubuntu) and Docker (`docker compose version` must work in the WSL shell). Keep the clone in the WSL
filesystem (for example `~/tradequery`), not under `/mnt/c`: Docker and Wine are much faster there.

```bash
make test      # 285 unit tests, inside Docker
make sample    # store a synthetic sample account (48 made-up trades with planted patterns)
make web       # http://localhost:8000: pick the account, click an example question
```

To ask your own questions, add a free Gemini API key (Google AI Studio) to `.env` as `GEMINI_API_KEY=...`, then type in
the page or run `make ask Q="What is my win rate?"`. Without a key the example questions still work.

## Eval results (summary)

80 questions with hand-written gold answers, 10 of them adversarial, scored on the sample account. Gemini 3.5
Flash-Lite, free tier: **result match 91.4% on the first run (100% after prompt fixes), adversarial refusals 100%,
number check 100%, 3.9 s and $0 per question.** Details and caveats: [Eval results](#eval-results).

## Running on your own account

Every `make` target rebuilds its image first (cached, about a second when nothing changed), so code changes are never
silently skipped. `make` also creates `data/` as you, before Docker could create it owned by root.

### Connect a live account

```bash
cp .env.example .env      # fill in MT5_LOGIN, MT5_INVESTOR_PASSWORD, MT5_SERVER
make mt5-setup            # once: Wine prefix, Windows Python, MT5 terminal (10-20 min)
make inspect              # logs in, prints the account and a history summary, changes nothing
make sync                 # saves the history to data/accounts/<login>.db (later runs: only what is new)
make show                 # analyse the stored account (no MT5 needed)
docker compose run --rm app metrics   # win rate, profit factor, streaks, drawdown, ... (--symbol, --magic, --since, --until)
docker compose run --rm app tags      # detector tags with evidence (revenge, overtrading, no stop loss, oversized, martingale, grid)
docker compose run --rm app sequences # martingale and grid sequences: depth, lot multiplier, result
```

- **Use the investor (read-only) password**, never the master password. `inspect` prints "read-only (investor)" or warns if the login can trade.
- `MT5_SERVER` must match the broker's server name exactly, spaces included (for example `Broker-Live 3`).
- **Use your broker's installer.** Set `MT5_INSTALLER_URL` in `.env` to your broker's MT5 installer before `make mt5-setup`. It installs the broker's release build together with its server list. Without it the generic MetaQuotes installer is used. `.env.example` shows the format.
- After the first sync, `make sync` re-reads only from a day before the newest stored deal and updates by ticket, so nothing duplicates. To re-read everything, run `docker compose run --rm mt5 download --full`.
- Brokers keep a limited history. If it starts after the account opened, `sync` infers the opening balance (MT5 balance minus every stored deal) and says so.

### Import a report (no terminal needed)

In the MT5 terminal: Toolbox → History → right-click → *All History* → *Report* → *HTML*. Save the file into `data/`, then:

```bash
make import FILE=data/ReportHistory-<login>.html
```

The importer only accepts the report if every deal reproduces the report's own Balance column, so a misread number fails loudly. A report has no magic numbers and only partial close reasons. An import never overwrites deals already stored by a sync.

### Cross-check a report against the sync

```bash
docker compose run --rm app compare /data/ReportHistory-<login>.html
```

This compares every deal and every closed trade in the report's time span with the stored history, field by field. Both sources of the same account must agree.

### News overlap

```bash
make news FROM=2026.08.01 TO=2026.10.31   # export the MT5 economic calendar and join it to the trades
docker compose run --rm app news          # results inside vs outside news windows, drawdown attribution
```

`make news` compiles `mql5/ExportCalendar.mq5` in the Wine terminal, runs it as a start-up script, and imports the file it writes (`data/news/calendar.csv`). You can also run the script yourself in a Windows MT5 terminal and import its file with `import-calendar <file>`.

- **Time alignment is checked, not assumed.** A broker's calendar clock can differ from its trade server clock. The importer calibrates it on US Nonfarm Payrolls, which is always released at 08:30 New York time, and reports the clock it found.
- A trade overlaps an event when it is open from 15 minutes before to 60 minutes after a high-importance event for its symbol's currencies (settings.toml `[news]`).
- Results are worded as "overlapped with", never "caused by". Calendars list scheduled events only; surprise headlines are not covered.

## The web page

```bash
make web          # http://localhost:8000 (reachable from this computer only); make web-stop to stop it
```

A chat page that also works on a phone:

- **Start screen:** four key number cards (net result with this month against last, win rate per trade and per basket, account drawdown and basket drawdown with a bar), a daily result calendar (tap a day to see its trades), a "Risk to watch" box, a balance chart, net result by weekday, and suggested questions grouped by whole account, baskets and single trades. Suggested questions and calendar days run from hand-written specs with no model call.
- **Answers:** typed questions are answered in chat bubbles, with a badge saying whether every number was checked. A line of labels on top says what was counted ("Baskets · 1 basket found", "Single trades", "Account drawdown (equity)"). Switch buttons below re-ask at another level: the trades in a basket or drawdown, the balance drawdown instead of equity, or the same question per basket or for single trades. A single-row answer leads with one large number; for summaries the table sits behind "Show data", while lists stay open. "Open chart" shows a single trade or basket on a price chart. Answers get an automatic chart when their shape suits one, plus a "How this was found" panel with the spec and SQL. Chats are saved in the browser, per account.
- **Upload report:** drop an MT5 history report (.html) anywhere on the page to add or extend an account. This works the same as `make import`: stored deals are never overwritten.
- **Sync now:** downloads new trades from MT5 without the terminal. `make web` also starts a small **sync worker** in the Wine image when MT5 is set up. The page asks it to sync through files in `data/sync/`; your credentials stay in `.env` and never pass through the page. To also sync on a timer, set `SYNC_EVERY_MINUTES` in `.env`. The sidebar shows when the account was last updated.
- **Trade charts:** click any trade or basket number in an answer (or a basket tile) to see it on a candle chart. The chart is built from the stored one-minute bars and marks every entry, exit, stop loss and take profit. Hours without prices, such as weekends, are skipped and marked.

The API behind it (FastAPI, `/api/docs`) opens one account database per request (read-only, except for uploads) and rate limits model calls.

## Asking in plain English

```bash
make ask Q="Which hour of the day loses me the most money on gold?"
make ask        # interactive: follow-ups like "only September" refine the last question
docker compose run --rm app llm-models   # the Gemini models your key can use
```

Needs `GEMINI_API_KEY` in `.env` (Google AI Studio, free tier available). Two model calls per question:

1. **Question -> QuerySpec.** The model gets the schema, the plan's example questions, the account's date range, symbols and magic numbers, and the previous spec for follow-ups. An invalid spec goes back once with the validation error.
2. **Result -> a short answer.** The model sees only the question, the interpretation line and the result table. `verify.py` checks every number in the answer against the table (allowing rounding, percentages and signs); one retry on a mismatch, then the table is shown without prose.

Every answer prints the request and token count, so free-tier usage is visible. Trade comments and credentials never reach the model.

## Eval results

80 questions with hand-written gold answers (`evals/questions.jsonl`): 70 clear questions across basic filters, group-by and ranking, named metrics, detector tags, sequences, news, drawdown, exposure, cash flows and magic numbers, plus 10 adversarial ones (an instruction in the question, SQL text, "delete my trades", credentials, another account, a vague term, a prediction, an unknown metric, trade comments). They run on the synthetic account with planted calendar events, so they are reproducible and safe to publish. Results are compared, not spec text: any spec that returns the gold result passes.

```bash
make eval                     # about 160 model requests; paced for the free tier
make eval ARGS="--limit 10"   # a quick check
```

| Gemini 3.5 Flash-Lite (free tier) | Run 1 | Run 2 |
|---|---|---|
| Valid spec | 98.8% | 100% |
| Result match, clear questions (target 90%) | 91.4% | 100% |
| Refusals, adversarial (target 100%) | 100% | 100% |
| Number check passed | 100% | 100% |
| Requests per question | 1.9 | 1.9 |
| Seconds per question | 3.9 | 3.9 |
| Cost | $0 | $0 |

Run 1 failures, and what changed before run 2:
- A session value written in its own words ("London/New York"): the account context now lists the real `session` and `close_reason` values.
- "How long did my worst drawdown last" read as the longest drawdown: the prompt now says "worst" means the deepest.
- Lots open at a moment asked with a time range, and "average lots open" as a plain average: the prompt now points to `as_of` and to the named metric `avg_lots_open`.
- A Google 503 ("high demand"): the provider now retries once on server errors.
- A correct answer (a buys-versus-sells comparison with its own labels) failed on label text: that question now ignores label text, like the other comparisons. This changed the grading, not the model.

**Caveat:** the run 2 changes were made after seeing run 1's failures on these same questions, so run 2 partly measures tuning to this set. Run 1 is the fairer estimate for new questions; a held-out question set is the next step. The plan's two-model comparison is not done yet: the free tier allows only about 20 requests a day on larger Gemini models. Reports: `evals/results/`.

## Asking questions (QuerySpec)

The language model will never write SQL. It fills in a **QuerySpec**: a typed question (entity, filters, time range, group-by, aggregations, named metrics, comparisons, order, limit) that is checked against a whitelist and compiled to parameterized SQL. You can also write specs yourself:

```bash
docker compose run --rm app query --examples all          # the build plan's example questions
docker compose run --rm app query '{"entity": "trades", "filters": [{"field": "hold_minutes", "op": "gt", "value": 240}]}' --sql
```

- Entities: trades, sequences, streaks, exposure, cash_flows, drawdown_episodes, news_events (`spec.py` lists every field, operator and group-by).
- Named metrics (win rate, profit factor, R, streaks, ...) are always computed by the tested metric code, never by SQL arithmetic. Comparisons come with sample sizes and a 95% range.
- Safety: unknown entities, fields, operators or metrics are rejected with a reason; values are always bound parameters; the database is opened read-only; at most 500 rows; a 5-second time limit. Trade comments are not queryable.
- Every answer starts with an interpretation line (account, filters, time range, timezone), so a misread question is visible.

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
- Balance drawdown covers closed trades only. Equity drawdown, MAE and MFE come from one-minute bars and are approximate: a bar holds the minute's high and low but not their order. A conservative "worst case within the minute" bound is shown next to equity drawdown.
- Drawdown percentages are of the money at stake: the balance or equity at the peak plus deposits made before the trough.
- A plain "drawdown" question means equity drawdown (open positions counted) when the account has price bars, and balance drawdown otherwise. Ask for "balance drawdown" to get closed trades only. After an upgrade, `docker compose run --rm app rebuild` refreshes the stored drawdowns and trade links.
- Three drawdowns, always named: **account drawdown (balance)** (closed trades), **account drawdown (equity)** (open positions counted) and **basket drawdown**. A basket is a martingale or grid series (stored as a sequence). Its drawdown is how far its running result (closed legs plus open legs, at each minute's close) went below zero before it closed, in money and as a share of account equity when the basket opened. It needs one-minute bars for every leg.
- `make sync` also stores one-minute bars for the windows in which positions were open (only new windows on later runs; `--no-bars` skips them).

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
  exposure.py     lots open over time: peak, average, at a given moment
  detectors/      one rule per file, each tag with evidence; thresholds in settings.toml [detectors];
                  sequences.py finds martingale, grid and martingale-grid series
  pricepath.py    from one-minute bars: MAE/MFE, minutes in profit/loss, equity curve and equity drawdown
  news.py         calendar import with clock calibration, trade/event overlap, drawdown attribution
  facts.py        derived layers stored for querying (per-trade facts, streaks, drawdowns, exposure, equity)
  spec.py         QuerySpec models and the whitelist
  compiler.py     spec -> parameterized SQL (SQLAlchemy Core)
  execute.py      runs a spec read-only; named metrics via metrics.py; interpretation line
  examples.py     the plan's example questions as specs (tests, and worked examples for the LLM)
  llm.py          provider wrapper (Gemini; a scripted fake for tests)
  ask.py          question -> spec -> result -> checked answer, with follow-ups
  verify.py       number checker for the written answer
  api.py          the web API (FastAPI): accounts, ask with follow-up sessions, presets, specs
  web/index.html  the page: one file, no external libraries
  evaluate.py     the eval suite: eval account, matching, scoring, reports
  cli.py          every command (tradequery --help)
mql5/             ExportCalendar.mq5: exports the MT5 economic calendar
evals/            questions.jsonl (80 questions with gold specs) and results/
docs/             case-study.md, demo-script.md
docker/mt5/       Wine image and its setup script
tests/            unit tests (synthetic data and a made-up report; no real accounts)
```

## License

MIT

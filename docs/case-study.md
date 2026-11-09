# Case study: TradeQuery

*Draft for the portfolio and LinkedIn. Real account figures are deliberately left out; examples use the synthetic account.*

## The problem

MT5 shows a trader every deal, but not answers. "Which hour loses me the most on gold?", "How deep did my martingale baskets go before they closed green?", "Did news cause my worst drawdown?" take spreadsheets, and a spreadsheet will quietly treat a withdrawal as a loss or a broker's server clock as UTC. Language models can read a question, but they cannot be trusted with the arithmetic.

TradeQuery answers plain-English questions about an MT5 account with a table and a short explanation in which **every number is computed by tested code and checked before it is shown**.

## Design in one line

Code computes; the model only translates. The model turns a question into a typed QuerySpec (never SQL), code validates it against a whitelist, compiles it to parameterized SQL over a read-only per-account database, and the model then explains the result table, after which a checker confirms every number in the explanation is in the table.

## Problems solved

**Time is not what it looks like.** MT5 timestamps are broker server time encoded as UTC epoch seconds. Everything goes through one clock module with daylight-saving tests. The economic calendar turned out to run on a different clock from the trade server on a live broker (UTC+2 against UTC+3 in summer); assuming they matched would have shifted every news event by an hour without any error. The importer now calibrates the calendar on US Nonfarm Payrolls, which is always released at 08:30 New York time, and states the clock it found.

**Balance drawdown hides risk.** Closed-trade drawdown only sees losses once positions close. One-minute bars give an equity curve (balance plus floating P&L), with a conservative "every position at its worst in the same minute" bound. Deposits and withdrawals are removed from both, and percentages are of the money at stake (the balance at the peak plus deposits before the trough), so a refilled account never shows a fall above 100%.

**Martingales that win are the dangerous ones.** The sequence detector treats a position as losing when the next one opens if it closed at a loss *or* is still open at a worse price, which catches both one-at-a-time martingales and EAs that keep adding to a losing basket. Each sequence records its depth and lot multiplier, so a basket that reached 128x and closed slightly green is visible as the risk it was.

**Getting MT5 to talk.** The MetaTrader5 Python package exists only for Windows, so sync runs in Wine inside Docker. On the way: a hidden Wine dialog that froze set-up forever, current MT5 builds refusing to run under older Wine ("a debugger has been found"), numpy 2.x crashing under Wine, and the Python library deadlocking against a terminal that was not logged in yet. Each is handled in the image and documented.

**Every number is checked.** The explanation step sees only the result table. A checker extracts every number, date and time from the text and matches it against the table, allowing rounding, percentages and signs; one retry, then the table is shown without prose. The first live run caught a case the checker itself missed (a percentage matched a row count), which led to a stricter rule: percentages only match shares.

**Injection-safe querying.** The model fills a typed spec; unknown entities, fields, operators and metrics are rejected with a reason; values are always bound parameters; the database is opened read-only with a row cap and a time limit; trade comments, which are free text from the account, are never queryable and never reach the model.

**Honest reconciliation.** A report import must reproduce the report's own running balance on every deal, and a report and the live sync of the same account must give the same deals and trades field by field (they did, for every deal of a real account).

## Eval results

80 questions with hand-written gold answers, 10 of them adversarial, on a synthetic account with planted events; results are compared, not spec text. On Gemini 3.5 Flash-Lite (free tier, $0):

| | Run 1 | Run 2 |
|---|---|---|
| Result match, clear questions (target 90%) | 91.4% | 100% |
| Refusals, adversarial (target 100%) | 100% | 100% |
| Number check passed | 100% | 100% |
| Seconds per question | 3.9 | 3.9 |

Run 2 followed prompt fixes made after seeing run 1's failures on the same questions, so run 1 is the fairer estimate for new questions.

## Stack

Python end to end: pandas-free metrics in plain Python, SQLite per account, SQLAlchemy Core for the compiler, Pydantic for the spec, FastAPI and a single HTML page, the Gemini API behind a one-file provider wrapper, Wine and Docker Compose for MT5, pytest (285 tests) and GitHub Actions.

## What I would build next

- A held-out eval set and a second model in the comparison.
- Headline (unscheduled) news, live monitoring and alerts, and a discipline score.
- A hosted version that takes report uploads only, never broker passwords.

"""Read the HTML "Trade History Report" that the MT5 terminal exports.

In the terminal: Toolbox > History > right-click > Report > HTML. The file is
UTF-16 HTML with these parts, all in one table:

  header     Name, Account ("<login> (<currency>, <server>, <real|demo>, <Hedge|Netting>)"),
             Company, Date
  Positions  one row per closed position: used to find each exit deal's position
  Orders     one row per order: gives the stop loss and take profit at entry
  Deals      one row per deal, with the running balance after it
  Results    summary figures

Labels are read in English, so export with the terminal language set to English.

What a report cannot give, compared with the live sync: magic numbers (stored
as 0), the deal reason (inferred from MT5's "[sl ...]", "[tp ...]" and
"[so ...]" comments, else empty), and anything before the report's start date.
Built for hedging accounts, where a position's id is its opening order's ticket.
The account holder's name is read past and never stored.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from ..models import NON_BALANCE_FLOW_TYPES, TRADE_TYPES, Deal
from ..timeutil import ServerClock

SECTIONS = ("Positions", "Orders", "Deals", "Results")

_DIRECTIONS = {"in": "in", "out": "out", "in/out": "inout", "out by": "out_by"}
_MARGIN_MODES = {"hedge": "retail_hedging", "netting": "retail_netting", "exchange": "exchange"}
_REASON_COMMENT = re.compile(r"^\[(sl|tp|so)\b", re.IGNORECASE)
_ACCOUNT_LINE = re.compile(r"^(\d+)\s*\((.*)\)$")
_BALANCE_TOLERANCE = 0.005


class ReportError(ValueError):
    """The file is not an MT5 HTML history report this importer understands."""


@dataclass(frozen=True)
class _Cell:
    text: str
    hidden: bool


class _TableRows(HTMLParser):
    """Collect every <tr> as a list of cells (text, and whether the cell is hidden)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self._row: list[_Cell] | None = None
        self._text: list[str] | None = None
        self._hidden = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._text = []
            self._hidden = "hidden" in (dict(attrs).get("class") or "").split()

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._text is not None and self._row is not None:
            text = " ".join("".join(self._text).replace("\xa0", " ").split())
            self._row.append(_Cell(text, self._hidden))
            self._text = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._text is not None:
            self._text.append(data)


@dataclass
class Report:
    """A parsed report. ``deals`` are normalized like every other source."""

    login: int
    currency: str
    server: str
    trade_mode: str  # "real", "demo", ...
    margin_mode: str  # "retail_hedging", "retail_netting", ...
    company: str
    generated_at: str  # the report's "Date:" line, server time, as printed
    deals: list[Deal]
    opening_balance: float  # balance before the first deal in the report
    closing_balance: float  # balance after the last deal, as printed
    unmatched_exits: int = 0  # exit deals whose position is not in the report
    positions: list[dict] = field(default_factory=list)  # the Positions section, for cross-checks
    summary: dict[str, str] = field(default_factory=dict)  # Results section, label -> text

    def account(self) -> dict:
        """Account facts in the shape db.save_account expects (no name)."""
        return {
            "login": self.login,
            "company": self.company,
            "server": self.server,
            "currency": self.currency,
            "trade_mode": self.trade_mode,
            "margin_mode": self.margin_mode,
            "balance": self.closing_balance,
        }


# ---------------------------------------------------------------- small parsers


def parse_number(text: str) -> float:
    """'10 062.67' -> 10062.67; '' -> 0.0. MT5 groups thousands with spaces."""
    cleaned = text.replace(" ", "")
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        raise ReportError(f"not a number: {text!r}") from None


def parse_server_time(text: str) -> datetime:
    """'2026.09.01 01:03:00' (broker server time) -> naive datetime."""
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ReportError(f"not a report time: {text!r}")


def reason_from_comment(comment: str) -> str:
    """MT5 marks closes by stop loss, take profit and stop-out in the comment."""
    match = _REASON_COMMENT.match(comment)
    return match.group(1).lower() if match else ""


def read_report_text(path: Path | str) -> str:
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-16")


# ---------------------------------------------------------------- sections


def _visible(row: list[_Cell]) -> list[str]:
    return [c.text for c in row if not c.hidden]


def _section_title(row: list[_Cell]) -> str | None:
    if len(row) == 1 and row[0].text in SECTIONS:
        return row[0].text
    return None


def _split_sections(rows: list[list[_Cell]]) -> tuple[list[list[_Cell]], dict[str, list[list[_Cell]]]]:
    header: list[list[_Cell]] = []
    sections: dict[str, list[list[_Cell]]] = {}
    current = None
    for row in rows:
        title = _section_title(row)
        if title:
            current = title
            sections[current] = []
        elif current is None:
            header.append(row)
        else:
            sections[current].append(row)
    for needed in ("Orders", "Deals"):
        if needed not in sections:
            raise ReportError(
                f"no {needed!r} section found. Export the report as HTML from the History tab, "
                "with the terminal language set to English."
            )
    return header, sections


def _records(section: list[list[_Cell]]) -> list[dict[str, str]]:
    """Rows under the section's column header, as {column: text}. A repeated name gets _2."""
    rows = [r for r in section if any(c.text for c in r)]
    if not rows:
        return []
    names: list[str] = []
    for name in _visible(rows[0]):
        names.append(name if name not in names else f"{name}_2")
    records = []
    for row in rows[1:]:
        values = _visible(row)
        if len(values) == len(names):  # skips the totals line under a table
            records.append(dict(zip(names, values)))
    return records


def _header_fields(header: list[list[_Cell]]) -> dict[str, str]:
    out = {}
    for row in header:
        texts = [c.text for c in row]
        if len(texts) == 2 and texts[0].endswith(":"):
            out[texts[0][:-1]] = texts[1]
    return out


def _summary(section: list[list[_Cell]]) -> dict[str, str]:
    out = {}
    for row in section:
        texts = [c.text for c in row]
        for label, value in zip(texts, texts[1:]):
            if label.endswith(":") and value and not value.endswith(":"):
                out[label[:-1]] = value
    return out


# ---------------------------------------------------------------- the report


def parse_report(path: Path | str, clock: ServerClock) -> Report:
    parser = _TableRows()
    parser.feed(read_report_text(path))
    header_rows, sections = _split_sections(parser.rows)
    header = _header_fields(header_rows)

    match = _ACCOUNT_LINE.match(header.get("Account", ""))
    if not match:
        raise ReportError(f"cannot read the Account line: {header.get('Account')!r}")
    login = int(match.group(1))
    parts = [p.strip() for p in match.group(2).split(",")]
    if len(parts) != 4:
        raise ReportError(f"unexpected Account details: {match.group(2)!r}")
    currency, server, trade_mode, margin = parts

    stops = {}
    for order in _records(sections["Orders"]):
        stops[int(order["Order"])] = (parse_number(order.get("S / L", "")), parse_number(order.get("T / P", "")))

    deals: list[Deal] = []
    order_ids: list[int] = []
    printed_balances: list[float] = []
    for row in _records(sections["Deals"]):
        deal, order_id = _deal_from_row(row, clock, stops)
        deals.append(deal)
        order_ids.append(order_id)
        printed_balances.append(parse_number(row["Balance"]))
    if not deals:
        raise ReportError("the Deals section is empty")

    first = deals[0]
    opening = round(printed_balances[0] - (first.profit + first.commission + first.swap), 2)
    _check_running_balance(deals, printed_balances, opening)

    positions = _records(sections.get("Positions", []))
    deals, unmatched = _assign_positions(deals, order_ids, positions, clock)

    return Report(
        login=login,
        currency=currency,
        server=server,
        trade_mode=trade_mode.lower(),
        margin_mode=_MARGIN_MODES.get(margin.lower(), margin.lower()),
        company=header.get("Company", ""),
        generated_at=header.get("Date", ""),
        deals=sorted(deals, key=lambda d: (d.time_utc, d.ticket)),
        opening_balance=opening,
        closing_balance=printed_balances[-1],
        unmatched_exits=unmatched,
        positions=positions,
        summary=_summary(sections.get("Results", [])),
    )


def _deal_from_row(row: dict[str, str], clock: ServerClock, stops: dict) -> tuple[Deal, int]:
    kind = row["Type"].lower()
    commission = parse_number(row.get("Commission", "")) + parse_number(row.get("Fee", ""))
    swap = parse_number(row.get("Swap", ""))
    profit = parse_number(row.get("Profit", ""))
    time_utc = clock.to_utc(parse_server_time(row["Time"]))
    comment = row.get("Comment", "")
    order_id = int(row["Order"]) if row.get("Order") else 0

    if kind not in TRADE_TYPES:  # balance operation: signed amount in profit, like the sync
        deal = Deal(
            ticket=int(row["Deal"]), position_id=0, time_utc=time_utc, type=kind, entry="",
            symbol=row.get("Symbol", ""), volume=0.0, price=0.0, commission=0.0, swap=0.0,
            profit=round(profit + commission + swap, 2), comment=comment,
        )
        return deal, order_id

    direction = row["Direction"].lower()
    if direction not in _DIRECTIONS:
        raise ReportError(f"deal {row['Deal']}: unknown direction {row['Direction']!r}")
    entry = _DIRECTIONS[direction]
    sl, tp = stops.get(order_id, (0.0, 0.0)) if entry == "in" else (0.0, 0.0)
    deal = Deal(
        ticket=int(row["Deal"]), position_id=0, time_utc=time_utc, type=kind, entry=entry,
        symbol=row["Symbol"], volume=parse_number(row["Volume"]), price=parse_number(row["Price"]),
        commission=commission, swap=swap, profit=profit, reason=reason_from_comment(comment),
        comment=comment, sl=sl, tp=tp,
    )
    return deal, order_id


def _assign_positions(
    deals: list[Deal], order_ids: list[int], positions: list[dict[str, str]], clock: ServerClock
) -> tuple[list[Deal], int]:
    """Give each trade deal its position id. Returns the deals and the count of unmatched exits.

    The Deals section does not print the position. On a hedging account a
    position's id is the ticket of the order that opened it, so an entry deal
    takes its own order number. An exit deal is matched to a row of the
    Positions section by symbol, close time, close price, volume and profit.
    Exits whose position opened before the report starts (or that close a
    position in several parts) stay at 0 and are left out of closed trades.
    """
    candidates: dict[tuple, list[int]] = defaultdict(list)
    for p in positions:
        key = (
            p["Symbol"],
            clock.to_utc(parse_server_time(p["Time_2"])),
            round(parse_number(p["Price_2"]), 8),
            round(parse_number(p["Volume"]), 8),
            round(parse_number(p["Profit"]), 2),
        )
        candidates[key].append(int(p["Position"]))

    out, unmatched = [], 0
    for deal, order_id in zip(deals, order_ids):
        if deal.type not in TRADE_TYPES:
            out.append(deal)
        elif deal.entry == "in":
            out.append(replace(deal, position_id=order_id))
        else:
            key = (deal.symbol, deal.time_utc, round(deal.price, 8), round(deal.volume, 8), round(deal.profit, 2))
            matches = candidates.get(key)
            if matches:
                out.append(replace(deal, position_id=matches.pop(0)))
            else:
                unmatched += 1
                out.append(deal)
    return out, unmatched


def _check_running_balance(deals: list[Deal], printed: list[float], opening: float) -> None:
    """Every deal must reproduce the Balance column: proof the numbers were read right."""
    balance = opening
    for deal, shown in zip(deals, printed):
        if deal.type not in NON_BALANCE_FLOW_TYPES:
            balance = round(balance + deal.profit + deal.commission + deal.swap, 2)
        if abs(balance - shown) > _BALANCE_TOLERANCE:
            raise ReportError(
                f"running balance mismatch at deal {deal.ticket}: computed {balance:.2f}, "
                f"report shows {shown:.2f}"
            )

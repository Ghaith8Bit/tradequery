"""Number checker: every number in the written answer must come from the result.

Allowed sources: the result table's cells (numbers, and the dates and times
inside timestamp cells), the number of rows, and the numbers in the question
and in the spec's filters. A number matches when it equals an allowed value
after rounding to the number of decimals it was written with, also as a
percentage of a 0-1 share and ignoring its sign ("lost 829.90" for -829.90).

This catches invented numbers, not a misread question; the interpretation line
shown with every answer is the guard for that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}

_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?\b")
_MONTH_DAY = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b"
                        r"|\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
                        re.IGNORECASE)
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b")
_NUMBER = re.compile(r"(?<![\w.])[-−+]?\$?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(%?)")


@dataclass
class Check:
    ok: bool
    unsupported: list[str]  # the numbers in the text that matched nothing
    checked: int  # how many numbers were checked


def _allowed(rows, extra_numbers, extra_text) -> tuple[list[float], set[str], set[tuple[int, int]], set[tuple[int, int]]]:
    numbers: list[float] = [float(n) for n in extra_numbers]
    dates: set[str] = set()
    month_days: set[tuple[int, int]] = set()
    times: set[tuple[int, int]] = set()

    def take_text(text: str) -> None:
        for m in _DATE.finditer(text):
            dates.add(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
            month_days.add((int(m.group(2)), int(m.group(3))))
            if m.group(4):
                times.add((int(m.group(4)), int(m.group(5))))
        for m in _NUMBER.finditer(_DATE.sub(" ", text)):
            numbers.append(float(m.group(1).replace(",", "") + (f".{m.group(2)}" if m.group(2) else "")))

    shares: list[float] = []  # decimal cells: the only values a percentage may stand for
    for row in rows:
        for cell in row:
            if isinstance(cell, bool):
                numbers.append(float(cell))
            elif isinstance(cell, float):
                numbers.append(cell)
                shares.append(cell)
            elif isinstance(cell, int):
                numbers.append(float(cell))
            elif isinstance(cell, str):
                take_text(cell)
    for text in extra_text:
        take_text(text)
    return numbers, shares, dates, month_days, times


def _matches(value: float, decimals: int, percent: bool, allowed: list[float], shares: list[float]) -> bool:
    tolerance = 0.5 * 10 ** -decimals + 1e-9  # rounding to the decimals written
    if percent:
        # 62.5% stands for a 0.625 share in the table, never for a count such as the number of rows
        return any(abs(abs(a) - value / 100) <= tolerance / 100 for a in shares)
    return any(abs(abs(a) - value) <= tolerance for a in allowed)


def check_numbers(text: str, rows: list[list], *, extra_numbers=(), extra_text=()) -> Check:
    """Is every number in ``text`` supported by the result rows (or the question/spec)?"""
    numbers, shares, dates, month_days, times = _allowed(rows, list(extra_numbers) + [len(rows)], list(extra_text))
    unsupported, checked = [], 0
    remaining = text

    def blank(match) -> str:
        return " " * (match.end() - match.start())

    for m in list(_DATE.finditer(remaining)):
        checked += 1
        if f"{m.group(1)}-{m.group(2)}-{m.group(3)}" not in dates:
            unsupported.append(m.group(0))
        elif m.group(4) and (int(m.group(4)), int(m.group(5))) not in times:
            unsupported.append(m.group(0))
    remaining = _DATE.sub(blank, remaining)
    for m in list(_MONTH_DAY.finditer(remaining)):
        checked += 1
        month = MONTHS[(m.group(1) or m.group(4)).lower()[:3]]
        day = int(m.group(2) or m.group(3))
        if (month, day) not in month_days:
            unsupported.append(m.group(0))
    remaining = _MONTH_DAY.sub(blank, remaining)
    for m in list(_TIME.finditer(remaining)):
        checked += 1
        if (int(m.group(1)), int(m.group(2))) not in times:
            unsupported.append(m.group(0))
    remaining = _TIME.sub(blank, remaining)
    years = {int(d[:4]) for d in dates}
    for m in _NUMBER.finditer(remaining):
        whole, frac, percent = m.group(1), m.group(2), m.group(3) == "%"
        value = float(whole.replace(",", "") + (f".{frac}" if frac else ""))
        if not frac and not percent and value in years:
            continue  # a year that appears in the result's dates
        checked += 1
        if not _matches(value, len(frac) if frac else 0, percent, numbers, shares):
            unsupported.append(m.group(0).strip())
    return Check(not unsupported, unsupported, checked)

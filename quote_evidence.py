"""Price provenance observations only; no I/O, freshness gate, or fallback policy.

Only the same selected response/item may supply both price and time evidence.
REST Upbit UTC trade_date/trade_time and ExchangeRate-API Unix seconds have
documented meanings. yfinance 1.7.0 daily history indexes label exchange dates,
not the instant at which their closing price traded. Unverified contracts remain
unknown; HTTP Date, retrieval time, and separate metadata calls are not substitutes.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import re
from collections.abc import Mapping


SOURCES = frozenset({"upbit", "naver_fx", "yahoo_fx_fast_info", "er_api",
                     "naver_krx_api", "naver_krx_html", "swissquote",
                     "yahoo_gold_fast_info", "yahoo_gold_history"})
TIME_ISSUES = frozenset({"source_time_missing", "source_time_invalid", "source_time_naive",
                         "source_time_future", "source_date_future", "unverified_time_contract",
                         "response_evidence_mismatch"})


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware_datetime_required")
    return value.astimezone(timezone.utc)


def _price(value):
    if isinstance(value, bool):
        raise ValueError("quote_value_invalid")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("quote_value_invalid") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError("quote_value_invalid")
    return number


@dataclass(frozen=True)
class Quote:
    value: float
    source: str
    fetched_at: datetime
    source_time: datetime | None = None
    source_date: date | None = None
    time_precision: str = "unknown"
    time_resolution: str | None = None
    time_issue: str | None = "source_time_missing"

    def __post_init__(self):
        object.__setattr__(self, "value", _price(self.value))
        object.__setattr__(self, "fetched_at", _aware(self.fetched_at))
        if self.source not in SOURCES or self.time_issue not in TIME_ISSUES | {None}:
            raise ValueError("quote_contract_invalid")
        if self.time_precision == "instant":
            if self.source_date is not None or self.time_resolution != "second":
                raise ValueError("quote_contract_invalid")
            object.__setattr__(self, "source_time", _aware(self.source_time))
        elif self.time_precision == "date":
            if type(self.source_date) is not date or self.source_time is not None or self.time_resolution != "day":
                raise ValueError("quote_contract_invalid")
        elif self.time_precision != "unknown" or any(v is not None for v in (
                self.source_time, self.source_date, self.time_resolution)):
            raise ValueError("quote_contract_invalid")

    def __float__(self):
        return self.value

    def to_dict(self):
        return {"value": self.value, "source": self.source, "fetched_at": self.fetched_at.isoformat(),
                "source_time": self.source_time.isoformat() if self.source_time else None,
                "source_date": self.source_date.isoformat() if self.source_date else None,
                "time_precision": self.time_precision, "time_resolution": self.time_resolution,
                "time_issue": self.time_issue}


def capture_quote(value, source, *, fetched_at, response=None, history_index=None):
    """Attach evidence without rejecting an otherwise valid price for bad metadata.

    response is the exact Upbit selected ticker item or er-api rates response.
    history_index must be the index of the selected daily Close in the *same*
    history result. The caller supplies fetched_at immediately after that result
    is received; this module never reads a clock or another provider response.
    """
    price = _price(value)
    fetched = _aware(fetched_at)
    if source not in SOURCES:
        raise ValueError("quote_source_invalid")

    def unknown(issue):
        return Quote(price, source, fetched, time_issue=issue)

    def instant(stamp):
        return Quote(price, source, fetched, source_time=stamp, time_precision="instant",
                     time_resolution="second", time_issue="source_time_future" if stamp > fetched else None)

    if source in {"upbit", "er_api"}:
        if not isinstance(response, Mapping):
            return unknown("source_time_missing")
        try:
            bound_value = response["trade_price"] if source == "upbit" else response["rates"]["KRW"]
            if _price(bound_value) != price:
                return unknown("response_evidence_mismatch")
        except (KeyError, TypeError, ValueError, OverflowError):
            return unknown("response_evidence_mismatch")
        if source == "upbit":
            day, clock = response.get("trade_date"), response.get("trade_time")
            if day is None or clock is None:
                return unknown("source_time_missing")
            if not isinstance(day, str) or not isinstance(clock, str) or not re.fullmatch(r"[0-9]{8}", day) or not re.fullmatch(r"[0-9]{6}", clock):
                return unknown("source_time_invalid")
            try:
                stamp = datetime(int(day[:4]), int(day[4:6]), int(day[6:8]),
                                 int(clock[:2]), int(clock[2:4]), int(clock[4:6]), tzinfo=timezone.utc)
            except ValueError:
                return unknown("source_time_invalid")
            return instant(stamp)
        seconds = response.get("time_last_update_unix")
        if seconds is None:
            return unknown("source_time_missing")
        if type(seconds) is not int or seconds < 0:
            return unknown("source_time_invalid")
        try:
            return instant(datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds))
        except (ValueError, OverflowError):
            return unknown("source_time_invalid")

    if source == "yahoo_gold_history":
        if history_index is None:
            return unknown("source_time_missing")
        # Aware daily index retains its own exchange calendar date. Do not first
        # convert its midnight to UTC (which can change the labelled date).
        if not isinstance(history_index, datetime):
            return unknown("source_time_invalid")
        if history_index.tzinfo is None or history_index.utcoffset() is None:
            return unknown("source_time_naive")
        try:
            label = history_index.date()
            today_in_index_zone = fetched.astimezone(history_index.tzinfo).date()
        except (ValueError, OverflowError):
            return unknown("source_time_invalid")
        return Quote(price, source, fetched, source_date=label, time_precision="date",
                     time_resolution="day", time_issue="source_date_future" if label > today_in_index_zone else None)

    # Naver localTradedAt/public BBO and fast_info time contracts have not been
    # established for these endpoints. Never parse a plausible-looking field.
    return unknown("unverified_time_contract")

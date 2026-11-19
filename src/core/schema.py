"""The records the engine stores. Every number is one Fact, tied to a page of one filing.

Rules enforced here, at the door:
- A value is never invented: value_as_reported has no default and must be a finite decimal.
- Values are kept exactly as printed (currency + scale); conversion happens when read, never stored.
- A confirmed fact must say who confirmed it and when.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum


class Currency(str, Enum):
    USD = "USD"
    KHR = "KHR"


class Scale(int, Enum):
    """How the report prints amounts: exact, in thousands, or in millions."""
    UNITS = 1
    THOUSANDS = 1_000
    MILLIONS = 1_000_000


class InstrumentType(str, Enum):
    EQUITY = "equity"
    BOND = "bond"


class FilingKind(str, Enum):
    ANNUAL = "annual"
    SEMIANNUAL = "semiannual"
    QUARTERLY = "quarterly"
    OTHER = "other"


class Origin(str, Enum):
    """Where a filing came from. Only official filings reach the public dataset and API."""
    OFFICIAL = "official"   # downloaded from the company, the exchange or a broker's filing folder
    UPLOAD = "upload"       # supplied by a user; stays in their own workspace


class PeriodType(str, Enum):
    FY = "FY"
    H1 = "H1"
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"
    M9 = "9M"      # nine months year to date (Q3 reports)
    M12 = "12M"    # twelve months printed in a quarterly report (unaudited; FY is the audited annual)


class Statement(str, Enum):
    BALANCE_SHEET = "BS"
    INCOME = "IS"
    CASH_FLOW = "CF"
    EQUITY_CHANGES = "EQ"
    NOTES = "NOTES"


class Method(str, Enum):
    """How the value got out of the document."""
    TABLE = "table"
    TEXT = "text"
    MANUAL = "manual"


class CheckStatus(str, Enum):
    PROPOSED = "proposed"    # code read it, nobody has looked yet
    CONFIRMED = "confirmed"  # a person checked it against the page
    FAILED = "failed"        # a check (balance, units) rejected it


_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def to_decimal(value: object, field: str) -> Decimal:
    """Exact decimal from str/int/Decimal. Floats are refused: they cannot hold report numbers exactly."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field}: pass a str, int or Decimal, not {type(value).__name__}")
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"{field}: not a number: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{field}: must be finite, got {value!r}")
    return result


def _need_id(value: str, field: str) -> None:
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"{field}: invalid id {value!r}")


@dataclass(frozen=True)
class Issuer:
    id: str                       # exchange ticker, e.g. as printed by the exchange
    name: str
    exchange: str = "CSX"
    instrument_type: InstrumentType | str = InstrumentType.EQUITY
    sector: str = ""
    reporting_currency: Currency | str | None = None

    def __post_init__(self) -> None:
        _need_id(self.id, "issuer.id")
        _need_id(self.exchange, "issuer.exchange")
        if not self.name.strip():
            raise ValueError("issuer.name: empty")
        object.__setattr__(self, "instrument_type", InstrumentType(self.instrument_type))
        if self.reporting_currency is not None:
            object.__setattr__(self, "reporting_currency", Currency(self.reporting_currency))


@dataclass(frozen=True)
class Filing:
    id: str
    issuer_id: str
    kind: FilingKind | str
    source_url: str
    pdf_sha256: str
    published_on: date            # decides which filing wins when a period is restated
    title: str = ""
    retrieved_at: datetime | None = None
    origin: Origin | str = Origin.OFFICIAL

    def __post_init__(self) -> None:
        _need_id(self.id, "filing.id")
        object.__setattr__(self, "origin", Origin(self.origin))
        _need_id(self.issuer_id, "filing.issuer_id")
        object.__setattr__(self, "kind", FilingKind(self.kind))
        if not self.source_url.startswith(("https://", "http://", "file:")):
            raise ValueError(f"filing.source_url: need a URL, got {self.source_url!r}")
        if not _SHA256.match(self.pdf_sha256):
            raise ValueError("filing.pdf_sha256: need 64 lowercase hex characters")
        if not isinstance(self.published_on, date):
            raise TypeError("filing.published_on: need a date")


@dataclass(frozen=True)
class Fact:
    issuer_id: str
    filing_id: str
    statement: Statement | str
    period_end: date
    period_type: PeriodType | str
    line_item: str                # our standard name, snake_case
    label_as_printed: str         # the row label exactly as in the report
    value_as_reported: Decimal | str | int  # required, no default: never invented
    currency: Currency | str
    scale: Scale | int
    page: int                     # 1-based page in the PDF
    method: Method | str
    check_status: CheckStatus | str = CheckStatus.PROPOSED
    label_km: str = ""
    confirmed_by: str = ""
    confirmed_at: datetime | None = None

    def __post_init__(self) -> None:
        _need_id(self.issuer_id, "fact.issuer_id")
        _need_id(self.filing_id, "fact.filing_id")
        object.__setattr__(self, "statement", Statement(self.statement))
        object.__setattr__(self, "period_type", PeriodType(self.period_type))
        object.__setattr__(self, "currency", Currency(self.currency))
        object.__setattr__(self, "scale", Scale(self.scale))
        object.__setattr__(self, "method", Method(self.method))
        object.__setattr__(self, "check_status", CheckStatus(self.check_status))
        object.__setattr__(self, "value_as_reported", to_decimal(self.value_as_reported, "fact.value_as_reported"))
        if not isinstance(self.period_end, date):
            raise TypeError("fact.period_end: need a date")
        if not _SNAKE.match(self.line_item):
            raise ValueError(f"fact.line_item: need snake_case, got {self.line_item!r}")
        if not self.label_as_printed.strip():
            raise ValueError("fact.label_as_printed: empty (we must be able to find it on the page)")
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 1:
            raise ValueError(f"fact.page: need a page number >= 1, got {self.page!r}")
        if self.check_status is CheckStatus.CONFIRMED and not (self.confirmed_by and self.confirmed_at):
            raise ValueError("fact: a confirmed value needs confirmed_by and confirmed_at")

    def value_in_units(self) -> Decimal:
        """The amount in whole currency units (scale applied), still in the reported currency."""
        return self.value_as_reported * int(self.scale)


@dataclass(frozen=True)
class FxRate:
    """One official rate: 1 unit of base = rate units of quote, on that day."""
    on: date
    base: Currency | str
    quote: Currency | str
    rate: Decimal | str | int
    source_url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "base", Currency(self.base))
        object.__setattr__(self, "quote", Currency(self.quote))
        object.__setattr__(self, "rate", to_decimal(self.rate, "fx.rate"))
        if self.base == self.quote:
            raise ValueError("fx: base and quote must differ")
        if self.rate <= 0:
            raise ValueError("fx.rate: must be positive")
        if not self.source_url:
            raise ValueError("fx.source_url: every rate needs its source")


@dataclass(frozen=True)
class Price:
    issuer_id: str
    on: date
    close: Decimal | str | int
    volume: int
    currency: Currency | str
    source_url: str

    def __post_init__(self) -> None:
        _need_id(self.issuer_id, "price.issuer_id")
        object.__setattr__(self, "currency", Currency(self.currency))
        object.__setattr__(self, "close", to_decimal(self.close, "price.close"))
        if self.close <= 0:
            raise ValueError("price.close: must be positive")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume < 0:
            raise ValueError("price.volume: need an int >= 0")
        if not self.source_url:
            raise ValueError("price.source_url: every price needs its source")

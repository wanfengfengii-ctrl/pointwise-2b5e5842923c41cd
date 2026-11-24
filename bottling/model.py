"""Immutable inputs, results and refusals for the split/combine bottle ledger."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AdmittedRowSource:
    """Take millilitres from one exact original row of a formal receipt.

    ``row_ordinal`` may be supplied when a receipt repeats the same device and
    line number; otherwise that pair must select exactly one original row.
    """

    receipt_id: str
    device_id: str
    line_number: int
    milliliters: int
    row_ordinal: int | None = None


@dataclass(frozen=True)
class BottlePortionSource:
    """Take millilitres from one already stored portion of a sealed bottle."""

    bottle_id: str
    portion_ordinal: int
    milliliters: int


@dataclass(frozen=True)
class NewBottle:
    """A bottle to issue, with every stored source portion named explicitly."""

    bottle_id: str
    sources: tuple[AdmittedRowSource | BottlePortionSource, ...]


@dataclass(frozen=True)
class IssuedPortion:
    ordinal: int
    receipt_id: str
    fact_position: int
    row_ordinal: int
    device_id: str
    line_number: int
    path: tuple[str, ...]
    milliliters: int
    source_bottle_id: str | None = None
    source_ordinal: int | None = None


@dataclass(frozen=True)
class IssuedBottle:
    bottle_id: str
    position: int
    total_ml: int
    portions: tuple[IssuedPortion, ...]


@dataclass(frozen=True)
class SplitDecision:
    request_id: str
    position: int
    receipt_id: str
    bottles: tuple[IssuedBottle, ...]


@dataclass(frozen=True)
class PortionView:
    ordinal: int
    receipt_id: str
    fact_position: int
    row_ordinal: int
    device_id: str
    line_number: int
    path: tuple[str, ...]
    issued_ml: int
    drawn_ml: int
    remaining_ml: int


@dataclass(frozen=True)
class SourceDraw:
    """One stored transfer: an issued portion and the source it was drawn from.

    ``source_bottle_id`` is ``None`` for a first split out of an original row.
    """

    request_id: str
    output_bottle_id: str
    output_ordinal: int
    source_bottle_id: str | None
    source_ordinal: int | None
    receipt_id: str
    fact_position: int
    row_ordinal: int
    device_id: str
    line_number: int
    path: tuple[str, ...]
    milliliters: int


@dataclass(frozen=True)
class BottleIssue:
    reason: str
    source: str
    expected: object = None
    actual: object = None
    detail: str = ''


@dataclass(frozen=True)
class BottleAccount:
    bottle_id: str
    split_request_id: str
    split_receipt_id: str
    position: int
    total_ml: int
    drawn_ml: int
    remaining_ml: int
    portions: tuple[PortionView, ...]
    inbound: tuple[SourceDraw, ...]
    outbound: tuple[SourceDraw, ...]
    issues: tuple[BottleIssue, ...] = field(default=())


@dataclass(frozen=True)
class RowAccount:
    receipt_id: str
    fact_position: int
    row_ordinal: int
    device_id: str
    line_number: int
    original_ml: int | None
    drawn_ml: int
    remaining_ml: int | None
    missing: bool
    draws: tuple[SourceDraw, ...] = field(default=())
    issues: tuple[BottleIssue, ...] = field(default=())


class SplitError(ValueError):
    """Base class for a refused split/combine request."""


class InvalidSplitRequest(SplitError):
    """The request is structurally incomplete or names an impossible bottle."""


class SplitRequestConflict(SplitError):
    """A request identifier is already bound to different request parameters."""


class BottleAlreadyExists(SplitError):
    """An output bottle identifier is already used by another issue."""


class SourceUnavailable(SplitError):
    """The named source has no volume left, is missing, or has too little left."""

    def __init__(self, message, *, source, reason, expected=None, actual=None):
        super().__init__(message)
        self.source = source
        self.reason = reason
        self.expected = expected
        self.actual = actual


class EvidenceMismatch(SplitError):
    """Stored provenance no longer matches the formal receipt or original row."""

    def __init__(self, message, *, source, reason, expected=None, actual=None):
        super().__init__(message)
        self.source = source
        self.reason = reason
        self.expected = expected
        self.actual = actual


class UnknownBottle(LookupError):
    """No bottle with this identifier was ever issued."""


class UnknownBottlePortion(LookupError):
    """The bottle exists but has no stored source portion at this ordinal."""


class UnknownOriginalRow(LookupError):
    """The named formal receipt has no single original row with this identity."""

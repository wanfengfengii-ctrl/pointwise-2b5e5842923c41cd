"""Resolve requested portions to exact original rows before any volume moves.

Validation never writes. Every requested source is traced back to one exact
archived original row, so a later write can debit only that row (or the stored
bottle portion that holds it). Any failure rejects the whole request.
"""
import sqlite3
from dataclasses import dataclass

from archive.index import UnknownArchiveReceipt
from archive.integrity import ArchiveIntegrityError
from archive.retrieve import load_claim
from bottling.ledger import (
    drawn_from_portion,
    drawn_from_row,
    header_problems,
    load_bottle,
    load_portion,
)
from bottling.model import (
    AdmittedRowSource,
    BottlePortionSource,
    EvidenceMismatch,
    SourceUnavailable,
    UnknownBottle,
    UnknownBottlePortion,
    UnknownOriginalRow,
)
from intake.units import InvalidReading, to_milliliters


@dataclass(frozen=True)
class ResolvedSource:
    """One requested source, proven to map to one exact original row."""

    receipt_id: str
    fact_position: int
    row_ordinal: int
    device_id: str
    line_number: int
    path: tuple[str, ...]
    available_ml: int
    requested_ml: int
    source_bottle_id: str | None = None
    source_ordinal: int | None = None

    @property
    def key(self) -> tuple:
        if self.source_bottle_id is None:
            return ('row', self.receipt_id, self.fact_position, self.row_ordinal)
        return ('bottle', self.source_bottle_id, self.source_ordinal)

    @property
    def label(self) -> str:
        if self.source_bottle_id is None:
            return f'receipt {self.receipt_id!r} row {self.row_ordinal}'
        return f'bottle {self.source_bottle_id!r} portion {self.source_ordinal}'


@dataclass(frozen=True)
class OriginalRow:
    fact_position: int
    row_ordinal: int
    device_id: str
    line_number: int
    original_ml: int | None
    missing: bool


def receipt_fact_position(warehouse: sqlite3.Connection, receipt_id: str) -> int:
    row = warehouse.execute(
        'SELECT position FROM admission_receipts WHERE receipt_id = ?', (receipt_id,)
    ).fetchone()
    if row is None:
        raise UnknownArchiveReceipt(f'unknown admission receipt {receipt_id!r}')
    return row[0]


def original_rows(
    warehouse: sqlite3.Connection, receipt_id: str
) -> tuple[int, tuple[OriginalRow, ...]]:
    """Verify one receipt's archive and read every original row with its millilitres."""
    try:
        claimed = load_claim(warehouse, receipt_id)
    except UnknownArchiveReceipt as error:
        raise EvidenceMismatch(
            f'receipt {receipt_id!r} cannot be verified in this warehouse',
            source=receipt_id, reason='unknown_receipt', actual=str(error),
        ) from error
    except ArchiveIntegrityError as error:
        raise EvidenceMismatch(
            f'receipt {receipt_id!r} archive is not internally consistent',
            source=receipt_id, reason='archive_integrity', actual=str(error),
        ) from error
    position = receipt_fact_position(warehouse, receipt_id)
    rows = []
    for archived in claimed.rows:
        try:
            milliliters = to_milliliters(archived.value, archived.unit, archived.missing)
        except InvalidReading as error:
            raise EvidenceMismatch(
                f'receipt {receipt_id!r} row {archived.position} has an unsupported reading',
                source=receipt_id, reason='invalid_reading', actual=str(error),
            ) from error
        rows.append(OriginalRow(
            position, archived.position, archived.device_id, archived.line_number,
            milliliters, archived.missing,
        ))
    return position, tuple(rows)


def fact_matches(warehouse: sqlite3.Connection, row: OriginalRow) -> None:
    stored = warehouse.execute(
        '''SELECT device_id, line_number, milliliters FROM fact_rows
           WHERE fact_position = ? AND ordinal = ?''',
        (row.fact_position, row.row_ordinal),
    ).fetchone()
    expected = (row.device_id, hex(row.line_number),
                None if row.original_ml is None else hex(row.original_ml))
    if stored != expected:
        raise EvidenceMismatch(
            f'published row {row.row_ordinal} no longer matches its archived original',
            source=f'fact position {row.fact_position} row {row.row_ordinal}',
            reason='fact_row_mismatch', expected=expected, actual=stored,
        )


def find_admitted_row(
    receipt_id: str, source: AdmittedRowSource, rows: tuple[OriginalRow, ...]
) -> OriginalRow:
    label = f'receipt {receipt_id!r} line {source.line_number} device {source.device_id!r}'
    candidates = [
        row for row in rows
        if row.device_id == source.device_id and row.line_number == source.line_number
        and (source.row_ordinal is None or row.row_ordinal == source.row_ordinal)
    ]
    if not candidates:
        raise UnknownOriginalRow(f'{label} has no matching original row')
    if len(candidates) > 1:
        raise UnknownOriginalRow(
            f'{label} names {len(candidates)} original rows; name the exact row ordinal'
        )
    return candidates[0]


def verify_portion_row(
    warehouse: sqlite3.Connection, portion, label: str
) -> None:
    """Prove one stored bottle portion still matches its archived original row."""
    try:
        position, rows = original_rows(warehouse, portion.receipt_id)
    except EvidenceMismatch as error:
        raise EvidenceMismatch(
            str(error), source=label, reason=error.reason,
            expected=error.expected, actual=error.actual,
        ) from error
    if position != portion.fact_position:
        raise EvidenceMismatch(
            f'{label} points at fact position {portion.fact_position} but its receipt is {position}',
            source=label, reason='fact_position_mismatch',
            expected=portion.fact_position, actual=position,
        )
    match = [row for row in rows if row.row_ordinal == portion.row_ordinal]
    if not match:
        raise EvidenceMismatch(
            f'{label} names original row {portion.row_ordinal} that the receipt does not have',
            source=label, reason='missing_original_row',
            expected=portion.row_ordinal, actual=tuple(row.row_ordinal for row in rows),
        )
    row = match[0]
    stored_identity = (portion.device_id, portion.line_number)
    if (row.device_id, row.line_number) != stored_identity:
        raise EvidenceMismatch(
            f'{label} names an original row with a different device or line',
            source=label, reason='row_identity_mismatch',
            expected=(row.device_id, row.line_number), actual=stored_identity,
        )
    fact_matches(warehouse, row)


def resolve_admitted_row(
    warehouse: sqlite3.Connection, source: AdmittedRowSource
) -> ResolvedSource:
    if type(source.receipt_id) is not str or not source.receipt_id.strip():
        raise UnknownOriginalRow('an original row source requires a receipt identifier')
    position, rows = original_rows(warehouse, source.receipt_id)
    row = find_admitted_row(source.receipt_id, source, rows)
    label = f'receipt {source.receipt_id!r} row {row.row_ordinal}'
    fact_matches(warehouse, row)
    if row.missing:
        raise SourceUnavailable(
            f'{label} is a missing reading and cannot be split',
            source=label, reason='missing_reading', expected='measured volume', actual=None,
        )
    if row.original_ml is None or row.original_ml <= 0:
        raise SourceUnavailable(
            f'{label} holds no measured volume and cannot be split',
            source=label, reason='zero_volume', expected='> 0', actual=row.original_ml,
        )
    available = row.original_ml - drawn_from_row(warehouse, position, row.row_ordinal)
    if source.milliliters > available:
        raise SourceUnavailable(
            f'{label} has only {available} mL left, requested {source.milliliters} mL',
            source=label, reason='insufficient_remaining',
            expected=source.milliliters, actual=available,
        )
    return ResolvedSource(
        source.receipt_id, position, row.row_ordinal, row.device_id, row.line_number,
        (), available, source.milliliters,
    )


def resolve_bottle_portion(
    warehouse: sqlite3.Connection, source: BottlePortionSource
) -> ResolvedSource:
    bottle = load_bottle(warehouse, source.bottle_id)
    if bottle is None:
        raise UnknownBottle(f'unknown bottle {source.bottle_id!r}')
    problems = header_problems(bottle)
    if problems:
        raise EvidenceMismatch(
            f'bottle {source.bottle_id!r} has inconsistent stored portions: {", ".join(problems)}',
            source=f'bottle {source.bottle_id!r}', reason='bottle_header', actual=problems,
        )
    portion = load_portion(warehouse, source.bottle_id, source.portion_ordinal)
    if portion is None:
        raise UnknownBottlePortion(
            f'bottle {source.bottle_id!r} has no portion {source.portion_ordinal}'
        )
    label = f'bottle {source.bottle_id!r} portion {source.portion_ordinal}'
    verify_portion_row(warehouse, portion, label)
    available = portion.issued_ml - drawn_from_portion(
        warehouse, source.bottle_id, source.portion_ordinal
    )
    if source.milliliters > available:
        raise SourceUnavailable(
            f'{label} has only {available} mL left, requested {source.milliliters} mL',
            source=label, reason='insufficient_remaining',
            expected=source.milliliters, actual=available,
        )
    return ResolvedSource(
        portion.receipt_id, portion.fact_position, portion.row_ordinal,
        portion.device_id, portion.line_number, portion.path,
        available, source.milliliters, source.bottle_id, source.portion_ordinal,
    )


def resolve_source(warehouse: sqlite3.Connection, source) -> ResolvedSource:
    if isinstance(source, AdmittedRowSource):
        return resolve_admitted_row(warehouse, source)
    if isinstance(source, BottlePortionSource):
        return resolve_bottle_portion(warehouse, source)
    raise SourceUnavailable(
        'each source must be an AdmittedRowSource or BottlePortionSource',
        source='request', reason='unknown_source_kind', actual=type(source).__name__,
    )

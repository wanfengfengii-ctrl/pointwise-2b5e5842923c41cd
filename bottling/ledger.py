"""Read the stored bottle ledger without touching the catalog or staging tables.

Everything here is derived from ``bottles``, ``bottle_portions`` and
``bottle_draws``, so the ledger stays available when the directory database is
closed or moved away.
"""
import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class StoredPortion:
    bottle_id: str
    ordinal: int
    receipt_id: str
    fact_position: int
    row_ordinal: int
    device_id: str
    line_number: int
    path: tuple[str, ...]
    issued_ml: int


@dataclass(frozen=True)
class StoredDraw:
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
class StoredBottle:
    bottle_id: str
    request_id: str
    split_receipt_id: str
    position: int
    ordinal: int
    total_ml: int
    portions: tuple[StoredPortion, ...]


class LedgerIntegrityError(RuntimeError):
    """Stored bottle rows do not add up to their own header."""


def _portion(row: tuple) -> StoredPortion:
    bottle_id, ordinal, receipt_id, fact_position, row_ordinal, device_id, line_number, path, issued_ml = row
    return StoredPortion(
        bottle_id, ordinal, receipt_id, fact_position, row_ordinal, device_id,
        int(line_number, 16), tuple(json.loads(path)), int(issued_ml, 16),
    )


def _draw(row: tuple) -> StoredDraw:
    (request_id, output_bottle_id, output_ordinal, source_bottle_id, source_ordinal,
     receipt_id, fact_position, row_ordinal, device_id, line_number, path, milliliters) = row
    return StoredDraw(
        request_id, output_bottle_id, output_ordinal, source_bottle_id, source_ordinal,
        receipt_id, fact_position, row_ordinal, device_id, int(line_number, 16),
        tuple(json.loads(path)), int(milliliters, 16),
    )


_PORTION_COLUMNS = '''bottle_id, ordinal, receipt_id, fact_position, row_ordinal,
                       device_id, line_number, path, issued_ml'''

_DRAW_COLUMNS = '''d.request_id, d.output_bottle_id, d.output_ordinal, d.source_bottle_id,
                   d.source_ordinal, d.receipt_id, d.fact_position, d.row_ordinal,
                   d.device_id, d.line_number, d.path, d.milliliters'''


def load_portion(warehouse: sqlite3.Connection, bottle_id: str, ordinal: int) -> StoredPortion | None:
    row = warehouse.execute(
        f'SELECT {_PORTION_COLUMNS} FROM bottle_portions WHERE bottle_id = ? AND ordinal = ?',
        (bottle_id, ordinal),
    ).fetchone()
    return None if row is None else _portion(row)


def load_bottle(warehouse: sqlite3.Connection, bottle_id: str) -> StoredBottle | None:
    header = warehouse.execute(
        '''SELECT b.bottle_id, b.request_id, s.position, b.position, b.ordinal, b.total_ml
           FROM bottles AS b JOIN split_requests AS s USING (request_id)
           WHERE b.bottle_id = ?''',
        (bottle_id,),
    ).fetchone()
    if header is None:
        return None
    portions = warehouse.execute(
        f'SELECT {_PORTION_COLUMNS} FROM bottle_portions WHERE bottle_id = ? ORDER BY ordinal',
        (bottle_id,),
    )
    return StoredBottle(
        header[0], header[1], f'split-{header[2]}', header[3], header[4],
        int(header[5], 16), tuple(_portion(row) for row in portions),
    )


def bottles_for_request(warehouse: sqlite3.Connection, request_id: str) -> tuple[StoredBottle, ...]:
    identifiers = warehouse.execute(
        'SELECT bottle_id FROM bottles WHERE request_id = ? ORDER BY ordinal', (request_id,)
    )
    loaded = [load_bottle(warehouse, bottle_id) for (bottle_id,) in identifiers]
    return tuple(bottle for bottle in loaded if bottle is not None)


def _draws(warehouse: sqlite3.Connection, condition: str, parameter: str) -> tuple[StoredDraw, ...]:
    rows = warehouse.execute(
        f'''SELECT {_DRAW_COLUMNS} FROM bottle_draws AS d
            JOIN bottles AS b ON b.bottle_id = d.output_bottle_id
            WHERE {condition} ORDER BY d.output_bottle_id, d.output_ordinal''',
        (parameter,),
    )
    return tuple(_draw(row) for row in rows)


def load_inbound_draws(warehouse: sqlite3.Connection, bottle_id: str) -> tuple[StoredDraw, ...]:
    """Every draw that created a portion of this bottle."""
    return _draws(warehouse, 'd.output_bottle_id = ?', bottle_id)


def load_outbound_draws(warehouse: sqlite3.Connection, bottle_id: str) -> tuple[StoredDraw, ...]:
    """Every draw that took volume out of one of this bottle's portions."""
    return _draws(warehouse, 'd.source_bottle_id = ?', bottle_id)


def load_row_draws(
    warehouse: sqlite3.Connection, fact_position: int, row_ordinal: int
) -> tuple[StoredDraw, ...]:
    """Every first split of one exact original row, across all requests."""
    rows = warehouse.execute(
        f'''SELECT {_DRAW_COLUMNS} FROM bottle_draws AS d
            JOIN bottles AS b ON b.bottle_id = d.output_bottle_id
            WHERE d.fact_position = ? AND d.row_ordinal = ? AND d.source_bottle_id IS NULL
            ORDER BY d.output_bottle_id, d.output_ordinal''',
        (fact_position, row_ordinal),
    )
    return tuple(_draw(row) for row in rows)


def drawn_from_portion(warehouse: sqlite3.Connection, bottle_id: str, ordinal: int) -> int:
    values = warehouse.execute(
        'SELECT milliliters FROM bottle_draws WHERE source_bottle_id = ? AND source_ordinal = ?',
        (bottle_id, ordinal),
    )
    return sum(int(value, 16) for (value,) in values)


def drawn_from_row(warehouse: sqlite3.Connection, fact_position: int, row_ordinal: int) -> int:
    """Millilitres first-split out of an original row; later transfers do not repeat it."""
    values = warehouse.execute(
        '''SELECT milliliters FROM bottle_draws
           WHERE fact_position = ? AND row_ordinal = ? AND source_bottle_id IS NULL''',
        (fact_position, row_ordinal),
    )
    return sum(int(value, 16) for (value,) in values)


def header_problems(bottle: StoredBottle) -> tuple[str, ...]:
    problems = []
    if not bottle.portions:
        problems.append('no_portion')
    if any(portion.ordinal != index for index, portion in enumerate(bottle.portions)):
        problems.append('portion_ordinal')
    if sum(portion.issued_ml for portion in bottle.portions) != bottle.total_ml:
        problems.append('bottle_total')
    return tuple(problems)

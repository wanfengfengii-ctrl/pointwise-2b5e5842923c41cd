"""Warehouse tables for the split and combine bottle ledger.

The ledger references only already fixed receipt and fact rows, so it stays
readable after the catalog database is closed or moved away.
"""
import sqlite3

BOTTLING_SCHEMA = '''
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS split_order (
    position INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS split_requests (
    request_id TEXT PRIMARY KEY NOT NULL,
    position INTEGER NOT NULL UNIQUE REFERENCES split_order(position),
    parameters TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bottle_order (
    position INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS bottles (
    bottle_id TEXT PRIMARY KEY NOT NULL,
    request_id TEXT NOT NULL REFERENCES split_requests(request_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    position INTEGER NOT NULL UNIQUE REFERENCES bottle_order(position),
    total_ml TEXT NOT NULL,
    UNIQUE (request_id, ordinal)
);
CREATE TABLE IF NOT EXISTS bottle_portions (
    bottle_id TEXT NOT NULL REFERENCES bottles(bottle_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    receipt_id TEXT NOT NULL REFERENCES admission_receipts(receipt_id),
    fact_position INTEGER NOT NULL REFERENCES fact_batches(position),
    row_ordinal INTEGER NOT NULL CHECK (row_ordinal >= 0),
    device_id TEXT NOT NULL,
    line_number TEXT NOT NULL,
    path TEXT NOT NULL,
    issued_ml TEXT NOT NULL,
    PRIMARY KEY (bottle_id, ordinal)
);
CREATE INDEX IF NOT EXISTS bottle_portions_by_row
    ON bottle_portions(fact_position, row_ordinal);
CREATE TABLE IF NOT EXISTS bottle_draws (
    request_id TEXT NOT NULL REFERENCES split_requests(request_id),
    output_bottle_id TEXT NOT NULL,
    output_ordinal INTEGER NOT NULL,
    source_bottle_id TEXT,
    source_ordinal INTEGER,
    receipt_id TEXT NOT NULL,
    fact_position INTEGER NOT NULL,
    row_ordinal INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    line_number TEXT NOT NULL,
    path TEXT NOT NULL,
    milliliters TEXT NOT NULL,
    PRIMARY KEY (output_bottle_id, output_ordinal),
    FOREIGN KEY (output_bottle_id, output_ordinal)
        REFERENCES bottle_portions(bottle_id, ordinal),
    FOREIGN KEY (source_bottle_id, source_ordinal)
        REFERENCES bottle_portions(bottle_id, ordinal)
);
CREATE INDEX IF NOT EXISTS bottle_draws_by_source
    ON bottle_draws(source_bottle_id, source_ordinal);
CREATE INDEX IF NOT EXISTS bottle_draws_by_row
    ON bottle_draws(fact_position, row_ordinal);
COMMIT;
'''


def initialize_bottling(warehouse: sqlite3.Connection) -> None:
    """Create the bottle ledger tables next to the existing warehouse facts."""
    if warehouse.in_transaction:
        raise RuntimeError('bottle ledger initialization requires an idle connection')
    try:
        warehouse.executescript(BOTTLING_SCHEMA)
    except BaseException:
        warehouse.rollback()
        raise


def next_split_position(warehouse: sqlite3.Connection) -> int:
    if not warehouse.in_transaction:
        raise RuntimeError('a split position requires a warehouse transaction')
    cursor = warehouse.execute('INSERT INTO split_order DEFAULT VALUES')
    return cursor.lastrowid


def next_bottle_position(warehouse: sqlite3.Connection) -> int:
    if not warehouse.in_transaction:
        raise RuntimeError('a bottle position requires a warehouse transaction')
    cursor = warehouse.execute('INSERT INTO bottle_order DEFAULT VALUES')
    return cursor.lastrowid

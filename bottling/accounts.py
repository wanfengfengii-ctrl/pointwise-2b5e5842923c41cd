"""Audit bottles and original rows by identifier, entirely from the warehouse.

For each bottle the audit reports its first-issue composition, its usable
remaining volume and every stored hand-off segment. A portion whose archive,
fact row or stored identity no longer agrees yields a concrete issue naming the
source and the reason, instead of silently substituting another receipt or row.
"""
import json
import sqlite3

from bottling.authority import (
    fact_matches,
    find_admitted_row,
    original_rows,
    verify_portion_row,
)
from bottling.ledger import (
    drawn_from_portion,
    drawn_from_row,
    header_problems,
    load_bottle,
    load_inbound_draws,
    load_outbound_draws,
    load_row_draws,
)
from bottling.model import (
    AdmittedRowSource,
    BottleAccount,
    BottleIssue,
    EvidenceMismatch,
    PortionView,
    RowAccount,
    SourceDraw,
    UnknownBottle,
)
from publication.storage import warehouse_snapshot


def _source_draw(draw) -> SourceDraw:
    return SourceDraw(
        draw.request_id, draw.output_bottle_id, draw.output_ordinal,
        draw.source_bottle_id, draw.source_ordinal, draw.receipt_id,
        draw.fact_position, draw.row_ordinal, draw.device_id, draw.line_number,
        draw.path, draw.milliliters,
    )


def _check_header(bottle) -> list[BottleIssue]:
    return [
        BottleIssue(
            'bottle_header', f'bottle {bottle.bottle_id!r}',
            expected='portions that sum to the issued total', actual=problem,
            detail=f'stored bottle {bottle.bottle_id!r} is inconsistent: {problem}',
        )
        for problem in header_problems(bottle)
    ]


def _path_of(warehouse: sqlite3.Connection, bottle_id: str, ordinal: int) -> tuple[str, ...] | None:
    row = warehouse.execute(
        'SELECT path FROM bottle_portions WHERE bottle_id = ? AND ordinal = ?', (bottle_id, ordinal)
    ).fetchone()
    return None if row is None else tuple(json.loads(row[0]))


def _check_draw_links(bottle, warehouse: sqlite3.Connection) -> list[BottleIssue]:
    issues = []
    inbound = {draw.output_ordinal: draw for draw in load_inbound_draws(warehouse, bottle.bottle_id)}
    for portion in bottle.portions:
        label = f'bottle {bottle.bottle_id!r} portion {portion.ordinal}'
        draw = inbound.get(portion.ordinal)
        if draw is None:
            issues.append(BottleIssue(
                'missing_draw', label, expected='one stored source draw', actual=None,
                detail=f'{label} has no stored draw recording where it came from',
            ))
            continue
        stored = (portion.receipt_id, portion.fact_position, portion.row_ordinal,
                  portion.device_id, portion.line_number, portion.path, portion.issued_ml)
        recorded = (draw.receipt_id, draw.fact_position, draw.row_ordinal,
                    draw.device_id, draw.line_number, draw.path, draw.milliliters)
        if stored != recorded:
            issues.append(BottleIssue(
                'portion_draw_mismatch', label, expected=recorded, actual=stored,
                detail=f'{label} does not match the draw that created it',
            ))
            continue
        if draw.source_bottle_id is None:
            expected_path = (bottle.bottle_id,)
        else:
            source_path = _path_of(warehouse, draw.source_bottle_id, draw.source_ordinal)
            if source_path is None:
                issues.append(BottleIssue(
                    'missing_source_portion', label,
                    expected=f'{draw.source_bottle_id!r}#{draw.source_ordinal}', actual=None,
                    detail=f'{label} names a source portion that is no longer stored',
                ))
                continue
            expected_path = (*source_path, bottle.bottle_id)
        if portion.path != tuple(expected_path):
            issues.append(BottleIssue(
                'portion_path', label, expected=expected_path, actual=portion.path,
                detail=f'{label} does not trace the exact bottles it passed through',
            ))
    return issues


def _check_portion_evidence(bottle, warehouse: sqlite3.Connection) -> list[BottleIssue]:
    issues = []
    for portion in bottle.portions:
        label = f'bottle {bottle.bottle_id!r} portion {portion.ordinal}'
        try:
            verify_portion_row(warehouse, portion, label)
        except EvidenceMismatch as error:
            issues.append(BottleIssue(
                error.reason, error.source, expected=error.expected,
                actual=error.actual, detail=str(error),
            ))
    return issues


def _check_remaining(bottle, warehouse: sqlite3.Connection) -> list[BottleIssue]:
    issues = []
    for portion in bottle.portions:
        drawn = drawn_from_portion(warehouse, bottle.bottle_id, portion.ordinal)
        if drawn > portion.issued_ml:
            issues.append(BottleIssue(
                'overdrawn_portion', f'bottle {bottle.bottle_id!r} portion {portion.ordinal}',
                expected=portion.issued_ml, actual=drawn,
                detail='more volume was drawn from this portion than it ever held',
            ))
    return issues


def read_bottle(warehouse: sqlite3.Connection, bottle_id: str) -> BottleAccount:
    """Audit one bottle and every portion in it from the stored warehouse alone."""
    if type(bottle_id) is not str or not bottle_id.strip():
        raise UnknownBottle('bottle_id must be a non-empty string')
    with warehouse_snapshot(warehouse):
        bottle = load_bottle(warehouse, bottle_id)
        if bottle is None:
            raise UnknownBottle(f'unknown bottle {bottle_id!r}')
        issues = _check_header(bottle)
        issues.extend(_check_draw_links(bottle, warehouse))
        issues.extend(_check_portion_evidence(bottle, warehouse))
        issues.extend(_check_remaining(bottle, warehouse))
        portions = tuple(
            PortionView(
                portion.ordinal, portion.receipt_id, portion.fact_position, portion.row_ordinal,
                portion.device_id, portion.line_number, portion.path, portion.issued_ml,
                drawn_from_portion(warehouse, bottle.bottle_id, portion.ordinal),
                max(portion.issued_ml - drawn_from_portion(warehouse, bottle.bottle_id, portion.ordinal), 0),
            )
            for portion in bottle.portions
        )
        inbound = tuple(_source_draw(draw) for draw in load_inbound_draws(warehouse, bottle_id))
        outbound = tuple(_source_draw(draw) for draw in load_outbound_draws(warehouse, bottle_id))
        drawn_total = sum(portion.drawn_ml for portion in portions)
        return BottleAccount(
            bottle_id, bottle.request_id, bottle.split_receipt_id, bottle.position,
            bottle.total_ml, drawn_total, max(bottle.total_ml - drawn_total, 0),
            portions, inbound, outbound, tuple(issues),
        )


def read_original_row(
    warehouse: sqlite3.Connection, receipt_id: str, device_id: str, line_number: int,
    row_ordinal: int | None = None,
) -> RowAccount:
    """Show one original row's issued volume and every first split out of it."""
    with warehouse_snapshot(warehouse):
        position, rows = original_rows(warehouse, receipt_id)
        row = find_admitted_row(
            receipt_id, AdmittedRowSource(receipt_id, device_id, line_number, 1, row_ordinal), rows
        )
        fact_matches(warehouse, row)
        drawn = drawn_from_row(warehouse, position, row.row_ordinal)
        remaining = None if row.original_ml is None else max(row.original_ml - drawn, 0)
        return RowAccount(
            receipt_id, position, row.row_ordinal, row.device_id, row.line_number,
            row.original_ml, drawn, remaining, row.missing,
            tuple(_source_draw(draw) for draw in load_row_draws(
                warehouse, position, row.row_ordinal
            )),
        )

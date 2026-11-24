"""Issue bottles by splitting original rows and combining stored bottle portions.

A request is all-or-nothing. Every source is verified against its exact formal
receipt and original row, every remaining volume is recomputed from the stored
draws inside one write transaction, and only then does any new bottle appear.
A repeated request identifier returns its first receipt when the request
parameters are identical and is refused otherwise.
"""
import json
import sqlite3

from bottling.authority import ResolvedSource, resolve_source
from bottling.ledger import bottles_for_request, header_problems
from bottling.model import (
    AdmittedRowSource,
    BottleAlreadyExists,
    BottlePortionSource,
    IssuedBottle,
    IssuedPortion,
    InvalidSplitRequest,
    NewBottle,
    SourceUnavailable,
    SplitDecision,
    SplitRequestConflict,
)
from bottling.storage import next_bottle_position, next_split_position
from intake.units import encode_integer
from publication.storage import warehouse_write


def _parameters(bottles: tuple[NewBottle, ...]) -> str:
    encoded = []
    for bottle in bottles:
        sources = []
        for source in bottle.sources:
            if isinstance(source, AdmittedRowSource):
                sources.append(('row', source.receipt_id, source.device_id,
                                hex(source.line_number), encode_integer(source.row_ordinal),
                                encode_integer(source.milliliters)))
            else:
                sources.append(('bottle', source.bottle_id, encode_integer(source.portion_ordinal),
                                encode_integer(source.milliliters)))
        encoded.append((bottle.bottle_id, sources))
    return json.dumps((encoded,), separators=(',', ':'))


def _normalise(bottles) -> tuple[NewBottle, ...]:
    if not isinstance(bottles, (list, tuple)) or not bottles:
        raise InvalidSplitRequest('a split request must issue at least one bottle')
    normalised = []
    seen_bottles = set()
    for entry in bottles:
        if not isinstance(entry, NewBottle):
            raise InvalidSplitRequest('every output must be a NewBottle')
        bottle_id = entry.bottle_id
        if type(bottle_id) is not str or not bottle_id.strip():
            raise InvalidSplitRequest('each output bottle needs a non-empty identifier')
        if bottle_id in seen_bottles:
            raise InvalidSplitRequest(f'output bottle {bottle_id!r} is named twice')
        seen_bottles.add(bottle_id)
        sources = tuple(entry.sources)
        if not sources:
            raise InvalidSplitRequest(f'bottle {bottle_id!r} must name at least one source')
        seen_sources = set()
        for source in sources:
            if isinstance(source, AdmittedRowSource):
                if type(source.receipt_id) is not str or not source.receipt_id.strip():
                    raise InvalidSplitRequest('an original row source requires a receipt identifier')
                if type(source.device_id) is not str or not source.device_id.strip():
                    raise InvalidSplitRequest('an original row source requires a device identifier')
                if type(source.line_number) is not int or source.line_number < 1:
                    raise InvalidSplitRequest('an original row needs a positive integer line number')
                if source.row_ordinal is not None and (
                    type(source.row_ordinal) is not int or source.row_ordinal < 0
                ):
                    raise InvalidSplitRequest('a row ordinal must be a non-negative integer')
                key = ('row', source.receipt_id, source.device_id,
                       source.line_number, source.row_ordinal)
            elif isinstance(source, BottlePortionSource):
                if type(source.bottle_id) is not str or not source.bottle_id.strip():
                    raise InvalidSplitRequest('a bottle portion source requires a bottle identifier')
                if type(source.portion_ordinal) is not int or source.portion_ordinal < 0:
                    raise InvalidSplitRequest('a bottle portion ordinal must be a non-negative integer')
                key = ('bottle', source.bottle_id, source.portion_ordinal)
            else:
                raise InvalidSplitRequest(
                    f'bottle {bottle_id!r} names an unsupported source {type(source).__name__}'
                )
            if type(source.milliliters) is not int or source.milliliters <= 0:
                raise InvalidSplitRequest(
                    f'bottle {bottle_id!r} must draw a positive integer number of millilitres'
                )
            if key in seen_sources:
                raise InvalidSplitRequest(
                    f'bottle {bottle_id!r} names the same source twice; every portion must be explicit'
                )
            seen_sources.add(key)
        normalised.append(NewBottle(bottle_id, sources))
    return tuple(normalised)


def _recheck_remaining(resolved: list[ResolvedSource]) -> None:
    demand: dict[tuple, int] = {}
    labels: dict[tuple, ResolvedSource] = {}
    for source in resolved:
        demand[source.key] = demand.get(source.key, 0) + source.requested_ml
        labels[source.key] = source
    for key, requested in demand.items():
        source = labels[key]
        if requested > source.available_ml:
            raise SourceUnavailable(
                f'{source.label} has only {source.available_ml} mL left, request needs {requested} mL',
                source=source.label, reason='insufficient_remaining',
                expected=requested, actual=source.available_ml,
            )


def _ensure_new(warehouse: sqlite3.Connection, bottles: tuple[NewBottle, ...]) -> None:
    for bottle in bottles:
        if warehouse.execute(
            'SELECT 1 FROM bottles WHERE bottle_id = ?', (bottle.bottle_id,)
        ).fetchone() is not None:
            raise BottleAlreadyExists(f'bottle {bottle.bottle_id!r} already exists')


def _portion_path(source: ResolvedSource, bottle_id: str) -> tuple[str, ...]:
    if source.source_bottle_id is None:
        return (bottle_id,)
    return (*source.path, bottle_id)


def _issue_one(
    warehouse: sqlite3.Connection,
    request_id: str,
    ordinal: int,
    bottle: NewBottle,
    resolved: list[ResolvedSource],
) -> IssuedBottle:
    position = next_bottle_position(warehouse)
    warehouse.execute(
        '''INSERT INTO bottles(bottle_id, request_id, ordinal, position, total_ml)
           VALUES (?, ?, ?, ?, ?)''',
        (bottle.bottle_id, request_id, ordinal, position,
         hex(sum(source.requested_ml for source in resolved))),
    )
    portions = []
    for portion_ordinal, source in enumerate(resolved):
        path = _portion_path(source, bottle.bottle_id)
        warehouse.execute(
            '''INSERT INTO bottle_portions(bottle_id, ordinal, receipt_id, fact_position,
                                          row_ordinal, device_id, line_number, path, issued_ml)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (bottle.bottle_id, portion_ordinal, source.receipt_id, source.fact_position,
             source.row_ordinal, source.device_id, hex(source.line_number),
             json.dumps(path, separators=(',', ':')), hex(source.requested_ml)),
        )
        warehouse.execute(
            '''INSERT INTO bottle_draws(request_id, output_bottle_id, output_ordinal,
                                       source_bottle_id, source_ordinal, receipt_id,
                                       fact_position, row_ordinal, device_id, line_number,
                                       path, milliliters)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (request_id, bottle.bottle_id, portion_ordinal,
             source.source_bottle_id, source.source_ordinal, source.receipt_id,
             source.fact_position, source.row_ordinal, source.device_id,
             hex(source.line_number), json.dumps(path, separators=(',', ':')),
             hex(source.requested_ml)),
        )
        portions.append(IssuedPortion(
            portion_ordinal, source.receipt_id, source.fact_position, source.row_ordinal,
            source.device_id, source.line_number, path, source.requested_ml,
            source.source_bottle_id, source.source_ordinal,
        ))
    return IssuedBottle(bottle.bottle_id, position,
                        sum(portion.milliliters for portion in portions), tuple(portions))


def _stored_decision(warehouse: sqlite3.Connection, request_id: str) -> SplitDecision:
    header = warehouse.execute(
        'SELECT position FROM split_requests WHERE request_id = ?', (request_id,)
    ).fetchone()
    issued = []
    for bottle in bottles_for_request(warehouse, request_id):
        problems = header_problems(bottle)
        if problems:
            raise SplitRequestConflict(
                f'stored split request {request_id!r} has an inconsistent bottle {bottle.bottle_id!r}'
            )
        issued.append(IssuedBottle(
            bottle.bottle_id, bottle.position, bottle.total_ml,
            tuple(
                IssuedPortion(portion.ordinal, portion.receipt_id, portion.fact_position,
                              portion.row_ordinal, portion.device_id, portion.line_number,
                              portion.path, portion.issued_ml, draw_source[0], draw_source[1])
                for portion, draw_source in zip(
                    bottle.portions, _draw_sources(warehouse, bottle)
                )
            ),
        ))
    return SplitDecision(request_id, header[0], f'split-{header[0]}', tuple(issued))


def _draw_sources(warehouse: sqlite3.Connection, bottle) -> list[tuple[str | None, int | None]]:
    rows = warehouse.execute(
        '''SELECT source_bottle_id, source_ordinal FROM bottle_draws
           WHERE output_bottle_id = ? ORDER BY output_ordinal''',
        (bottle.bottle_id,),
    )
    return [(source, ordinal) for source, ordinal in rows]


def split(warehouse: sqlite3.Connection, request_id: str, bottles) -> SplitDecision:
    """Split original rows and/or combine sealed bottle portions into new bottles."""
    if type(request_id) is not str or not request_id.strip():
        raise InvalidSplitRequest('request_id must be a non-empty string')
    outputs = _normalise(bottles)
    parameters = _parameters(outputs)

    with warehouse_write(warehouse):
        existing = warehouse.execute(
            'SELECT parameters FROM split_requests WHERE request_id = ?', (request_id,)
        ).fetchone()
        if existing is not None:
            if existing[0] != parameters:
                raise SplitRequestConflict(
                    f'split request {request_id!r} is already bound to different parameters'
                )
            return _stored_decision(warehouse, request_id)

        _ensure_new(warehouse, outputs)
        plans = [[resolve_source(warehouse, source) for source in bottle.sources]
                 for bottle in outputs]
        _recheck_remaining([source for plan in plans for source in plan])

        position = next_split_position(warehouse)
        warehouse.execute(
            'INSERT INTO split_requests(request_id, position, parameters) VALUES (?, ?, ?)',
            (request_id, position, parameters),
        )
        issued = tuple(
            _issue_one(warehouse, request_id, ordinal, bottle, sources)
            for ordinal, (bottle, sources) in enumerate(zip(outputs, plans))
        )
        return SplitDecision(request_id, position, f'split-{position}', issued)

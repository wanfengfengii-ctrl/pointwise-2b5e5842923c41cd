import argparse
import json
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from account.day import read_day, read_day_snapshot
from account.reconcile import reconcile_day
from archive.retrieve import claim
from bottling import (
    AdmittedRowSource,
    BottlePortionSource,
    EvidenceMismatch,
    NewBottle,
    SourceUnavailable,
    read_bottle,
    read_original_row,
    split,
)
from catalog.lifecycle import PointStatus
from catalog.registry import initialize_catalog, open_catalog, register
from catalog.retirement import retire
from intake.rows import SourceRow
from intake.staging import stage
from publication.admission import RetiredPoint, seal
from publication.storage import initialize_warehouse, open_warehouse


@contextmanager
def databases(root, *, create=False):
    if create:
        root.mkdir()
    with closing(open_catalog(root / 'catalog.sqlite3')) as catalog:
        with closing(open_warehouse(root / 'warehouse.sqlite3')) as warehouse:
            if create:
                initialize_catalog(catalog)
                initialize_warehouse(warehouse)
            yield catalog, warehouse


def metrics(account):
    return [account.row_count, account.total_ml]


def equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f'{label} actual={actual!r} expected={expected!r}')


def scene(case_id, inputs, expected, actual, downstream_expected, downstream_actual, paths, oracle):
    record = {
        'case_id': case_id,
        'input': inputs,
        'expected': expected,
        'actual': actual,
        'downstream_expected': downstream_expected,
        'downstream_actual': downstream_actual,
        'code_paths': paths,
        'oracle': oracle,
    }
    print('POINTWISE_SCENARIO ' + json.dumps(record, sort_keys=True), flush=True)


def stage_sample(catalog, warehouse, sample, request_id, group):
    rows = [SourceRow(sample['device_id'], **entry) for entry in sample[group]]
    return stage(catalog, warehouse, request_id, sample['point_id'], sample['business_day'], rows)


def mixed_day(root, sample):
    point_id, business_day = sample['point_id'], sample['business_day']
    with databases(root, create=True) as (catalog, warehouse):
        register(catalog, point_id, sample['device_id'])
        followup = stage_sample(catalog, warehouse, sample, 'followup', 'followup')
        mixed = stage_sample(catalog, warehouse, sample, 'mixed', 'mixed')
        equal([mixed.row_count, mixed.total_ml], [3, 1005], 'mixed staging')
        mixed_receipt = seal(catalog, warehouse, mixed.request_id, mixed.batch_id)
        mixed_actual = metrics(read_day(catalog, warehouse, point_id, business_day))
        equal(mixed_actual, [3, 1005], 'mixed publication')
        followup_receipt = seal(catalog, warehouse, followup.request_id, followup.batch_id)
        retire(catalog, warehouse, 'stop', point_id)
        snapshot = read_day_snapshot(catalog, warehouse, point_id, business_day)
        equal(snapshot.account.receipts, (mixed_receipt, followup_receipt), 'receipt order')
        equal(metrics(snapshot.account), [4, 1012], 'mixed day')
    (root / 'catalog.sqlite3').rename(root / 'catalog.offline')
    with closing(open_warehouse(root / 'warehouse.sqlite3')) as warehouse:
        first = claim(warehouse, mixed_receipt.receipt_id)
        second = claim(warehouse, followup_receipt.receipt_id)
        raw_actual = [[row.line_number, row.value, row.unit, row.missing] for row in (*first.rows, *second.rows)]
        raw_expected = [[30, 1, 'L', False], [10, 5, 'mL', False], [20, None, 'mL', True], [17, 7, 'mL', False]]
        equal(raw_actual, raw_expected, 'offline original rows')
        issues = reconcile_day(warehouse, snapshot)
        equal(issues, (), 'offline reconciliation')
        scene('v8', {'point_id': point_id, 'business_day': business_day,
                     'mixed': sample['mixed'], 'followup': sample['followup']},
              {'mixed': [3, 1005], 'day': [4, 1012]},
              {'mixed': mixed_actual, 'day': metrics(snapshot.account)},
              {'rows': raw_expected, 'issues': 0}, {'rows': raw_actual, 'issues': len(issues)},
              ['intake/staging.py', 'publication/admission.py', 'account/day.py',
               'archive/retrieve.py', 'account/reconcile.py'],
              '1 L contributes 1000 mL, 5 and 7 contribute their raw values, and the missing row contributes one row only.')


def retirement_first(root, sample):
    point_id, business_day = sample['point_id'], sample['business_day']
    with databases(root, create=True) as (catalog, warehouse):
        register(catalog, point_id, sample['device_id'])
        original = stage_sample(catalog, warehouse, sample, 'original', 'original')
        original_receipt = seal(catalog, warehouse, original.request_id, original.batch_id)
        followup = stage_sample(catalog, warehouse, sample, 'F', 'followup')
        retirement = []

        def stop_after_preview(status):
            equal(status, PointStatus.ACTIVE, 'initial eligibility')
            retirement.append(retire(catalog, warehouse, 'stop', point_id))

        receipt = None
        denied = False
        try:
            receipt = seal(catalog, warehouse, followup.request_id, followup.batch_id,
                           before_decision=stop_after_preview)
        except RetiredPoint:
            denied = True
        actual = read_day(catalog, warehouse, point_id, business_day)
        if metrics(actual) != [1, 5]:
            raise AssertionError(f'RETIRED_POINT_FACT_VISIBLE actual={actual.total_ml} expected=5 '
                                 f'actual_rows={actual.row_count} expected_rows=1')
        equal(len(retirement), 1, 'completed retirement')
        equal(denied, True, 'retired followup denial')
        equal(receipt, None, 'followup receipt')
        equal(actual.receipts, (original_receipt,), 'day receipts')
        equal(claim(warehouse, original_receipt.receipt_id).rows[0].value, 5, 'original raw value')
        scene('v4', {'point_id': point_id, 'original_ml': 5, 'staged_ml': 7, 'first': 'retirement'},
              [1, 5], metrics(actual), {'denied': True, 'receipt': None},
              {'denied': denied, 'receipt': receipt},
              ['catalog/retirement.py', 'publication/admission.py', 'account/day.py'],
              'Only the original 5 mL was formally accepted before retirement.')


def admission_first(root, sample):
    point_id, business_day = sample['point_id'], sample['business_day']
    with databases(root, create=True) as (catalog, warehouse):
        register(catalog, point_id, sample['device_id'])
        original = stage_sample(catalog, warehouse, sample, 'original', 'original')
        seal(catalog, warehouse, original.request_id, original.batch_id)
        followup = stage_sample(catalog, warehouse, sample, 'F', 'followup')
        seal(catalog, warehouse, followup.request_id, followup.batch_id)
        retirement = retire(catalog, warehouse, 'stop', point_id)
    with databases(root) as (catalog, warehouse):
        actual = read_day(catalog, warehouse, point_id, business_day)
        equal(metrics(actual), [2, 12], 'reopened day after a lost reply')
        replay = seal(catalog, warehouse, followup.request_id, followup.batch_id)
        equal(replay, actual.receipts[1], 'original admission receipt')
        equal(replay.position < retirement.position, True, 'formal decision order')
        equal(read_day(catalog, warehouse, point_id, business_day), actual, 'day after replay')
    (root / 'catalog.sqlite3').rename(root / 'catalog.offline')
    with closing(open_warehouse(root / 'warehouse.sqlite3')) as warehouse:
        original_row, = claim(warehouse, replay.receipt_id).rows
        original = [original_row.line_number, original_row.value, original_row.unit]
        equal(original, [17, 7, 'mL'], 'followup original row')
        scene('v5', {'point_id': point_id, 'original_ml': 5, 'staged_ml': 7, 'first': 'admission'},
              [2, 12], metrics(actual), {'original': [17, 7, 'mL'], 'same_receipt': True},
              {'original': original, 'same_receipt': replay == actual.receipts[1]},
              ['publication/admission.py', 'publication/retry.py', 'account/day.py', 'archive/retrieve.py'],
              'Two admitted rows contribute 5 + 7 mL. The sample fixes source line 17 for F.')


def bottling_night(root, sample):
    """Split and combine bottles, then audit them after the directory goes away."""
    point_id = sample['bottling_point_id']
    device_id = sample['bottling_device_id']
    business_day = sample['business_day']
    with databases(root, create=True) as (catalog, warehouse):
        register(catalog, point_id, device_id)
        rows = lambda group: [SourceRow(device_id, **entry) for entry in sample['bottling'][group]]
        left = stage(catalog, warehouse, 'left', point_id, business_day, rows('left'))
        right = stage(catalog, warehouse, 'right', point_id, business_day, rows('right'))
        shared = stage(catalog, warehouse, 'shared', point_id, business_day, rows('shared'))
        left_receipt = seal(catalog, warehouse, left.request_id, left.batch_id)
        right_receipt = seal(catalog, warehouse, right.request_id, right.batch_id)
        shared_receipt = seal(catalog, warehouse, shared.request_id, shared.batch_id)

        # Same 10 mL on the same line, but the second receipt keeps its own stock.
        split(warehouse, 'draw-left', (
            NewBottle('A', (AdmittedRowSource(left_receipt.receipt_id, device_id, 4, 6),)),
        ))
        left_row = read_original_row(warehouse, left_receipt.receipt_id, device_id, 4)
        right_row = read_original_row(warehouse, right_receipt.receipt_id, device_id, 4)

        # One original row enters two bottles, then the two paths are merged.
        split(warehouse, 'split-shared', (
            NewBottle('R1', (AdmittedRowSource(shared_receipt.receipt_id, device_id, 4, 60),)),
            NewBottle('R2', (AdmittedRowSource(shared_receipt.receipt_id, device_id, 4, 40),)),
        ))
        merged = split(warehouse, 'merge', (NewBottle('C', (
            BottlePortionSource('R1', 0, 30),
            BottlePortionSource('R2', 0, 20),
        )),))

        # A request whose second output overdraws issues nothing at all.
        failed = False
        try:
            split(warehouse, 'overdraw', (
                NewBottle('NEVER', (BottlePortionSource('C', 0, 10),)),
                NewBottle('TOO-MUCH', (BottlePortionSource('C', 0, 60),)),
            ))
        except SourceUnavailable:
            failed = True

        # A same-volume replacement receipt must not be accepted as the binding.
        substituted = False
        try:
            split(warehouse, 'wrong-receipt', (
                NewBottle('WRONG', (AdmittedRowSource(left_receipt.receipt_id, device_id, 4, 11),)),
            ))
        except SourceUnavailable:
            substituted = True

        pre_offline = read_bottle(warehouse, 'C')
        expected_evidence = [
            [portion.receipt_id, list(portion.path), portion.issued_ml, portion.remaining_ml]
            for portion in pre_offline.portions
        ]

    (root / 'catalog.sqlite3').rename(root / 'catalog.offline')
    with closing(open_warehouse(root / 'warehouse.sqlite3')) as warehouse:
        online = read_bottle(warehouse, 'C')
        never = warehouse.execute("SELECT 1 FROM bottles WHERE bottle_id = 'NEVER'").fetchone()
        split(warehouse, 'offline-more', (
            NewBottle('D', (BottlePortionSource('C', 1, 5),)),
        ))
        downstream = read_bottle(warehouse, 'C')
        moved = read_bottle(warehouse, 'D')
        left_after = read_original_row(warehouse, left_receipt.receipt_id, device_id, 4)
        evidence = [[portion.receipt_id, list(portion.path), portion.issued_ml, portion.remaining_ml]
                    for portion in online.portions]
        scene('v9', {'point_id': point_id, 'same_volume_receipts': 2, 'shared_ml': 100},
              {'left': [10, 6], 'right': [10, 0], 'merge': 50},
              {'left': [left_row.original_ml, left_row.drawn_ml],
               'right': [right_row.original_ml, right_row.drawn_ml],
               'merge': merged.bottles[0].total_ml},
              {'evidence': expected_evidence,
               'failed_clean': True, 'never_issued': True, 'substituted_refused': True,
               'offline_c': 45, 'offline_d': 5, 'left_remaining': 4},
              {'evidence': evidence, 'failed_clean': failed and never is None,
               'never_issued': never is None, 'substituted_refused': substituted,
               'offline_c': downstream.remaining_ml, 'offline_d': moved.remaining_ml,
               'left_remaining': left_after.remaining_ml},
              ['bottling/service.py', 'bottling/authority.py', 'bottling/accounts.py',
               'bottling/ledger.py', 'archive/retrieve.py'],
              'Two equal 10 mL rows stay separate; one row entering R1 and R2 merges as two '
              'distinct paths; the overdrawing request leaves every balance intact; after the '
              'directory is moved the remaining volumes and hand-off segments still audit.')


def bottling_evidence(root, sample):
    """Show that a changed original row is reported, not silently replaced."""
    point_id = sample['bottling_point_id']
    device_id = sample['bottling_device_id']
    business_day = sample['business_day']
    with databases(root, create=True) as (catalog, warehouse):
        register(catalog, point_id, device_id)
        rows = [SourceRow(device_id, **entry) for entry in sample['bottling']['left']]
        batch = stage(catalog, warehouse, 'evidence', point_id, business_day, rows)
        receipt = seal(catalog, warehouse, batch.request_id, batch.batch_id)
        split(warehouse, 'bind', (
            NewBottle('B', (AdmittedRowSource(receipt.receipt_id, device_id, 4, 4),)),
        ))
        warehouse.execute(
            'UPDATE archive_rows SET raw_value = ? WHERE batch_id = ? AND ordinal = 0',
            ('0x63', receipt.batch_id),
        )
        blocked = False
        try:
            split(warehouse, 'later', (NewBottle('C', (BottlePortionSource('B', 0, 1),)),))
        except EvidenceMismatch:
            blocked = True
        account = read_bottle(warehouse, 'B')

    scene('v10', {'receipt': receipt.receipt_id, 'row': [device_id, 4], 'tampered_to': 99},
          {'issues': 1, 'reason': 'archive_integrity', 'remaining': 4, 'blocked': True},
          {'issues': len(account.issues),
           'reason': account.issues[0].reason if account.issues else None,
           'remaining': account.remaining_ml, 'blocked': blocked},
          {'source': "bottle 'B' portion 0", 'detail_mentions_receipt': True},
          {'source': account.issues[0].source if account.issues else None,
           'detail_mentions_receipt': bool(account.issues)
           and receipt.receipt_id in account.issues[0].detail},
          ['bottling/accounts.py', 'bottling/authority.py', 'bottling/ledger.py'],
          'The changed archived row is reported against the exact bottle portion and blocks '
          'further splitting without touching the stored remainder.')


def run(root):
    sample = json.loads(Path(__file__).with_name('sample.json').read_text(encoding='utf-8'))
    mixed_day(root / 'mixed-day', sample)
    retirement_first(root / 'retirement-first', sample)
    admission_first(root / 'admission-first', sample)
    bottling_night(root / 'bottling-night', sample)
    bottling_evidence(root / 'bottling-evidence', sample)


def main():
    parser = argparse.ArgumentParser(description='Run the sampling warehouse handover examples.')
    parser.add_argument('--workspace', type=Path)
    arguments = parser.parse_args()
    if arguments.workspace is None:
        with TemporaryDirectory(prefix='sampling-demo-') as directory:
            run(Path(directory))
    else:
        root = arguments.workspace
        root.mkdir(exist_ok=True)
        if any(root.iterdir()):
            parser.error('--workspace must be empty')
        run(root)


if __name__ == '__main__':
    main()

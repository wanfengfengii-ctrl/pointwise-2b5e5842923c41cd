from bottling import (
    AdmittedRowSource,
    BottleAlreadyExists,
    BottlePortionSource,
    EvidenceMismatch,
    NewBottle,
    SourceUnavailable,
    SplitRequestConflict,
    UnknownBottlePortion,
    read_bottle,
    read_original_row,
    split,
)
from intake.rows import SourceRow
from tests.fixtures import WarehouseCase

class BottlingCase(WarehouseCase):
    def split(self, request, bottles):
        return split(self.warehouse, request, bottles)

    def assert_no_bottles(self):
        self.assertEqual(
            self.warehouse.execute('SELECT COUNT(*) FROM bottles').fetchone()[0], 0
        )
        self.assertEqual(
            self.warehouse.execute('SELECT COUNT(*) FROM bottle_draws').fetchone()[0], 0
        )


class FirstSplitTests(BottlingCase):
    def test_first_split_traces_exact_receipt_row(self):
        receipt = self.seal_batch(self.stage_rows('mixed', (
            SourceRow('device-p', 30, 1, 'L'),
            SourceRow('device-p', 10, 5, 'mL'),
        )))
        decision = self.split('first', (
            NewBottle('B-30', (AdmittedRowSource(receipt.receipt_id, 'device-p', 30, 400),)),
        ))
        bottle, = decision.bottles
        self.assertEqual(bottle.total_ml, 400)
        portion, = bottle.portions
        # One exact original row: 1000 mL from line 30, not the 5 mL row.
        self.assertEqual((portion.receipt_id, portion.device_id, portion.line_number),
                         (receipt.receipt_id, 'device-p', 30))
        row = read_original_row(self.warehouse, receipt.receipt_id, 'device-p', 30)
        self.assertEqual((row.original_ml, row.drawn_ml, row.remaining_ml), (1000, 400, 600))
        other = read_original_row(self.warehouse, receipt.receipt_id, 'device-p', 10)
        self.assertEqual(other.remaining_ml, 5)

    def test_missing_and_zero_cannot_split(self):
        batch = self.stage_rows('mixed', (
            SourceRow('device-p', 30, 0, 'mL'),
            SourceRow('device-p', 20, None, 'mL', missing=True),
        ))
        receipt = self.seal_batch(batch)
        with self.assertRaises(SourceUnavailable) as missing:
            self.split('m', (NewBottle('B-m', (AdmittedRowSource(receipt.receipt_id, 'device-p', 20, 1),)),))
        self.assertEqual(missing.exception.reason, 'missing_reading')
        with self.assertRaises(SourceUnavailable) as zero:
            self.split('z', (NewBottle('B-z', (AdmittedRowSource(receipt.receipt_id, 'device-p', 30, 1),)),))
        self.assertEqual(zero.exception.reason, 'zero_volume')
        self.assert_no_bottles()

    def test_named_row_must_exist_in_the_named_receipt(self):
        first = self.seal_batch(self.stage_value('a', 10, line=4))
        second = self.seal_batch(self.stage_value('b', 10, line=8))
        with self.assertRaises(LookupError):
            # Line 8 belongs to the other receipt, never to this one.
            self.split('bad', (NewBottle('B', (AdmittedRowSource(first.receipt_id, 'device-p', 8, 5),)),))
        from bottling import UnknownOriginalRow
        with self.assertRaises(UnknownOriginalRow):
            self.split('bad2', (NewBottle('B', (AdmittedRowSource(second.receipt_id, 'device-p', 4, 5),)),))
        self.assert_no_bottles()

    def test_same_volume_rows_stay_separate(self):
        first = self.seal_batch(self.stage_value('a', 10, line=4))
        second = self.seal_batch(self.stage_value('b', 10, line=4))
        self.split('from-a', (NewBottle('A1', (AdmittedRowSource(first.receipt_id, 'device-p', 4, 4),)),))
        a = read_original_row(self.warehouse, first.receipt_id, 'device-p', 4)
        b = read_original_row(self.warehouse, second.receipt_id, 'device-p', 4)
        self.assertEqual((a.drawn_ml, a.remaining_ml), (4, 6))
        # Equal volumes and equal identities do not share stock across receipts.
        self.assertEqual((b.drawn_ml, b.remaining_ml), (0, 10))
        self.split('from-b', (NewBottle('B1', (AdmittedRowSource(second.receipt_id, 'device-p', 4, 10),)),))
        b = read_original_row(self.warehouse, second.receipt_id, 'device-p', 4)
        self.assertEqual(b.remaining_ml, 0)
        a = read_original_row(self.warehouse, first.receipt_id, 'device-p', 4)
        self.assertEqual(a.remaining_ml, 6)


class CombineTests(BottlingCase):
    def test_same_original_through_two_bottles_is_distinguished_by_path(self):
        receipt = self.seal_batch(self.stage_value('source', 100, line=4))
        self.split('s1', (NewBottle('R1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 60),)),))
        self.split('s2', (NewBottle('R2', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 40),)),))
        decision = self.split('merge', (NewBottle('C', (
            BottlePortionSource('R1', 0, 30),
            BottlePortionSource('R2', 0, 20),
        )),))
        combined, = decision.bottles
        self.assertEqual(combined.total_ml, 50)
        account = read_bottle(self.warehouse, 'C')
        self.assertEqual(account.remaining_ml, 50)
        self.assertEqual(account.issues, ())
        # Both portions name the same original receipt and row, but their paths
        # and drawn quantities stay distinct.
        seen = [(portion.path, portion.issued_ml) for portion in account.portions]
        self.assertEqual(seen, [(('R1', 'C'), 30), (('R2', 'C'), 20)])
        self.assertEqual({portion.receipt_id for portion in account.portions}, {receipt.receipt_id})
        self.assertEqual({portion.row_ordinal for portion in account.portions}, {0})
        self.assertEqual(read_bottle(self.warehouse, 'R1').remaining_ml, 30)
        self.assertEqual(read_bottle(self.warehouse, 'R2').remaining_ml, 20)

    def test_multi_source_combine_names_each_amount(self):
        a = self.seal_batch(self.stage_value('a', 10, line=4))
        b = self.seal_batch(self.stage_value('b', 7, line=8))
        self.split('fill', (
            NewBottle('PA', (AdmittedRowSource(a.receipt_id, 'device-p', 4, 6),)),
            NewBottle('PB', (AdmittedRowSource(b.receipt_id, 'device-p', 8, 5),)),
        ))
        decision = self.split('mix', (NewBottle('MIX', (
            BottlePortionSource('PA', 0, 2),
            BottlePortionSource('PB', 0, 3),
        )),))
        mix, = decision.bottles
        self.assertEqual(mix.total_ml, 5)
        account = read_bottle(self.warehouse, 'MIX')
        self.assertEqual(
            [(portion.receipt_id, portion.path, portion.issued_ml) for portion in account.portions],
            [(a.receipt_id, ('PA', 'MIX'), 2), (b.receipt_id, ('PB', 'MIX'), 3)],
        )

    def test_multi_output_multi_source_request(self):
        a = self.seal_batch(self.stage_value('a', 10, line=4))
        b = self.seal_batch(self.stage_value('b', 10, line=8))
        decision = self.split('batch', (
            NewBottle('X', (
                AdmittedRowSource(a.receipt_id, 'device-p', 4, 3),
                AdmittedRowSource(b.receipt_id, 'device-p', 8, 4),
            )),
            NewBottle('Y', (
                AdmittedRowSource(a.receipt_id, 'device-p', 4, 5),
                AdmittedRowSource(b.receipt_id, 'device-p', 8, 6),
            )),
        ))
        self.assertEqual([(bottle.bottle_id, bottle.total_ml) for bottle in decision.bottles],
                         [('X', 7), ('Y', 11)])
        # Stock is debited per original row across every output of one request.
        self.assertEqual(read_original_row(self.warehouse, a.receipt_id, 'device-p', 4).remaining_ml, 2)
        self.assertEqual(read_original_row(self.warehouse, b.receipt_id, 'device-p', 8).remaining_ml, 0)


class RefusalTests(BottlingCase):
    def test_failed_request_debits_nothing(self):
        receipt = self.seal_batch(self.stage_value('source', 10, line=4))
        self.split('fill', (NewBottle('B1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 6),)),))
        with self.assertRaises(SourceUnavailable):
            self.split('over', (
                NewBottle('GOOD', (BottlePortionSource('B1', 0, 2),)),
                NewBottle('BAD', (BottlePortionSource('B1', 0, 5),)),
            ))
        # Even the first, valid output is not issued.
        self.assertIsNone(self.warehouse.execute(
            "SELECT 1 FROM bottles WHERE bottle_id = 'GOOD'"
        ).fetchone())
        self.assertEqual(read_bottle(self.warehouse, 'B1').remaining_ml, 6)
        self.assertEqual(read_original_row(self.warehouse, receipt.receipt_id, 'device-p', 4).remaining_ml, 4)

    def test_existing_bottle_id_does_not_disturb_stock(self):
        receipt = self.seal_batch(self.stage_value('source', 10, line=4))
        self.split('one', (NewBottle('B1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 6),)),))
        with self.assertRaises(BottleAlreadyExists):
            self.split('two', (NewBottle('B1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 1),)),))
        self.assertEqual(read_bottle(self.warehouse, 'B1').remaining_ml, 6)
        self.assertEqual(read_original_row(self.warehouse, receipt.receipt_id, 'device-p', 4).remaining_ml, 4)

    def test_unknown_portion_is_refused(self):
        receipt = self.seal_batch(self.stage_value('source', 10, line=4))
        self.split('one', (NewBottle('B1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 6),)),))
        with self.assertRaises(UnknownBottlePortion):
            self.split('bad', (NewBottle('B2', (BottlePortionSource('B1', 4, 1),)),))
        self.assertIsNone(self.warehouse.execute(
            "SELECT 1 FROM bottles WHERE bottle_id = 'B2'"
        ).fetchone())

    def test_replay_same_request_returns_first_receipt(self):
        receipt = self.seal_batch(self.stage_value('source', 10, line=4))
        bottles = (NewBottle('B1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 6),)),)
        first = self.split('same', bottles)
        self.reopen()
        replay = self.split('same', (
            NewBottle('B1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 6),)),
        ))
        self.assertEqual(replay, first)
        # A repeated identifier with different parameters must not issue twice.
        with self.assertRaises(SplitRequestConflict):
            self.split('same', (NewBottle('B1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 5),)),))
        self.assertEqual(read_original_row(self.warehouse, receipt.receipt_id, 'device-p', 4).remaining_ml, 4)


class EvidenceTests(BottlingCase):
    def test_other_receipt_with_same_volume_cannot_be_substituted(self):
        first = self.seal_batch(self.stage_value('a', 10, line=4))
        second = self.seal_batch(self.stage_value('b', 10, line=4))
        self.split('bind', (NewBottle('B1', (AdmittedRowSource(first.receipt_id, 'device-p', 4, 10),)),))
        # The identical-looking second receipt is untouched and B1 stays bound to
        # the first receipt even though both hold 10 mL on line 4.
        account = read_bottle(self.warehouse, 'B1')
        self.assertEqual({portion.receipt_id for portion in account.portions}, {first.receipt_id})
        self.assertEqual(read_original_row(self.warehouse, second.receipt_id, 'device-p', 4).remaining_ml, 10)
        with self.assertRaises(SourceUnavailable):
            self.split('again', (NewBottle('B2', (AdmittedRowSource(first.receipt_id, 'device-p', 4, 1),)),))

    def test_tampered_archive_reports_source_and_reason(self):
        receipt = self.seal_batch(self.stage_value('source', 10, line=4))
        self.split('bind', (NewBottle('B1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 5),)),))
        self.warehouse.execute(
            'UPDATE archive_rows SET raw_value = ? WHERE batch_id = ? AND ordinal = 0', ('0x63', receipt.batch_id)
        )
        account = read_bottle(self.warehouse, 'B1')
        self.assertTrue(account.issues)
        issue = account.issues[0]
        self.assertEqual(issue.reason, 'archive_integrity')
        self.assertEqual(issue.source, "bottle 'B1' portion 0")
        with self.assertRaises(EvidenceMismatch):
            self.split('more', (NewBottle('B2', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 1),)),))
        self.assertEqual(read_bottle(self.warehouse, 'B1').remaining_ml, 5)

    def test_changed_fact_row_reports_reason(self):
        receipt = self.seal_batch(self.stage_value('source', 10, line=4))
        self.split('bind', (NewBottle('B1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 5),)),))
        self.warehouse.execute(
            'UPDATE fact_rows SET milliliters = ? WHERE fact_position = ? AND ordinal = 0',
            ('0x9', receipt.position),
        )
        account = read_bottle(self.warehouse, 'B1')
        reasons = {issue.reason for issue in account.issues}
        self.assertIn('fact_row_mismatch', reasons)


class TimelineTests(BottlingCase):
    def test_retirement_and_downstream_do_not_change_issued_composition(self):
        from catalog.retirement import retire
        receipt = self.seal_batch(self.stage_value('source', 10, line=4))
        self.split('bind', (NewBottle('B1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 8),)),))
        retire(self.catalog, self.warehouse, 'stop', 'P')
        issued = read_bottle(self.warehouse, 'B1')
        self.assertEqual((issued.total_ml, issued.remaining_ml), (8, 8))
        self.assertEqual([(portion.ordinal, portion.issued_ml) for portion in issued.portions], [(0, 8)])
        # Downstream splitting only moves volume out; the initial issue stands.
        self.split('downstream', (NewBottle('B2', (BottlePortionSource('B1', 0, 3),)),))
        after = read_bottle(self.warehouse, 'B1')
        self.assertEqual((after.total_ml, after.remaining_ml, after.drawn_ml), (8, 5, 3))
        self.assertEqual([(portion.ordinal, portion.issued_ml) for portion in after.portions], [(0, 8)])
        self.assertEqual(after.inbound, issued.inbound)

    def test_offline_reopen_audits_composition_remaining_and_segments(self):
        receipt = self.seal_batch(self.stage_value('source', 100, line=4))
        self.split('a', (NewBottle('R1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 60),)),))
        self.split('b', (NewBottle('R2', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 40),)),))
        self.split('c', (NewBottle('C', (
            BottlePortionSource('R1', 0, 25),
            BottlePortionSource('R2', 0, 15),
        )),))
        self.split('d', (NewBottle('D', (BottlePortionSource('C', 1, 10),)),))
        self.take_catalog_offline()
        self.reopen_warehouse_only()
        account = read_bottle(self.warehouse, 'D')
        self.assertEqual(account.issues, ())
        # First-issue composition of D and the full segment it travelled.
        portion, = account.inbound
        self.assertEqual((portion.receipt_id, portion.row_ordinal), (receipt.receipt_id, 0))
        self.assertEqual(portion.path, ('R2', 'C', 'D'))
        self.assertEqual(account.portions[0].path, ('R2', 'C', 'D'))
        self.assertEqual(account.remaining_ml, 10)
        combined = read_bottle(self.warehouse, 'C')
        self.assertEqual([portion.path for portion in combined.portions],
                         [('R1', 'C'), ('R2', 'C')])
        self.assertEqual([portion.remaining_ml for portion in combined.portions], [25, 5])
        # Offline combine still works and consumes from the stored remainder only.
        self.split('e', (NewBottle('E', (BottlePortionSource('C', 1, 5),)),))
        self.assertEqual(read_bottle(self.warehouse, 'C').remaining_ml, 25)


class ConcurrencyTests(BottlingCase):
    def test_two_connections_use_only_actual_remaining(self):
        from contextlib import closing
        from publication.storage import open_warehouse
        receipt = self.seal_batch(self.stage_value('source', 10, line=4))
        with closing(open_warehouse(self.warehouse_path)) as other:
            self.split('first', (NewBottle('B1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 6),)),))
            # The second connection resolves against the committed remainder.
            with self.assertRaises(SourceUnavailable):
                split(other, 'second', (
                    NewBottle('B2', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 10),)),
                ))
            split(other, 'third', (
                NewBottle('B3', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 4),)),
            ))
        row = read_original_row(self.warehouse, receipt.receipt_id, 'device-p', 4)
        self.assertEqual((row.drawn_ml, row.remaining_ml), (10, 0))
        self.assertEqual(read_bottle(self.warehouse, 'B1').remaining_ml, 6)
        self.assertEqual(read_bottle(self.warehouse, 'B3').remaining_ml, 4)

    def test_racing_connections_never_overdraw(self):
        import threading
        from contextlib import closing
        from publication.storage import open_warehouse
        receipt = self.seal_batch(self.stage_value('source', 10, line=4))
        barrier = threading.Barrier(2)
        outcomes = []

        def attempt(name):
            with closing(open_warehouse(self.warehouse_path)) as warehouse:
                barrier.wait()
                try:
                    decision = split(warehouse, name, (
                        NewBottle(name, (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 6),)),
                    ))
                    outcomes.append((name, decision.bottles[0].total_ml))
                except SourceUnavailable:
                    outcomes.append((name, None))

        threads = [threading.Thread(target=attempt, args=(name,)) for name in ('T1', 'T2')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(total for _, total in outcomes if total is not None), [6])
        self.assertEqual(len(outcomes), 2)
        row = read_original_row(self.warehouse, receipt.receipt_id, 'device-p', 4)
        self.assertEqual((row.drawn_ml, row.remaining_ml), (6, 4))

    def test_split_only_needs_the_warehouse_connection(self):
        receipt = self.seal_batch(self.stage_value('source', 10, line=4))
        self.take_catalog_offline()
        self.reopen_warehouse_only()
        decision = self.split('offline', (
            NewBottle('B1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 10),)),
        ))
        self.assertEqual(decision.bottles[0].total_ml, 10)
        self.assertEqual(read_bottle(self.warehouse, 'B1').remaining_ml, 10)


class ExactRowTests(BottlingCase):
    def test_duplicate_identical_rows_need_the_exact_ordinal(self):
        from bottling import UnknownOriginalRow
        receipt = self.seal_batch(self.stage_rows('twice', (
            SourceRow('device-p', 4, 10, 'mL'),
            SourceRow('device-p', 4, 10, 'mL'),
        )))
        with self.assertRaises(UnknownOriginalRow):
            # Device and line alone cannot choose between the two equal rows.
            self.split('ambiguous', (
                NewBottle('B', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 4),)),
            ))
        self.split('exact', (
            NewBottle('B1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 4, row_ordinal=1),)),
        ))
        first = read_original_row(self.warehouse, receipt.receipt_id, 'device-p', 4, row_ordinal=0)
        second = read_original_row(self.warehouse, receipt.receipt_id, 'device-p', 4, row_ordinal=1)
        self.assertEqual((first.drawn_ml, first.remaining_ml), (0, 10))
        self.assertEqual((second.drawn_ml, second.remaining_ml), (4, 6))

    def test_replay_does_not_debit_twice(self):
        receipt = self.seal_batch(self.stage_value('source', 10, line=4))
        bottles = (NewBottle('B1', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 10),)),)
        first = self.split('once', bottles)
        replay = self.split('once', bottles)
        self.assertEqual(replay, first)
        self.assertEqual(read_original_row(self.warehouse, receipt.receipt_id, 'device-p', 4).remaining_ml, 0)
        self.assertEqual(read_bottle(self.warehouse, 'B1').remaining_ml, 10)

    def test_missing_archive_cannot_be_split(self):
        receipt = self.seal_batch(self.stage_value('source', 10, line=4))
        self.warehouse.execute('DELETE FROM archive_rows WHERE batch_id = ?', (receipt.batch_id,))
        with self.assertRaises(EvidenceMismatch) as error:
            self.split('broken', (
                NewBottle('B', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 1),)),
            ))
        self.assertEqual(error.exception.reason, 'archive_integrity')
        self.assertIsNone(self.warehouse.execute(
            "SELECT 1 FROM bottles WHERE bottle_id = 'B'"
        ).fetchone())

    def test_non_positive_amounts_are_refused(self):
        from bottling import InvalidSplitRequest
        receipt = self.seal_batch(self.stage_value('source', 10, line=4))
        for amount in (0, -1, True, 1.5):
            with self.subTest(amount=amount):
                with self.assertRaises(InvalidSplitRequest):
                    self.split(f'bad-{amount}', (
                        NewBottle('B', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, amount),)),
                    ))
        self.assertEqual(read_original_row(self.warehouse, receipt.receipt_id, 'device-p', 4).remaining_ml, 10)

    def test_mid_write_failure_rolls_back_every_bottle(self):
        receipt = self.seal_batch(self.stage_value('source', 10, line=4))
        self.warehouse.execute(
            '''CREATE TEMP TRIGGER refuse_second BEFORE INSERT ON main.bottles
               WHEN NEW.bottle_id = 'SECOND'
               BEGIN SELECT RAISE(ABORT, 'simulated disk error'); END'''
        )
        with self.assertRaisesRegex(Exception, 'simulated disk error'):
            self.split('atomic', (
                NewBottle('FIRST', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 3),)),
                NewBottle('SECOND', (AdmittedRowSource(receipt.receipt_id, 'device-p', 4, 4),)),
            ))
        self.warehouse.execute('DROP TRIGGER refuse_second')
        self.assert_no_bottles()
        self.assertEqual(read_original_row(self.warehouse, receipt.receipt_id, 'device-p', 4).remaining_ml, 10)
        self.assertIsNone(self.warehouse.execute(
            "SELECT 1 FROM split_requests WHERE request_id = 'atomic'"
        ).fetchone())

    def test_repointed_receipt_is_reported_with_source_and_reason(self):
        first = self.seal_batch(self.stage_value('a', 10, line=4))
        second = self.seal_batch(self.stage_value('b', 10, line=4))
        self.split('bind', (NewBottle('B1', (AdmittedRowSource(first.receipt_id, 'device-p', 4, 5),)),))
        # Forge the portion onto the other, equal-volume receipt.
        self.warehouse.execute(
            "UPDATE bottle_portions SET receipt_id = ? WHERE bottle_id = 'B1'",
            (second.receipt_id,),
        )
        account = read_bottle(self.warehouse, 'B1')
        by_reason = {issue.reason: issue for issue in account.issues}
        self.assertIn('fact_position_mismatch', by_reason)
        issue = by_reason['fact_position_mismatch']
        self.assertEqual(issue.source, "bottle 'B1' portion 0")
        self.assertEqual(issue.expected, first.position)
        self.assertEqual(issue.actual, second.position)
        with self.assertRaises(EvidenceMismatch):
            self.split('more', (NewBottle('B2', (BottlePortionSource('B1', 0, 1),)),))
        self.assertEqual(read_bottle(self.warehouse, 'B1').remaining_ml, 5)


class _WarehouseReopen:
    def reopen_warehouse_only(self):
        if self.warehouse is not None:
            self.warehouse.close()
        from publication.storage import open_warehouse
        self.warehouse = open_warehouse(self.warehouse_path)


BottlingCase.reopen_warehouse_only = _WarehouseReopen.reopen_warehouse_only

"""带数量的批次谱系桥：守恒、余额、路径相加、撤回与版本核对。"""
import json
import subprocess
import sys
import unittest
from fractions import Fraction
from pathlib import Path

from warehouse_data.engine import (
    Event, LedgerError, Warehouse, apply_manifest, build_warehouse,
    parse_event, parse_manifest,
)
from warehouse_data.io import load
from warehouse_data.quant import format_quantity, parse_quantity
from warehouse_data.report import build_report, report_json, trace_loss_paths, trace_paths


def event(event_id, inputs, outputs, loss='0', version=1):
    return parse_event({
        'event_id': event_id, 'version': version,
        'inputs': inputs, 'outputs': outputs, 'loss': loss,
    })


def manifest(raw, events, withdrawals=(), version='w0'):
    return {
        'kind': 'lot-bridge', 'raw_lots': raw,
        'events': events, 'withdraw_events': list(withdrawals),
        'batches': [[e['event_id']] for e in events],
        'unit': 'base-mass', 'warehouse_version': version,
    }


class NormalScenarioTests(unittest.TestCase):
    def setUp(self):
        self.value, _ = load('data/normal.json')
        self.wh = build_warehouse(self.value)

    def bridge(self, descendant):
        return {row['root_lot']: row for row in self.wh.bridge_rows()
                if row['descendant_lot'] == descendant}

    def test_bridge_keeps_two_paths_as_quantities_not_reachability(self):
        # r1 经 r1->b->d 与 r1->a->c->d 两条路径汇合，相加不去重
        d = self.bridge('d')
        self.assertEqual(d['r1']['quantity'], Fraction(70))
        self.assertEqual(d['r2']['quantity'], Fraction(20))
        self.assertEqual(d['r1']['ratio'], Fraction(7, 9))
        self.assertEqual(d['r2']['ratio'], Fraction(2, 9))
        # 所有根贡献之和恰等于产量
        self.assertEqual(sum(r['quantity'] for r in d.values()), Fraction(90))

    def test_intermediate_bridge_amounts(self):
        c = self.bridge('c')
        self.assertEqual(c['r1']['quantity'], Fraction(40))
        self.assertEqual(c['r2']['quantity'], Fraction(20))
        self.assertEqual(self.bridge('a')['r1']['quantity'], Fraction(40))
        self.assertEqual(self.bridge('b')['r1']['quantity'], Fraction(50))
        # 原料根自身一行，比例为 1
        self.assertEqual(self.bridge('r1')['r1']['ratio'], Fraction(1))

    def test_segmented_paths_sum_to_bridge(self):
        paths = trace_paths(self.wh, 'd')
        r1_paths = [p for p in paths if p['root_lot'] == 'r1']
        self.assertEqual(len(r1_paths), 2)  # 两条路径都保留
        self.assertEqual(sum(p['quantity'] for p in r1_paths), Fraction(70))
        via_b = next(p for p in r1_paths if p['segments'][-1]['from_lot'] == 'b')
        via_c = next(p for p in r1_paths if p['segments'][-1]['from_lot'] == 'c')
        self.assertEqual(via_b['quantity'], Fraction(30))
        self.assertEqual(via_c['quantity'], Fraction(40))
        # 每段都带因子，且逐段乘积正确
        self.assertEqual(
            [s['event_id'] for s in via_b['segments']], ['split', 'merge'])
        self.assertEqual(
            [s['event_id'] for s in via_c['segments']], ['split', 'mix', 'merge'])

    def test_partial_consumption_leaves_balance_not_erased(self):
        # b 产 50 仅消耗 30，余额 20 留在余额账
        rows = {r['lot']: r for r in build_report(self.wh)['balances']}
        self.assertEqual(rows['b']['produced'], Fraction(50))
        self.assertEqual(rows['b']['consumed'], Fraction(30))
        self.assertEqual(rows['b']['balance'], Fraction(20))
        self.assertEqual(rows['d']['balance'], Fraction(90))
        self.assertEqual(rows['unused']['balance'], Fraction(7))

    def test_loss_is_separate_and_not_reallocated_to_survivors(self):
        ledger = {row['event_id']: row for row in self.wh.loss_ledger}
        self.assertEqual(ledger['split']['loss'], Fraction(10))
        self.assertEqual(ledger['split']['by_root'], {'r1': Fraction(10)})
        # 损耗路径可追溯到根
        loss_paths = trace_loss_paths(self.wh)
        self.assertEqual(len(loss_paths), 1)
        self.assertEqual(loss_paths[0]['root_lot'], 'r1')
        self.assertEqual(loss_paths[0]['quantity'], Fraction(10))
        # 幸存品的占比不含损耗：a+b 仍是 r1 的 90，不是按 100 重新归一
        report = build_report(self.wh)
        self.assertTrue(report['conservation']['balanced'])
        self.assertEqual(report['conservation']['initial_raw'], Fraction(127))
        self.assertEqual(report['conservation']['current_balances'], Fraction(117))
        self.assertEqual(report['conservation']['cumulative_loss'], Fraction(10))

    def test_global_and_per_lot_invariants(self):
        self.wh.verify()  # 不抛即通过
        report = json.loads(report_json(self.wh))
        for row in report['bridge']:
            # 序列化必须是字符串，绝不出现浮点
            self.assertIsInstance(row['quantity'], str)
            self.assertIsInstance(row['ratio'], str)


class FractionalMixTests(unittest.TestCase):
    def test_two_root_mix_with_exact_fractions(self):
        wh = build_warehouse(manifest(
            {'rA': '1', 'rB': '1'},
            [{'event_id': 'm', 'version': 1,
              'inputs': {'rA': '1/3', 'rB': '2/3'},
              'outputs': {'x': '1'}, 'loss': '0'}]))
        row = {r['root_lot']: r for r in wh.bridge_rows() if r['descendant_lot'] == 'x'}
        self.assertEqual(row['rA']['quantity'], Fraction(1, 3))
        self.assertEqual(row['rB']['quantity'], Fraction(2, 3))
        self.assertEqual(row['rA']['ratio'], Fraction(1, 3))
        wh.verify()

    def test_reachability_is_not_contribution_ratio(self):
        # 两个根都可达 z，但数量 90:10，可达性给出的 50:50 是错的
        wh = build_warehouse(manifest(
            {'p': '100', 'q': '100'},
            [{'event_id': 's', 'version': 1,
              'inputs': {'p': '100'}, 'outputs': {'p1': '90', 'p2': '10'},
              'loss': '0'},
             {'event_id': 'm', 'version': 1,
              'inputs': {'p1': '90', 'q': '10'},
              'outputs': {'z': '100'}, 'loss': '0'}]))
        row = {r['root_lot']: r for r in wh.bridge_rows() if r['descendant_lot'] == 'z'}
        self.assertEqual(row['p']['ratio'], Fraction(9, 10))
        self.assertEqual(row['q']['ratio'], Fraction(1, 10))
        self.assertNotEqual(row['p']['ratio'], Fraction(1, 2))


class RejectionTests(unittest.TestCase):
    def test_over_consumption_rejected(self):
        value, _ = load('data/over-consume-balance.json')
        with self.assertRaises(LedgerError):
            build_warehouse(value)

    def test_over_consumption_failure_is_atomic(self):
        base, _ = load('data/normal.json')
        wh = build_warehouse(base)
        bad = event('over', {'b': '21'}, {'e': '21'})
        before = json.loads(report_json(wh))
        with self.assertRaises(LedgerError):
            wh.add_event(bad)
        # 桥、余额、事件集均未变
        after = json.loads(report_json(wh))
        self.assertEqual(before, after)
        self.assertNotIn('over', wh.events)
        self.assertEqual(wh.balances['b'], Fraction(20))

    def test_withdraw_ancestor_orphans_downstream_and_is_rejected(self):
        value, _ = load('data/withdraw-ancestor.json')
        with self.assertRaises(LedgerError) as ctx:
            build_warehouse(value)
        self.assertIn('失去合法输入', str(ctx.exception))

    def test_failed_withdrawal_changes_nothing(self):
        wh = build_warehouse(load('data/normal.json')[0])
        before = json.loads(report_json(wh))
        with self.assertRaises(LedgerError):
            wh.withdraw_event('split')
        self.assertEqual(json.loads(report_json(wh)), before)
        self.assertNotIn('split', wh.withdrawn)
        self.assertEqual(sorted(wh.active_events), ['merge', 'mix', 'split'])

    def test_leaf_withdrawal_succeeds_and_restores_balances(self):
        wh = build_warehouse(load('data/normal.json')[0])
        affected = wh.withdraw_event('merge')
        self.assertIn('d', affected)
        self.assertIn('b', affected)
        self.assertNotIn('d', wh.produced)
        self.assertEqual(wh.balances['b'], Fraction(50))   # 30 的消耗被释放
        self.assertEqual(wh.balances['c'], Fraction(60))
        # d 的桥行消失，无关原料根仍在
        descendants = {row['descendant_lot'] for row in wh.bridge_rows()}
        self.assertNotIn('d', descendants)
        self.assertIn('unused', descendants)
        wh.verify()

    def test_self_loop_and_duplicate_producer_rejected(self):
        wh = Warehouse({'r': '100'})
        wh.add_event(event('e1', {'r': '100'}, {'a': '100'}))
        with self.assertRaises(LedgerError):
            wh.add_event(event('loop', {'a': '50'}, {'a': '50'}))
        with self.assertRaises(LedgerError):
            wh.add_event(event('again', {'a': '50'}, {'a': '50'}))

    def test_raw_lot_cannot_be_produced(self):
        wh = Warehouse({'r': '100'})
        with self.assertRaises(LedgerError):
            wh.add_event(event('e', {'r': '50'}, {'r': '50'}))

    def test_conservation_mismatch_rejected_at_parse(self):
        with self.assertRaises(LedgerError):
            event('bad', {'r': '10'}, {'a': '8'}, loss='1')

    def test_unknown_input_source_rejected(self):
        wh = Warehouse({'r': '100'})
        with self.assertRaises(LedgerError):
            wh.add_event(event('e', {'ghost': '10'}, {'a': '10'}))


class ReplayAndVersionTests(unittest.TestCase):
    def test_replay_same_manifest_does_not_double_consume(self):
        value, _ = load('data/normal.json')
        wh = build_warehouse(value)
        first_digest = wh.source_digest
        affected = apply_manifest(wh, value)  # 整份重放
        self.assertEqual(wh.source_digest, first_digest)
        self.assertEqual(affected, [])       # 幂等，无新影响
        self.assertEqual(wh.balances['b'], Fraction(20))
        wh.verify()

    def test_changed_event_payload_is_an_immutability_violation(self):
        value, _ = load('data/normal.json')
        wh = build_warehouse(value)
        tampered = json.loads(json.dumps(value))
        tampered['events'][0]['version'] = 2
        with self.assertRaises(LedgerError):
            apply_manifest(wh, tampered)

    def test_digest_deterministic_and_version_bound(self):
        value, _ = load('data/normal.json')
        wh1 = build_warehouse(value)
        wh2 = build_warehouse(value)
        self.assertEqual(wh1.source_digest, wh2.source_digest)
        self.assertEqual(wh1.versions['w0'], wh1.source_digest)
        tampered = json.loads(json.dumps(value))
        tampered['events'][0]['version'] = 9
        self.assertNotEqual(build_warehouse(tampered).source_digest,
                            wh1.source_digest)


class IncrementalClosureTests(unittest.TestCase):
    def test_new_event_only_touches_descendant_closure(self):
        wh = build_warehouse(load('data/normal.json')[0])
        affected = wh.add_event(
            event('branch', {'unused': '7'}, {'u': '7'}))
        self.assertEqual(affected, ['u'])          # 无关分支不扫
        self.assertNotIn('d', affected)
        self.assertEqual(wh.balances['unused'], Fraction(0))
        wh.verify()

    def test_rework_creates_new_lot_identity_not_back_edge(self):
        wh = build_warehouse(manifest(
            {'r': '100'},
            [{'event_id': 'make', 'version': 1,
              'inputs': {'r': '100'}, 'outputs': {'a': '60'}, 'loss': '40'},
             {'event_id': 'rework_src', 'version': 1,
              'inputs': {'a': '30'}, 'outputs': {'b': '30'}, 'loss': '0'}]))
        # 返工产出必须是新批身份 a2，不能给旧批 a 加回边
        wh.add_event(event('rework', {'b': '30'}, {'a2': '30'}))
        row = {r['root_lot']: r for r in wh.bridge_rows()
               if r['descendant_lot'] == 'a2'}
        self.assertEqual(row['r']['quantity'], Fraction(30))
        with self.assertRaises(LedgerError):
            wh.add_event(event('back_edge', {'a2': '10'}, {'a': '10'}))
        wh.verify()

    def test_withdrawn_event_id_cannot_be_reused(self):
        wh = build_warehouse(load('data/normal.json')[0])
        wh.withdraw_event('merge')
        with self.assertRaises(LedgerError):
            wh.add_event(event('merge', {'b': '1'}, {'d9': '1'}))

    def test_withdraw_and_correction_with_new_identity_keeps_conservation(self):
        wh = build_warehouse(load('data/normal.json')[0])
        wh.withdraw_event('merge')
        wh.add_event(event('merge_v2', {'b': '20', 'c': '60'}, {'d2': '80'}))
        wh.verify()
        row = {r['root_lot']: r for r in wh.bridge_rows()
               if r['descendant_lot'] == 'd2'}
        self.assertEqual(row['r1']['quantity'], Fraction(60))  # 20 经 b + 40 经 c
        self.assertEqual(row['r2']['quantity'], Fraction(20))
        self.assertEqual(sum(r['quantity'] for r in row.values()), Fraction(80))
        self.assertEqual(wh.balances['b'], Fraction(30))       # 50 - 20
        report = build_report(wh)
        self.assertTrue(report['conservation']['balanced'])

    def test_unrelated_branch_composition_untouched_by_change(self):
        wh = build_warehouse(load('data/normal.json')[0])
        before = {k: dict(v) for k, v in wh.composition.items()}
        wh.add_event(event('sidetrack', {'unused': '3'}, {'u': '3'}))
        for lot, roots in before.items():
            self.assertEqual(wh.composition[lot], roots)


class QuantityParsingTests(unittest.TestCase):
    def test_valid_and_invalid_quantities(self):
        self.assertEqual(parse_quantity('1/3'), Fraction(1, 3))
        self.assertEqual(parse_quantity('100'), Fraction(100))
        self.assertEqual(format_quantity(Fraction(1, 3)), '1/3')
        for bad in (1.0, True, False, '-1', '1/0', '0x1', None, '1.5'):
            with self.assertRaises(ValueError):
                parse_quantity(bad)
        with self.assertRaises(ValueError):
            parse_quantity('0', positive=True)


class CliTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def run_cli(self, name):
        return subprocess.run(
            [sys.executable, '-m', 'warehouse_data.cli', 'build',
             str(self.ROOT / 'data' / name)],
            capture_output=True, text=True, cwd=self.ROOT)

    def test_build_and_rejection_exit_code(self):
        ok = self.run_cli('normal.json')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        payload = json.loads(ok.stdout)
        self.assertTrue(payload['conservation']['balanced'])
        self.assertIn('bridge', payload)

        bad = self.run_cli('withdraw-ancestor.json')
        self.assertEqual(bad.returncode, 2)
        self.assertIn('构建被拒绝', bad.stderr)


if __name__ == '__main__':
    unittest.main()

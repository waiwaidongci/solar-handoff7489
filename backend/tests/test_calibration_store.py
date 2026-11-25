"""校时版本治理的集成测试（基于共享内存桩）。

验证业务规则而不是 ORM：
1. 新版本导入后待发旧报告立即失效、投递计划被停用；
2. 乱序到达的旧版本不会恢复投递资格（stale 只做 0→1）；
3. 已送达的投递保留当时校时版本，不被追溯修改；
4. 失效任务的复核与建计划均被拒绝；
5. 校时记录重复同步幂等，当前版本标记跨数据版本比较。

运行：cd backend && python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import _sqla_stub

_sqla_stub.install()

from src.services import calibration_store as store  # noqa: E402
from fastapi import HTTPException  # noqa: E402

NOW = 1_763_000_000.0


def calibration_row(day, zone, batch, version, lower, upper, dataset_id=1):
    return {'dataset_id': dataset_id, 'shift_date': day, 'zone': zone, 'batch_id': batch,
            'calibration_version': version, 'offset_lower_seconds': lower,
            'offset_upper_seconds': upper, 'created': NOW, 'owner': 'analyst'}


def import_record(day, zone, batch, version, lower, upper):
    return {'kind': 'calibration', 'shift_date': day, 'zone': zone, 'batch_id': batch,
            'calibration_version': version, 'offset_lower_seconds': lower,
            'offset_upper_seconds': upper}


def run_row(run_id, day, zone, basis, stale=0, decision='approved', dataset_id=1):
    return {'id': run_id, 'dataset_id': dataset_id,
            'result': {'rows': [{'晨检日期': day, '区域': zone}],
                       'calibration_basis': [{'校时批次': b, '校时版本': v} for b, v in basis.items()]},
            'decision': decision,
            'calibration_version': max(basis.values()) if basis else None,
            'calibration_basis': dict(basis), 'calibration_stale': stale}


class StalenessWorkflowTests(unittest.TestCase):
    def setUp(self):
        _sqla_stub.reset_data()
        self.conn = _sqla_stub.fresh_connection()

    def seed_v1_and_approved_run(self):
        store.sync_calibrations(self.conn, 1, [
            import_record('2026-11-24', '东区', 'B1', 1, -30, 0)], 'analyst', NOW)
        self.conn.data['analysis_runs'] = [run_row(10, '2026-11-24', '东区', {'B1': 1})]
        self.conn.data['report_schedules'] = [
            {'id': 20, 'run_id': 10, 'active': 1, 'next_at': NOW + 600, 'calibration_version': 1}]
        self.conn.data['report_deliveries'] = [
            {'id': 30, 'run_id': 10, 'calibration_version': 1, 'delivered': NOW - 3600}]

    def test_new_version_invalidates_pending_run_and_stops_schedule(self):
        self.seed_v1_and_approved_run()
        store.sync_calibrations(self.conn, 2, [
            import_record('2026-11-24', '东区', 'B1', 2, -45, 15)], 'analyst', NOW)
        count = store.invalidate_stale_runs(self.conn, now=NOW)
        self.assertEqual(count, 1)
        self.assertEqual(self.conn.data['analysis_runs'][0]['calibration_stale'], 1)
        self.assertEqual(self.conn.data['report_schedules'][0]['active'], 0)
        # 已送达报告保留当时依据，不被追溯。
        self.assertEqual(self.conn.data['report_deliveries'][0]['calibration_version'], 1)
        actions = [row['action'] for row in self.conn.data['operation_audit']]
        self.assertIn('calibration_stale', actions)
        self.assertIn('cancel', actions)

    def test_out_of_order_old_version_never_restores_eligibility(self):
        self.seed_v1_and_approved_run()
        store.sync_calibrations(self.conn, 2, [
            import_record('2026-11-24', '东区', 'B1', 2, -45, 15)], 'analyst', NOW)
        store.invalidate_stale_runs(self.conn, now=NOW)
        # 乱序补到 v1（同区间），只追加审计行，stale 不允许 1→0。
        store.sync_calibrations(self.conn, 3, [
            import_record('2026-11-24', '东区', 'B1', 1, -30, 0)], 'analyst', NOW)
        self.assertEqual(store.invalidate_stale_runs(self.conn, now=NOW), 0)
        self.assertEqual(self.conn.data['analysis_runs'][0]['calibration_stale'], 1)
        with self.assertRaises(HTTPException) as context:
            store.assert_run_current(self.conn, self.conn.data['analysis_runs'][0])
        self.assertEqual(context.exception.status_code, 409)

    def test_concurrent_recompute_is_idempotent(self):
        self.seed_v1_and_approved_run()
        store.sync_calibrations(self.conn, 2, [
            import_record('2026-11-24', '东区', 'B1', 2, -45, 15)], 'analyst', NOW)
        self.assertEqual(store.invalidate_stale_runs(self.conn, now=NOW), 1)
        # 并发重算重复调用：幂等，不会恢复计划。
        self.assertEqual(store.invalidate_stale_runs(self.conn, now=NOW), 0)
        self.assertEqual(self.conn.data['report_schedules'][0]['active'], 0)

    def test_current_run_passes_gate_and_latest_version_marked(self):
        self.seed_v1_and_approved_run()
        listing = store.list_calibrations(1)
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]['is_current'], 1)
        store.assert_run_current(self.conn, self.conn.data['analysis_runs'][0])
        # 追加 v2 后列表中 v1 标记为旧版本（跨数据版本比较）。
        store.sync_calibrations(self.conn, 2, [
            import_record('2026-11-24', '东区', 'B1', 2, -45, 15)], 'analyst', NOW)
        marks = {row['calibration_version']: row['is_current'] for row in store.list_calibrations()}
        self.assertEqual(marks, {1: 0, 2: 1})

    def test_duplicate_sync_is_idempotent(self):
        self.seed_v1_and_approved_run()
        before = len(store.list_calibrations())
        store.sync_calibrations(self.conn, 1, [
            import_record('2026-11-24', '东区', 'B1', 1, -30, 0)], 'analyst', NOW)
        self.assertEqual(len(store.list_calibrations()), before)

    def test_run_without_basis_skips_staleness_check(self):
        # 兼容历史任务：没有校时依据时不拦截（旧逻辑行为）。
        run = run_row(11, '2026-11-24', '东区', {})
        run['calibration_basis'] = {}
        self.conn.data['analysis_runs'] = [run]
        store.assert_run_current(self.conn, run)  # 不抛异常


if __name__ == '__main__':
    unittest.main()

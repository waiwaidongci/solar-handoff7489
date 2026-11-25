"""工作流测试：版本失效后复核、建计划、投递三道关卡。

运行：cd backend && python3 -m unittest discover -s tests -v
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import _sqla_stub

_sqla_stub.install()

from fastapi import HTTPException  # noqa: E402
from src.services import calibration_store as store  # noqa: E402
from src.services import workflow  # noqa: E402

NOW = 1_763_000_000.0


class Body:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def approved_run(run_id, stale=0, decision=None):
    return {'id': run_id, 'dataset_id': 1, 'state': 'completed',
            'result': {'rows': [{'晨检日期': '2026-11-24', '区域': '东区'}],
                       'can_publish': True, 'dispatch_recipient': '东区检修接班组',
                       'calibration_basis': [{'校时批次': 'B1', '校时版本': 1}]},
            'error': None, 'decision': decision, 'reviewer': None, 'review_note': None,
            'calibration_version': 1, 'calibration_basis': {'B1': 1}, 'calibration_stale': stale,
            'started': NOW, 'finished': NOW}


def calibration_import(version, lower, upper):
    return [{'kind': 'calibration', 'shift_date': '2026-11-24', 'zone': '东区', 'batch_id': 'B1',
             'calibration_version': version, 'offset_lower_seconds': lower,
             'offset_upper_seconds': upper}]


class WorkflowStalenessTests(unittest.TestCase):
    def setUp(self):
        _sqla_stub.reset_data()
        self.conn = _sqla_stub.fresh_connection()
        store.sync_calibrations(self.conn, 1, calibration_import(1, -30, 0), 'analyst', NOW)

    def make_pending_approved(self):
        self.conn.data['analysis_runs'] = [approved_run(10)]
        self.conn.data['report_schedules'] = [
            {'id': 20, 'run_id': 10, 'recipient': '东区检修接班组', 'cadence': 'once',
             'active': 1, 'next_at': NOW - 1, 'calibration_version': 1}]

    def push_v2(self):
        store.sync_calibrations(self.conn, 2, calibration_import(2, -45, 15), 'analyst', NOW)
        store.invalidate_stale_runs(self.conn, now=NOW)

    def test_review_blocked_after_version_update(self):
        self.conn.data['analysis_runs'] = [approved_run(10)]
        self.push_v2()
        with self.assertRaises(HTTPException) as context:
            workflow.review_run(10, Body(decision='approved', note='复核通过'), 'reviewer')
        self.assertEqual(context.exception.status_code, 409)
        self.assertIn('失效', context.exception.detail)

    def test_schedule_blocked_after_version_update(self):
        self.make_pending_approved()
        self.push_v2()
        body = Body(recipient='东区检修接班组', cadence='once', scheduled_at=None)
        with self.assertRaises(HTTPException) as context:
            workflow.schedule_report(10, body, 'reviewer')
        self.assertEqual(context.exception.status_code, 409)

    def test_publish_skips_and_deactivates_stale_schedule(self):
        self.make_pending_approved()
        self.push_v2()
        # publish_due 内部会再次校验，失效计划不产生投递。
        workflow.publish_due()
        self.assertEqual(self.conn.data.get('report_deliveries', []), [])
        self.assertEqual(self.conn.data['report_schedules'][0]['active'], 0)

    def test_publish_records_basis_version_on_delivery(self):
        self.make_pending_approved()
        workflow.publish_due()
        deliveries = self.conn.data['report_deliveries']
        self.assertEqual(len(deliveries), 1)
        # 已送达报告保留当时依据版本。
        self.assertEqual(deliveries[0]['calibration_version'], 1)

    def test_fresh_run_review_and_schedule_allowed(self):
        self.conn.data['analysis_runs'] = [approved_run(10)]
        decision = workflow.review_run(10, Body(decision='approved', note='复核通过'), 'reviewer')
        self.assertEqual(decision['decision'], 'approved')
        body = Body(recipient='东区检修接班组', cadence='once', scheduled_at=None)
        scheduled = workflow.schedule_report(10, body, 'reviewer')
        self.assertIn('id', scheduled)

    def test_gate_failure_blocks_approval_even_when_current(self):
        run = approved_run(11)
        run['result']['can_publish'] = False
        self.conn.data['analysis_runs'] = [run]
        with self.assertRaises(HTTPException) as context:
            workflow.review_run(11, Body(decision='approved', note='试图批准'), 'reviewer')
        self.assertEqual(context.exception.status_code, 409)
        self.assertIn('门槛', context.exception.detail)


if __name__ == '__main__':
    unittest.main()

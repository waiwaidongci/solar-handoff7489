"""CSV 导入测试：新版校时列、旧版六列兼容与表头校验。

运行：cd backend && python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import _sqla_stub

_sqla_stub.install()

from src.services.data import csv_records  # noqa: E402
from src.services.domain import compute  # noqa: E402

NEW_HEADER = ('shift_date,zone,kind,inverter_id,heartbeat_at,expected_count,'
              'batch_id,calibration_version,offset_lower_seconds,offset_upper_seconds')
OLD_HEADER = 'shift_date,zone,kind,inverter_id,heartbeat_at,expected_count'


class CsvImportTests(unittest.TestCase):
    def test_legacy_six_column_header_is_accepted(self):
        csv_text = OLD_HEADER + (
            '\n2026-11-24,东区,plan,-,-,1'
            '\n2026-11-24,东区,roster,E-01,-,0'
            '\n2026-11-24,东区,heartbeat,E-01,2026-11-24T06:59:00+08:00,0\n')
        rows = csv_records(csv_text)
        self.assertEqual(len(rows), 3)
        # 心跳归入默认批次；plan/roster 行的批次保持占位符 -。
        self.assertEqual(rows[2]['batch_id'], 'B1')
        self.assertEqual(rows[0]['batch_id'], '-')
        self.assertTrue(all(row['offset_lower_seconds'] == 0 for row in rows))
        result = compute(rows, {'zone': '东区', 'shift_date': '2026-11-24'})
        self.assertTrue(result['can_publish'])
        self.assertEqual(result['calibration_basis'][0]['校时版本'], 1)

    def test_modern_calibration_columns_parsed_as_ints(self):
        csv_text = NEW_HEADER + '\n2026-11-24,东区,calibration,-,-,0,B1,2,-45,15\n'
        row = csv_records(csv_text)[0]
        self.assertEqual(row['kind'], 'calibration')
        self.assertEqual(row['batch_id'], 'B1')
        self.assertEqual((row['calibration_version'], row['offset_lower_seconds'],
                          row['offset_upper_seconds']), (2, -45, 15))

    def test_unknown_header_rejected(self):
        with self.assertRaises(ValueError):
            csv_records('a,b\n1,2\n')

    def test_partial_calibration_header_rejected(self):
        # 只加了批次列、缺版本和上下限，不构成合法的新旧任一套表头。
        header = OLD_HEADER + ',batch_id'
        with self.assertRaises(ValueError):
            csv_records(header + '\n2026-11-24,东区,plan,-,-,3,B1\n')

    def test_offset_out_of_bound_rejected_by_domain(self):
        csv_text = NEW_HEADER + (
            '\n2026-11-24,东区,plan,-,-,1,-,0,0,0'
            '\n2026-11-24,东区,calibration,-,-,0,B1,1,-99999,99999'
            '\n2026-11-24,东区,roster,E-01,-,0,-,0,0,0\n')
        with self.assertRaises(ValueError) as context:
            compute(csv_records(csv_text), {'zone': '东区', 'shift_date': '2026-11-24'})
        self.assertIn('偏移', str(context.exception))


if __name__ == '__main__':
    unittest.main()

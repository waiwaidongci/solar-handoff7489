"""业务计算测试：批次校时解析、全偏移门槛、兼容旧版 CSV 与校验。

运行：cd backend && python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services import domain
from src.services.domain import compute, normalize_rows, sample_rows

DAY = '2026-11-24'
OPTIONS = {'zone': '东区', 'shift_date': DAY, 'cutoff': '07:00',
           'freshness_minutes': 15, 'robust_check': '启用'}


def row(kind, inverter='-', heartbeat='-', expected=0,
        batch='-', version=0, lower=0, upper=0, day=DAY, zone='东区'):
    return {'shift_date': day, 'zone': zone, 'kind': kind, 'inverter_id': inverter,
            'heartbeat_at': heartbeat, 'expected_count': expected, 'batch_id': batch,
            'calibration_version': version, 'offset_lower_seconds': lower,
            'offset_upper_seconds': upper}


def base_shift(zone='东区', lower=-30, upper=0):
    rows = [row('plan', expected=2, zone=zone),
            row('calibration', batch='B1', version=1, lower=lower, upper=upper, zone=zone),
            row('roster', inverter='D-01', zone=zone), row('roster', inverter='D-02', zone=zone)]
    return rows


class SampleDataTests(unittest.TestCase):
    def test_sample_has_calibration_and_batch_columns(self):
        rows = sample_rows()
        kinds = {item['kind'] for item in rows}
        self.assertIn('calibration', kinds)
        calibrations = [item for item in rows if item['kind'] == 'calibration']
        self.assertTrue(all(item['batch_id'] != '-' and item['calibration_version'] >= 1
                            for item in calibrations))
        heartbeats = [item for item in rows if item['kind'] == 'heartbeat']
        self.assertTrue(all(item['batch_id'] != '-' for item in heartbeats))

    def test_robust_gate_blocks_west_zone_on_sample(self):
        # 西区 W-03 走 B2[-60,60]，存在让其失鲜的偏移，全偏移门槛拦截。
        result = compute(sample_rows(), {**OPTIONS, 'zone': '西区'})
        self.assertFalse(result['can_publish'])
        affected = {item['逆变器']: item for item in result['calibration_report']['受影响设备']}
        self.assertEqual(set(affected), {'W-03'})
        # B1[0,30] 共 31 个偏移，B2[-60,60] 共 121 个；W-03 在 B2 取 [-60,0]
        # 的 61 个偏移时失鲜，失败组合 = 61 × 31。
        self.assertEqual(affected['W-03']['失败组合数'], 61 * 31)
        self.assertIn('B2=-60s', affected['W-03']['见证偏移组合'])
        w03 = next(item for item in result['rows'] if item['逆变器'] == 'W-03')
        self.assertEqual(w03['全偏移结论'], '存在失败组合')
        self.assertEqual(w03['校时版本'], '1')
        self.assertEqual(w03['校时批次'], 'B2')

    def test_east_zone_passes_robust_gate_on_sample(self):
        result = compute(sample_rows(), {**OPTIONS, 'zone': '东区'})
        self.assertTrue(result['can_publish'])
        self.assertEqual(result['calibration_report']['受影响设备'], [])
        self.assertIsNone(result['calibration_report']['失败偏移组合'])
        self.assertEqual(result['metrics']['全偏移失败台数'], 0)

    def test_south_zone_still_blocked_by_roster_and_missing_heartbeat(self):
        result = compute(sample_rows(), {**OPTIONS, 'zone': '南区'})
        self.assertFalse(result['can_publish'])
        self.assertIn('名册 2/3 台', result['rows'][0]['发布门禁'])


class RobustGateTests(unittest.TestCase):
    def test_all_combinations_fresh_publishes(self):
        rows = base_shift()
        # 06:58:00 与 06:59:30 在 B1[-30,0] 内均新鲜（最差 -30s 仍在 15 分钟窗）。
        rows += [row('heartbeat', inverter='D-01', heartbeat=f'{DAY}T06:58:00+08:00', batch='B1'),
                 row('heartbeat', inverter='D-02', heartbeat=f'{DAY}T06:59:30+08:00', batch='B1')]
        result = compute(rows, OPTIONS)
        self.assertTrue(result['can_publish'])
        self.assertEqual(result['metrics']['全偏移稳健台数'], 2)
        self.assertEqual(result['calibration_report']['门槛模式'], '全偏移组合')

    def test_failing_witness_and_affected_device_marked(self):
        rows = base_shift(lower=-60, upper=60)
        # D-01 零偏移下 06:45:10 新鲜（15 分钟内），但偏移 -60s 后掉到 16 分钟 → 失败；
        # D-02 选在窗口中段，±60s 修正都留在 15 分钟新鲜窗内，保持稳健作对照。
        rows += [row('heartbeat', inverter='D-01', heartbeat=f'{DAY}T06:45:10+08:00', batch='B1'),
                 row('heartbeat', inverter='D-02', heartbeat=f'{DAY}T06:50:30+08:00', batch='B1')]
        result = compute(rows, OPTIONS)
        # D-01 距截点 15 分钟整在零偏移下新鲜，但偏移 -60s 会掉到 16 分钟 → 失败。
        self.assertFalse(result['can_publish'])
        affected = {item['逆变器']: item for item in result['calibration_report']['受影响设备']}
        self.assertEqual(set(affected), {'D-01'})
        witness = affected['D-01']['见证偏移组合']
        self.assertIn('B1=-60s', witness)
        self.assertIn('D-01', result['headline'])
        detail = next(item for item in result['rows'] if item['逆变器'] == 'D-01')
        self.assertEqual(detail['全偏移结论'], '存在失败组合')
        self.assertGreater(detail['失败组合数'], 0)

    def test_missing_heartbeat_device_is_non_robust(self):
        rows = base_shift()
        rows.append(row('heartbeat', inverter='D-01', heartbeat=f'{DAY}T06:59:00+08:00', batch='B1'))
        result = compute(rows, OPTIONS)
        self.assertFalse(result['can_publish'])
        detail = next(item for item in result['rows'] if item['逆变器'] == 'D-02')
        self.assertEqual(detail['心跳状态'], '缺心跳')
        self.assertEqual(detail['全偏移结论'], '缺心跳')

    def test_offset_can_bring_outside_window_heartbeat_back(self):
        rows = base_shift(lower=-60, upper=60)
        # 07:00:30 在窗口外（零偏移），但偏移 -60s 后为 06:59:30 可进入窗口。
        rows += [row('heartbeat', inverter='D-01', heartbeat=f'{DAY}T07:00:30+08:00', batch='B1'),
                 row('heartbeat', inverter='D-02', heartbeat=f'{DAY}T06:59:00+08:00', batch='B1')]
        result = compute(rows, OPTIONS)
        d01 = next(item for item in result['rows'] if item['逆变器'] == 'D-01')
        # 零偏移时窗口外 → 常规状态缺心跳，但全偏移推演仍把它计入受影响分析。
        self.assertEqual(d01['心跳状态'], '缺心跳')
        self.assertEqual(d01['全偏移结论'], '存在失败组合')
        self.assertIn('D-01', {x['逆变器'] for x in result['calibration_report']['受影响设备']})

    def test_robust_check_disabled_uses_classic_gate(self):
        rows = base_shift(lower=-60, upper=60)
        rows += [row('heartbeat', inverter='D-01', heartbeat=f'{DAY}T06:50:00+08:00', batch='B1'),
                 row('heartbeat', inverter='D-02', heartbeat=f'{DAY}T06:59:59+08:00', batch='B1')]
        result = compute(rows, {**OPTIONS, 'robust_check': '关闭'})
        self.assertTrue(result['can_publish'])
        self.assertEqual(result['metrics']['全偏移稳健台数'], 0)
        self.assertEqual(result['calibration_report']['门槛模式'], '已关闭')


class CalibrationParsingTests(unittest.TestCase):
    def test_out_of_order_rows_keep_highest_version(self):
        rows = base_shift()
        rows += [row('calibration', batch='B1', version=3, lower=-10, upper=10),
                 row('calibration', batch='B1', version=2, lower=-20, upper=20),
                 row('heartbeat', inverter='D-01', heartbeat=f'{DAY}T06:59:00+08:00', batch='B1'),
                 row('heartbeat', inverter='D-02', heartbeat=f'{DAY}T06:59:00+08:00', batch='B1')]
        result = compute(rows, OPTIONS)
        basis = {item['校时批次']: item for item in result['calibration_basis']}
        self.assertEqual(basis['B1']['校时版本'], 3)
        self.assertEqual(basis['B1']['允许偏移下限（秒）'], -10)

    def test_duplicate_same_version_same_range_is_allowed(self):
        rows = base_shift()
        rows.append(row('calibration', batch='B1', version=1, lower=-30, upper=0))
        rows += [row('heartbeat', inverter='D-01', heartbeat=f'{DAY}T06:59:00+08:00', batch='B1'),
                 row('heartbeat', inverter='D-02', heartbeat=f'{DAY}T06:59:00+08:00', batch='B1')]
        result = compute(rows, OPTIONS)
        self.assertTrue(result['can_publish'])

    def test_conflicting_same_version_rejected(self):
        rows = base_shift()
        rows.append(row('calibration', batch='B1', version=1, lower=-45, upper=15))
        with self.assertRaises(ValueError) as context:
            compute(rows, OPTIONS)
        self.assertIn('冲突', str(context.exception))

    def test_heartbeat_referencing_unknown_batch_rejected(self):
        rows = base_shift()
        rows.append(row('heartbeat', inverter='D-01', heartbeat=f'{DAY}T06:59:00+08:00', batch='B9'))
        with self.assertRaises(ValueError) as context:
            compute(rows, OPTIONS)
        self.assertIn('B9', str(context.exception))

    def test_invalid_offset_bounds_rejected(self):
        rows = [row('plan', expected=1),
                row('calibration', batch='B1', version=1, lower=30, upper=-30),
                row('roster', inverter='D-01')]
        with self.assertRaises(ValueError):
            compute(rows, OPTIONS)

    def test_invalid_version_rejected(self):
        rows = [row('plan', expected=1),
                row('calibration', batch='B1', version=0, lower=0, upper=0),
                row('roster', inverter='D-01')]
        with self.assertRaises(ValueError):
            compute(rows, OPTIONS)


class LegacyNormalizationTests(unittest.TestCase):
    LEGACY_KEYS = ('shift_date', 'zone', 'kind', 'inverter_id', 'heartbeat_at', 'expected_count')

    def test_normalize_rows_fills_defaults(self):
        legacy = [{key: value for key, value in row('heartbeat', inverter='D-01',
                    heartbeat=f'{DAY}T06:59:00+08:00').items() if key in self.LEGACY_KEYS}]
        normalized = normalize_rows(legacy)
        self.assertEqual(normalized[0]['batch_id'], domain.DEFAULT_BATCH)
        self.assertEqual(normalized[0]['offset_lower_seconds'], 0)
        self.assertEqual(normalized[0]['offset_upper_seconds'], 0)

    def test_legacy_rows_compute_with_zero_offset(self):
        make = lambda **kwargs: {key: value for key, value in row(**kwargs).items() if key in self.LEGACY_KEYS}
        rows = [make(kind='plan', expected=1), make(kind='roster', inverter='D-01'),
                make(kind='heartbeat', inverter='D-01', heartbeat=f'{DAY}T06:59:00+08:00')]
        # 旧版数据没有 calibration 行：兼容路径合成 B1 版本 1、误差 0 秒的唯一组合，
        # 全偏移门槛与经典零偏移门槛结论一致。
        result = compute(rows, OPTIONS)
        self.assertTrue(result['can_publish'])
        self.assertEqual(result['calibration_basis'],
                         [{'校时批次': 'B1', '校时版本': 1,
                           '允许偏移下限（秒）': 0, '允许偏移上限（秒）': 0}])
        detail = result['rows'][0]
        self.assertEqual(detail['全偏移结论'], '稳健')
        self.assertEqual(detail['校时批次'], 'B1')
        self.assertEqual(detail['校时版本'], '1')


if __name__ == '__main__':
    unittest.main()

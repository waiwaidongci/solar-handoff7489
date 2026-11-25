"""纯逻辑测试：校时偏移组合推演，含笛卡尔积暴力交叉验证。

运行：cd backend && python3 -m unittest discover -s tests -v
"""
import itertools
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services import calibration as calib
from src.services.calibration import Calibration, FreshWindow

# 测试窗口按真实业务的整分钟边界构造：截点 1200，新鲜上限 300 秒，
# 修正心跳落在 [900, 1200) 即归属分钟新鲜（floor 后 >= 900）。
WINDOW = FreshWindow(900, 1199)
# 兼容早期用例的小窗口（同样落在整分边界语义内）。
SMALL_WINDOW = FreshWindow(1000, 1100)


def brute_force(device_heartbeats, calibrations, window):
    """枚举全部偏移组合，返回 (是否稳健, 失败组合数, 一个失败组合)。

    新鲜判定与业务一致：修正时间在窗口内，且归属分钟距截点不超过上限。
    """
    end = window.high + 1
    freshness = end - window.low
    batches = sorted(calibrations)
    choices = {batch: range(calibrations[batch].lower, calibrations[batch].upper + 1)
               for batch in batches}

    def is_fresh(observed, offset):
        corrected = observed + offset
        if not (window.low <= corrected <= window.high):
            return False
        minute = (corrected // 60) * 60
        return end - minute <= freshness

    failing = 0
    witness = None
    for values in itertools.product(*(choices[b] for b in batches)):
        combo = dict(zip(batches, values))
        fresh = any(is_fresh(observed, combo[batch]) for batch, observed in device_heartbeats)
        if not fresh:
            failing += 1
            witness = witness or dict(combo)
    return failing == 0, failing, witness


class FreshWindowTests(unittest.TestCase):
    def test_window_intersects_shift_start(self):
        window = calib.fresh_window(start_epoch=1000, end_epoch=2000, freshness_seconds=600)
        self.assertEqual(window, FreshWindow(1400, 1999))

    def test_window_clamped_by_start(self):
        window = calib.fresh_window(start_epoch=1900, end_epoch=2000, freshness_seconds=600)
        self.assertEqual(window, FreshWindow(1900, 1999))


class EvaluateDeviceTests(unittest.TestCase):
    def setUp(self):
        self.calibrations = {
            'B1': Calibration('B1', 1, -2, 2),
            'B2': Calibration('B2', 1, -3, 3),
        }

    def test_single_heartbeat_partial_coverage(self):
        # 1100 需要 offset ∈ [-100, 0]，与 [-2,2] 交集为 [-2,0]，坏偏移 {1,2}。
        verdict = calib.evaluate_device([('B1', 1100)], self.calibrations, SMALL_WINDOW)
        self.assertFalse(verdict['robust'])
        self.assertEqual(verdict['failing_combinations'], 2 * 7)
        self.assertEqual(verdict['witness'], {'B1': 1})
        robust, failing, witness = brute_force([('B1', 1100)], self.calibrations, SMALL_WINDOW)
        self.assertFalse(robust)
        self.assertEqual(failing, verdict['failing_combinations'])
        self.assertIsNotNone(witness)

    def test_single_heartbeat_full_coverage_is_robust(self):
        # 1098 需要 [-2,2]，恰好覆盖整个批次区间。
        verdict = calib.evaluate_device([('B1', 1098)], self.calibrations, SMALL_WINDOW)
        self.assertTrue(verdict['robust'])
        self.assertEqual(verdict['failing_combinations'], 0)
        robust, failing, _ = brute_force([('B1', 1098)], self.calibrations, SMALL_WINDOW)
        self.assertTrue(robust)
        self.assertEqual(failing, 0)

    def test_two_heartbeats_union_covers_range(self):
        # 1100 安全区间 [-2,0]，1098 安全区间 [0,2]，并集恰好覆盖 [-2,2]。
        heartbeats = [('B1', 1100), ('B1', 1098)]
        verdict = calib.evaluate_device(heartbeats, self.calibrations, SMALL_WINDOW)
        self.assertTrue(verdict['robust'])
        robust, failing, _ = brute_force(heartbeats, self.calibrations, SMALL_WINDOW)
        self.assertTrue(robust)
        self.assertEqual(failing, 0)

    def test_gap_in_good_intervals_counts_disjoint_bad_sets(self):
        # 整分钟对齐窗口 [1080,1140)：两条心跳的安全区间互不相邻，
        # 中间留出坏偏移，便于验证不相交区间的计数。
        narrow = FreshWindow(1080, 1139)
        calibration = {'B1': Calibration('B1', 1, -10, 10)}
        heartbeats = [('B1', 1140), ('B1', 1130)]  # 安全区间 [-60,-1] 与 [-50,9]
        verdict = calib.evaluate_device(heartbeats, calibration, narrow)
        self.assertFalse(verdict['robust'])
        # 并集 [-50,9]，坏偏移只有 {10}。
        self.assertEqual(verdict['failing_combinations'], 1)
        self.assertEqual(verdict['witness'], {'B1': 10})
        robust, failing, witness = brute_force(heartbeats, calibration, narrow)
        self.assertFalse(robust)
        self.assertEqual(failing, 1)
        self.assertIsNotNone(witness)

    def test_multi_batch_independence(self):
        # B1 坏偏移 {1,2}，B2 坏偏移 {1,2,3}，失败组合 2×3=6。
        heartbeats = [('B1', 1100), ('B2', 1100)]
        verdict = calib.evaluate_device(heartbeats, self.calibrations, SMALL_WINDOW)
        self.assertFalse(verdict['robust'])
        self.assertEqual(verdict['failing_combinations'], 6)
        self.assertEqual(verdict['witness'], {'B1': 1, 'B2': 1})
        robust, failing, _ = brute_force(heartbeats, self.calibrations, SMALL_WINDOW)
        self.assertFalse(robust)
        self.assertEqual(failing, 6)

    def test_multi_batch_one_batch_always_good_makes_robust(self):
        # B2 的心跳 1097 的安全区间 [-3,3] 覆盖整个 B2 允许区间。
        heartbeats = [('B1', 1100), ('B2', 1097)]
        verdict = calib.evaluate_device(heartbeats, self.calibrations, SMALL_WINDOW)
        self.assertTrue(verdict['robust'])
        robust, failing, _ = brute_force(heartbeats, self.calibrations, SMALL_WINDOW)
        self.assertTrue(robust)
        self.assertEqual(failing, 0)

    def test_unknown_batch_and_empty_heartbeats(self):
        verdict = calib.evaluate_device([('B9', 1098)], self.calibrations, SMALL_WINDOW)
        self.assertFalse(verdict['robust'])
        self.assertIsNone(verdict['failing_combinations'])
        verdict = calib.evaluate_device([], self.calibrations, SMALL_WINDOW)
        self.assertFalse(verdict['robust'])
        self.assertEqual(verdict['witness'], {})

    def test_minute_boundary_offsets_match_business_rule(self):
        # 截点 1200、上限 5 分钟：归属分钟须 >= 900。
        # 原始 905（06:15:05 类比）：offset=-5 修正为 900，归属分钟 900 新鲜；
        # offset=-6 修正为 899，归属分钟 840，失鲜。
        calibration = {'B1': Calibration('B1', 1, -10, 10)}
        verdict = calib.evaluate_device([('B1', 905)], calibration, WINDOW)
        self.assertFalse(verdict['robust'])
        # 安全偏移 [-5, 294]∩[-10,10] = [-5,10] 共 16 个；坏偏移 [-10,-6] 共 5 个。
        self.assertEqual(verdict['failing_combinations'], 5)
        self.assertEqual(verdict['witness'], {'B1': -10})
        robust, failing, witness = brute_force([('B1', 905)], calibration, WINDOW)
        self.assertFalse(robust)
        self.assertEqual(failing, 5)
        # 边界两侧的代表偏移：-5 新鲜、-6 失鲜。
        self.assertTrue(900 <= 905 - 5 <= WINDOW.high and ((905 - 5) // 60) * 60 >= 900)
        self.assertFalse(900 <= 905 - 6 <= WINDOW.high and ((905 - 6) // 60) * 60 >= 900)

    def test_witness_is_actually_failing_across_cases(self):
        # 随机化若干场景，验证解析法与暴力枚举结论、计数一致，见证确实失败。
        import random
        rng = random.Random(20261124)
        for _ in range(40):
            calibrations = {b: Calibration(b, 1, rng.randint(-5, 0), rng.randint(0, 5))
                            for b in ('B1', 'B2')}
            heartbeats = [(rng.choice(('B1', 'B2')), rng.randint(850, 1250))
                          for _ in range(rng.randint(1, 4))]
            verdict = calib.evaluate_device(heartbeats, calibrations, WINDOW)
            robust, failing, _ = brute_force(heartbeats, calibrations, WINDOW)
            self.assertEqual(verdict['robust'], robust)
            self.assertEqual(verdict['failing_combinations'], failing)
            if not robust:
                combo = calib.merge_witness(verdict['witness'], sorted(calibrations), calibrations)
                fresh = any(WINDOW.low <= observed + combo[batch] <= WINDOW.high
                            and ((observed + combo[batch]) // 60) * 60 >= WINDOW.low
                            for batch, observed in heartbeats)
                self.assertFalse(fresh)


if __name__ == '__main__':
    unittest.main()

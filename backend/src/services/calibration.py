"""网关校时批次的偏移组合推演。

晨检 CSV 的心跳按「批次」补录校时记录：同一批次内的所有心跳共用一个
网关时钟偏移，不同批次允许取各自允许区间内的不同整数秒偏移。本模块只做
纯逻辑推演，不依赖数据库或 Web 框架，便于直接用标准库测试。

约定：
- ``offset`` 为加到网关本地时间上的修正秒数（offset > 0 表示网关偏慢）；
- 修正时间 = 心跳原始时间 + offset；
- 窗口为 ``[start, end)``，新鲜条件为 ``0 <= end - minute <= freshness``，
  其中 ``minute`` 是修正心跳截断到本地分钟后的时间；
- 允许的偏移组合为各批次区间的笛卡尔积，仅当全部组合都满足门槛时才算通过。

推演方法（批次之间相互独立，可逐批判定）：
对设备在批次 b 内的每条心跳，算出能让它新鲜的偏移区间 N_i；并集
G_b 即该批次的「安全偏移」。其余偏移 B_b 会让该设备在批次 b 的所有
心跳同时失鲜。只要存在一个批次 B_b 为空，该设备在任意组合下都有新鲜
心跳（稳健）；否则从每个批次各取一个坏偏移即得到失败见证组合。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Calibration:
    """一条批次校时依据（同一批次同版本只允许一条）。"""

    batch: str
    version: int
    lower: int
    upper: int


@dataclass(frozen=True)
class FreshWindow:
    """修正心跳被视为窗口内且新鲜的允许时间区间（秒，含端点）。"""

    low: int
    high: int


def fresh_window(start_epoch, end_epoch, freshness_seconds):
    """返回修正心跳可接受的秒级区间（与归属分钟口径对齐）。

    心跳必须落在窗口 ``[start, end)`` 内，且其归属分钟（截断到整分）距
    截点不超过新鲜度上限：``floor(修正时间 / 60) * 60 >= end - freshness``
    且修正时间 ``< end``。因此修正秒数需满足
    ``end - freshness <= 修正时间 < end``，再与窗口起点取交集。
    """
    return FreshWindow(max(start_epoch, end_epoch - freshness_seconds), end_epoch - 1)


def _merge_intervals(intervals):
    """合并相邻或重叠的整数闭区间。"""
    merged = []
    for low, high in sorted(intervals):
        if merged and low <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def _good_intervals(calibration: Calibration, observed_epochs, window: FreshWindow):
    """设备在该批次内「至少一条心跳新鲜」的偏移区间（并集）。"""
    parts = []
    for observed in observed_epochs:
        low = max(calibration.lower, window.low - observed)
        high = min(calibration.upper, window.high - observed)
        if low <= high:
            parts.append((low, high))
    return _merge_intervals(parts)


def _bad_intervals(calibration: Calibration, observed_epochs, window: FreshWindow):
    """设备在该批次内「所有心跳都失鲜」的偏移区间。"""
    bad = []
    cursor = calibration.lower
    for low, high in _good_intervals(calibration, observed_epochs, window):
        if low > cursor:
            bad.append((cursor, low - 1))
        cursor = max(cursor, high + 1)
    if cursor <= calibration.upper:
        bad.append((cursor, calibration.upper))
    return bad


def _interval_size(intervals):
    return sum(high - low + 1 for low, high in intervals)


def evaluate_device(device_heartbeats, calibrations: dict, window: FreshWindow):
    """评估单台设备在全偏移组合下的稳健性。

    ``device_heartbeats`` 为 ``(batch, observed_epoch)`` 列表。返回：
    - ``robust``：是否在任意允许组合下都有新鲜心跳；
    - ``witness``：失败时的一组偏移（含所有相关批次），稳健时为 ``None``；
    - ``bad_by_batch``：相关批次的坏偏移区间；
    - ``failing_combinations``：会让该设备失败的完整组合数（按全部批次计）。
    """
    if not device_heartbeats:
        return {'robust': False, 'witness': {}, 'bad_by_batch': {}, 'failing_combinations': None}

    by_batch: dict[str, list[int]] = {}
    for batch, observed in device_heartbeats:
        if batch not in calibrations:
            return {'robust': False, 'witness': {}, 'bad_by_batch': {}, 'failing_combinations': None}
        by_batch.setdefault(batch, []).append(observed)

    bad_by_batch = {}
    witness = {}
    for batch, observed_epochs in by_batch.items():
        bad = _bad_intervals(calibrations[batch], observed_epochs, window)
        if not bad:
            # 该批次在整个允许区间内都能保证至少一条新鲜心跳。
            return {'robust': True, 'witness': None, 'bad_by_batch': {}, 'failing_combinations': 0}
        bad_by_batch[batch] = bad
        witness[batch] = bad[0][0]

    # 相关批次的坏偏移选择数之积，乘以未涉及批次的全部选择数。
    failing = 1
    for batch, bad in bad_by_batch.items():
        failing *= _interval_size(bad)
    for batch, calibration in calibrations.items():
        if batch not in bad_by_batch:
            failing *= calibration.upper - calibration.lower + 1
    return {'robust': False, 'witness': witness, 'bad_by_batch': bad_by_batch,
            'failing_combinations': failing}


def merge_witness(witness, batches, calibrations):
    """把单设备见证补齐为覆盖所有批次的完整组合，未涉及批次取下界。"""
    return {batch: witness.get(batch, calibrations[batch].lower) for batch in batches}

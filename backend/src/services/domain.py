import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from src.services import calibration as calib
from src.services.calibration import Calibration


SHANGHAI = ZoneInfo('Asia/Shanghai')
ZONES = ('东区', '西区', '南区')
SHIFT_START = time(6)
NOON = time(12)
DEFAULT_BATCH = 'B1'
DEFAULT_VERSION = 1
# 允许人工补录的网关偏移边界（秒），超出须先核对校时记录。
OFFSET_BOUND = 3600
HEARTBEAT_PATTERN = (
    r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?'
    r'(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?'
)
LEGACY_COLUMNS = ('batch_id', 'calibration_version', 'offset_lower_seconds', 'offset_upper_seconds')


@dataclass
class ShiftRecords:
    expected: int | None = None
    roster: set[str] = field(default_factory=set)
    # (设备编号, 原始心跳时间, 校时批次)
    heartbeats: list[tuple[str, datetime, str]] = field(default_factory=list)
    # 批次 -> 同批最高版本的校时依据
    calibrations: dict[str, Calibration] = field(default_factory=dict)
    # 批次 -> {版本: 依据}，用于发现同版本冲突与乱序到达
    calibration_versions: dict[str, dict[int, Calibration]] = field(default_factory=lambda: defaultdict(dict))


def normalize_rows(rows: list[dict]) -> list[dict]:
    """补齐旧版 CSV 缺失的校时列。

    旧表头没有校时列时，全部心跳归入默认批次 B1，并视为误差 0 秒、
    版本 1 的单一批次，保证历史数据仍可计算。
    """
    if not rows:
        return rows
    normalized = []
    for row in rows:
        item = dict(row)
        if any(column in item for column in LEGACY_COLUMNS):
            normalized.append(item)
            continue
        item.setdefault('batch_id', DEFAULT_BATCH)
        item.setdefault('calibration_version', 0)
        item.setdefault('offset_lower_seconds', 0)
        item.setdefault('offset_upper_seconds', 0)
        normalized.append(item)
    return normalized


def sample_rows() -> list[dict]:
    rows = []
    # 各分区默认批次的允许偏移区间（整数秒，含端点）。
    zone_ranges = {'东区': (-30, 0), '西区': (0, 30), '南区': (0, 30)}
    for day in ('2026-11-23', '2026-11-24'):
        for zone, prefix in zip(ZONES, ('E', 'W', 'S')):
            def add(kind, inverter='-', heartbeat='-', expected=0,
                    batch='-', version=0, lower=0, upper=0):
                rows.append({'shift_date': day, 'zone': zone, 'kind': kind,
                             'inverter_id': inverter, 'heartbeat_at': heartbeat, 'expected_count': expected,
                             'batch_id': batch, 'calibration_version': version,
                             'offset_lower_seconds': lower, 'offset_upper_seconds': upper})

            add('plan', expected=3)
            lower, upper = zone_ranges[zone]
            add('calibration', batch=DEFAULT_BATCH, version=1, lower=lower, upper=upper)
            incomplete = day == '2026-11-24' and zone == '南区'
            for number in range(1, 3 if incomplete else 4):
                add('roster', f'{prefix}-{number:02}')
            for number, clock in enumerate(('06:45:00', '06:56:12', '06:59:59'), start=1):
                if incomplete and number > 1:
                    continue
                if day == '2026-11-24' and zone == '西区' and number == 3:
                    clock = '06:44:59'
                batch = DEFAULT_BATCH
                if zone == '西区' and number == 3:
                    batch = 'B2'
                add('heartbeat', f'{prefix}-{number:02}', f'{day}T{clock}+08:00', batch=batch)
            # 西区 W-03 单独走 B2 批次，区间更宽，演示同批共用偏移、各批可不同。
            if zone == '西区':
                add('calibration', batch='B2', version=1, lower=-60, upper=60)
            if incomplete:
                add('heartbeat', 'S-99', f'{day}T06:58:31+08:00', batch=DEFAULT_BATCH)
            if day == '2026-11-24' and zone == '东区':
                add('heartbeat', 'E-02', f'{day}T06:56:54+08:00', batch=DEFAULT_BATCH)
                add('heartbeat', 'E-01', f'{day}T07:00:00+08:00', batch=DEFAULT_BATCH)
    return rows


def _local_date(value: str) -> date:
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError('请将晨检日期填写为有效的 YYYY-MM-DD。')


def _local_heartbeat(value: str, day: date) -> datetime:
    if isinstance(value, str) and re.fullmatch(HEARTBEAT_PATTERN, value):
        try:
            timestamp = datetime.fromisoformat(value)
            local = timestamp.replace(tzinfo=SHANGHAI) if timestamp.tzinfo is None else timestamp.astimezone(SHANGHAI)
        except (ValueError, OverflowError):
            pass
        else:
            if local.date() != day:
                raise ValueError('心跳换算为上海时间后与晨检日期不同，请核对日期和 UTC 偏移。')
            return local
    raise ValueError('请将心跳时间填为 ISO 8601 日期和时间，例如 2026-11-24T06:59:30+08:00。')


def _read_calibration(row) -> tuple[str, Calibration]:
    batch = row['batch_id']
    if not isinstance(batch, str) or not batch.strip() or batch == '-':
        raise ValueError('校时记录须填写非空的校时批次。')
    version, lower, upper = row['calibration_version'], row['offset_lower_seconds'], row['offset_upper_seconds']
    for name, value in (('校时版本', version), ('偏移下限', lower), ('偏移上限', upper)):
        if type(value) is not int:
            raise ValueError(f'{name}必须填整数。')
    if version < 1:
        raise ValueError('校时版本从 1 开始递增填写。')
    if not -OFFSET_BOUND <= lower <= upper <= OFFSET_BOUND:
        raise ValueError(f'偏移上下限须为 {-OFFSET_BOUND} 至 {OFFSET_BOUND} 秒，且下限不大于上限。')
    return batch, Calibration(batch=batch, version=version, lower=lower, upper=upper)


def _read_shifts(rows: list[dict]) -> dict[tuple[date, str], ShiftRecords]:
    rows = normalize_rows(rows)
    if not rows:
        raise ValueError('请导入至少一条分区晨检记录，按班次提供计划、名册、校时和心跳。')

    shifts = defaultdict(ShiftRecords)
    for line, row in enumerate(rows, start=2):
        try:
            day = _local_date(row['shift_date'])
            zone, kind = row['zone'], row['kind']
            inverter, timestamp, expected = row['inverter_id'], row['heartbeat_at'], row['expected_count']
            if zone not in ZONES:
                raise ValueError('请将区域改为东区、西区或南区。')
            if type(expected) is not int:
                raise ValueError('计划应检台数必须填整数。')
            shift = shifts[day, zone]
            if kind == 'plan':
                if inverter != '-' or timestamp != '-' or not 1 <= expected <= 5000:
                    raise ValueError('计划行的设备和心跳填 -，应检台数填 1 至 5000。')
                if shift.expected is not None and shift.expected != expected:
                    raise ValueError('同一分区晨班出现不同的应检台数，请按签核计划修正。')
                shift.expected = expected
            elif kind == 'calibration':
                if inverter != '-' or timestamp != '-' or expected != 0:
                    raise ValueError('校时行的设备和心跳填 -，应检台数填 0。')
                batch, record = _read_calibration(row)
                previous = shift.calibration_versions[batch].get(record.version)
                if previous is not None and (previous.lower, previous.upper) != (record.lower, record.upper):
                    raise ValueError(f'批次 {batch} 的校时版本 {record.version} 出现冲突的误差区间，请核对上游记录。')
                shift.calibration_versions[batch][record.version] = record
                current = shift.calibrations.get(batch)
                if current is None or record.version > current.version:
                    shift.calibrations[batch] = record
            elif kind in ('roster', 'heartbeat'):
                if not isinstance(inverter, str) or not inverter.strip() or inverter == '-' or expected != 0:
                    raise ValueError('名册和心跳行须填设备编号，应检台数填 0。')
                if kind == 'roster':
                    if timestamp != '-':
                        raise ValueError('名册行的心跳时间请填 -。')
                    shift.roster.add(inverter)
                else:
                    batch = row['batch_id']
                    if not isinstance(batch, str) or not batch.strip() or batch == '-':
                        raise ValueError('心跳行须填写校时批次，与同批校时记录对应。')
                    shift.heartbeats.append((inverter, _local_heartbeat(timestamp, day), batch))
            else:
                raise ValueError('请将记录类型改为 plan、roster、calibration 或 heartbeat。')
        except KeyError as error:
            raise ValueError(f'CSV 第 {line} 行缺少字段 {error.args[0]}，请使用示例表头补齐。') from error
        except ValueError as error:
            raise ValueError(f'CSV 第 {line} 行：{error}') from error

    for (day, zone), shift in shifts.items():
        if shift.heartbeats and not shift.calibrations:
            # 旧版 CSV 兼容：没有校时行时按零偏移默认批次处理，唯一组合即零偏移。
            record = Calibration(batch=DEFAULT_BATCH, version=DEFAULT_VERSION, lower=0, upper=0)
            shift.calibrations[DEFAULT_BATCH] = record
            shift.calibration_versions[DEFAULT_BATCH][DEFAULT_VERSION] = record
        missing = sorted({batch for _, _, batch in shift.heartbeats if batch not in shift.calibrations})
        if missing:
            raise ValueError(
                f'{day} {zone} 的心跳引用了未登记校时依据的批次 {"、".join(missing)}，请补录 calibration 行。'
            )
    return shifts


def _window_end(options: dict) -> time:
    value = options.get('cutoff', '07:00')
    if isinstance(value, str) and re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        cutoff = time.fromisoformat(value)
        if SHIFT_START < cutoff <= NOON:
            return cutoff
    raise ValueError('请将上海晨检截点填写为 06:01 至 12:00 间的 HH:MM。')


def _latest_heartbeats(heartbeats: list[tuple[str, datetime]], start: datetime, end: datetime):
    per_minute = {}
    duplicates = outside = 0
    for inverter, timestamp in heartbeats:
        if not start <= timestamp < end:
            outside += 1
            continue
        minute = timestamp.replace(second=0, microsecond=0)
        key = inverter, minute
        previous = per_minute.get(key)
        if previous is not None:
            duplicates += 1
        if previous is None or timestamp > previous:
            per_minute[key] = timestamp

    latest = {}
    for (inverter, minute), timestamp in per_minute.items():
        if inverter not in latest or timestamp > latest[inverter][1]:
            latest[inverter] = (minute, timestamp)
    return latest, duplicates, outside


def _format_offset(value: int) -> str:
    return f'{value:+d}s'


def _format_combo(combo: dict[str, int]) -> str:
    return '，'.join(f'{batch}={_format_offset(offset)}' for batch, offset in sorted(combo.items()))


def _basis_rows(calibrations: dict[str, Calibration]) -> list[dict]:
    return [{'校时批次': batch, '校时版本': record.version,
             '允许偏移下限（秒）': record.lower, '允许偏移上限（秒）': record.upper}
            for batch, record in sorted(calibrations.items())]


def _robust_eval(shift: ShiftRecords, start: datetime, end: datetime, freshness: int):
    """对全部批次偏移组合做门槛推演，返回逐设备结论与汇总见证。"""
    calibrations = shift.calibrations
    batches = sorted(calibrations)
    window = calib.fresh_window(int(start.timestamp()), int(end.timestamp()), freshness * 60)
    per_device = defaultdict(list)
    for inverter, timestamp, batch in shift.heartbeats:
        per_device[inverter].append((batch, int(timestamp.timestamp())))

    affected = []
    global_witness = None
    for inverter in sorted(per_device):
        verdict = calib.evaluate_device(per_device[inverter], calibrations, window)
        merged = None if verdict['witness'] is None else calib.merge_witness(verdict['witness'], batches, calibrations)
        if not verdict['robust'] and global_witness is None and merged is not None:
            global_witness = merged
        affected.append({'逆变器': inverter, '稳健': verdict['robust'],
                         '失败组合数': verdict['failing_combinations'],
                         '见证偏移组合': _format_combo(merged) if merged is not None else None,
                         '批次': sorted({batch for batch, _ in per_device[inverter]})})
    return batches, affected, global_witness


def compute(rows: list[dict], options: dict) -> dict:
    zone = options.get('zone', ZONES[0])
    if zone not in ZONES:
        raise ValueError('请选择东区、西区或南区作为交班报告范围。')
    freshness = options.get('freshness_minutes', 15)
    if type(freshness) is not int or not 1 <= freshness <= 120:
        raise ValueError('新鲜度上限请填写 1 至 120 的整数分钟。')
    robust_enabled = options.get('robust_check', '启用')
    if robust_enabled not in ('启用', '关闭'):
        raise ValueError('全偏移组合门槛只能选择启用或关闭。')
    cutoff = _window_end(options)
    shifts = _read_shifts(rows)
    requested_date = options.get('shift_date', '最新')
    day = max(record_day for record_day, _ in shifts) if requested_date == '最新' else _local_date(requested_date)
    shift = shifts.get((day, zone), ShiftRecords())
    start = datetime.combine(day, SHIFT_START, SHANGHAI)
    end = datetime.combine(day, cutoff, SHANGHAI)
    observed = [(inverter, timestamp) for inverter, timestamp, _ in shift.heartbeats]
    latest, duplicates, outside = _latest_heartbeats(observed, start, end)

    # 全偏移组合推演（含窗口外心跳：修正后可能重新进入窗口）。
    batches, robust_results, global_witness = (
        _robust_eval(shift, start, end, freshness) if robust_enabled == '启用' and shift.calibrations
        else ([], [], None)
    )
    robust_by_device = {item['逆变器']: item for item in robust_results}
    robust_failures = [item for item in robust_results if not item['稳健']]

    counts = {'新鲜': 0, '过期': 0, '缺心跳': 0, '名册外': 0}
    details = []
    for inverter in sorted(shift.roster | {inverter for inverter, _, _ in shift.heartbeats}):
        minute, timestamp = latest.get(inverter, (None, None))
        age = int((end - minute).total_seconds() // 60) if minute is not None else None
        if inverter not in shift.roster:
            status = '名册外'
        elif age is None:
            status = '缺心跳'
        elif age > freshness:
            status = '过期'
        else:
            status = '新鲜'
        counts[status] += 1
        device_batches = sorted({batch for name, _, batch in shift.heartbeats if name == inverter})
        basis = [shift.calibrations[batch] for batch in device_batches if batch in shift.calibrations]
        if robust_enabled != '启用':
            robust_status, witness_text, failing_count = '未启用', None, None
        elif inverter not in robust_by_device:
            robust_status, witness_text, failing_count = '缺心跳', None, None
        else:
            item = robust_by_device[inverter]
            robust_status = '稳健' if item['稳健'] else '存在失败组合'
            witness_text, failing_count = item['见证偏移组合'], item['失败组合数']
        details.append({'逆变器': inverter, '心跳状态': status,
                        '最后心跳（上海）': timestamp.isoformat() if timestamp else None,
                        '心跳归属分钟（上海）': minute.isoformat(timespec='minutes') if minute else None,
                        '距截点分钟': age,
                        '校时批次': '、'.join(device_batches) if device_batches else None,
                        '校时版本': '、'.join(str(record.version) for record in basis) if basis else None,
                        '允许偏移区间（秒）': '、'.join(f'{record.batch}[{record.lower},{record.upper}]' for record in basis) if basis else None,
                        '全偏移结论': robust_status,
                        '失败组合数': failing_count,
                        '见证偏移组合': witness_text})

    blockers = []
    if shift.expected is None:
        blockers.append('缺应检计划')
    elif len(shift.roster) != shift.expected:
        blockers.append(f'名册 {len(shift.roster)}/{shift.expected} 台')
    if counts['缺心跳']:
        blockers.append(f"{counts['缺心跳']} 台缺心跳")
    if counts['过期']:
        blockers.append(f"{counts['过期']} 台心跳过期")
    if robust_failures:
        names = '、'.join(item['逆变器'] for item in robust_failures)
        blockers.append(f'{len(robust_failures)} 台在允许偏移下会失败（{names}）')
    reason = '、'.join(blockers)
    gate = f'拦截 · {reason}' if blockers else '可发布'
    if not details:
        details.append({'逆变器': None, '心跳状态': '无设备明细', '最后心跳（上海）': None,
                        '心跳归属分钟（上海）': None, '距截点分钟': None, '校时批次': None,
                        '校时版本': None, '允许偏移区间（秒）': None, '全偏移结论': '无设备',
                        '失败组合数': None, '见证偏移组合': None})
    report_rows = [
        {'晨检日期': day.isoformat(), '区域': zone, '发布门禁': gate,
         '计划应检台数': shift.expected, '去重名册台数': len(shift.roster),
         '截点（上海）': end.isoformat(timespec='minutes'), **row}
        for row in details
    ]
    current_version = max((record.version for record in shift.calibrations.values()), default=None)
    metrics = {'计划应检台数': shift.expected if shift.expected is not None else '未填报',
               '去重名册台数': len(shift.roster), '新鲜台数': counts['新鲜'], '过期台数': counts['过期'],
               '缺心跳台数': counts['缺心跳'], '名册外设备数': counts['名册外'],
               '同分钟去重条数': duplicates, '窗口外心跳条数': outside,
               '校时批次数': len(shift.calibrations), '当前校时版本': current_version if current_version is not None else '未填报',
               '全偏移稳健台数': sum(1 for item in robust_results if item['稳健']),
               '全偏移失败台数': len(robust_failures)}
    calibration_report = {
        '门槛模式': '全偏移组合' if robust_enabled == '启用' else '已关闭',
        '结论': ('全部允许的偏移组合均满足门槛' if not robust_failures and robust_enabled == '启用'
                  else '存在失败偏移组合' if robust_failures else '未启用全偏移组合推演'),
        '校时依据': _basis_rows(shift.calibrations),
        '受影响设备': [{'逆变器': item['逆变器'], '失败组合数': item['失败组合数'],
                            '见证偏移组合': item['见证偏移组合']} for item in robust_failures],
        '失败偏移组合': _format_combo(global_witness) if global_witness else None,
    }
    notes = [
        f'截点按 Asia/Shanghai 解释。窗口从 06:00 含起点，到 {cutoff:%H:%M} 不含截点。',
        '同一班次、区域、设备和本地分钟只留最晚一条。截点减最后心跳归属分钟不超过 '
        f'{freshness} 分钟即为新鲜。',
        '网关校时按批次补录：同批心跳共用一个整数秒偏移，各批可在各自区间内取不同偏移；'
        '修正时间 = 心跳时间 + 批次偏移。',
        '所有允许的批次偏移组合下，每台应检设备都仍有窗口内新鲜心跳，全偏移门槛才通过；'
        '未通过时明细逐台给出会失败的一组偏移见证和受影响设备。',
        '班前签核计划给出应检台数，名册提供逐台预期设备。名册外心跳单列，无法补齐名册或替代应检设备。',
        '每个分区晨班独立复核并生成报告。上游校时版本更新后待发报告立即失效，须重新复核；已送达报告保留当时依据。',
    ]
    if blockers:
        notes.append('请向值班长核对应检计划、设备名册和批次校时依据，补传窗口内的有效心跳后重新计算。')
    headline = f'{day.isoformat()} {zone}晨班｜拦截：{reason}' if blockers else (
        f"{day.isoformat()} {zone}晨班｜{counts['新鲜']} 台应检逆变器齐备，全部偏移组合下心跳新鲜，可发布"
    )
    return {'headline': headline, 'can_publish': not blockers, 'metrics': metrics, 'rows': report_rows,
            'notes': notes, 'calibration_basis': _basis_rows(shift.calibrations),
            'calibration_version': current_version, 'calibration_report': calibration_report,
            'dispatch_recipient': f'{zone}检修接班组',
            'chart': {'label': f'{zone}晨班应检设备心跳状态', 'unit': '台',
                      'points': [{'label': status, 'value': counts[status]}
                                 for status in ('新鲜', '过期', '缺心跳')]}}

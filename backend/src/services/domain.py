import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo('Asia/Shanghai')
ZONES = ('东区', '西区', '南区')
SHIFT_START = time(6)
NOON = time(12)
HEARTBEAT_PATTERN = (
    r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?'
    r'(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?'
)


@dataclass
class ShiftRecords:
    expected: int | None = None
    roster: set[str] = field(default_factory=set)
    heartbeats: list[tuple[str, datetime]] = field(default_factory=list)


def sample_rows() -> list[dict]:
    rows = []
    for day in ('2026-11-23', '2026-11-24'):
        for zone, prefix in zip(ZONES, ('E', 'W', 'S')):
            def add(kind, inverter='-', heartbeat='-', expected=0):
                rows.append({'shift_date': day, 'zone': zone, 'kind': kind,
                             'inverter_id': inverter, 'heartbeat_at': heartbeat, 'expected_count': expected})

            add('plan', expected=3)
            incomplete = day == '2026-11-24' and zone == '南区'
            for number in range(1, 3 if incomplete else 4):
                add('roster', f'{prefix}-{number:02}')
            for number, clock in enumerate(('06:45:00', '06:56:12', '06:59:59'), start=1):
                if incomplete and number > 1:
                    continue
                if day == '2026-11-24' and zone == '西区' and number == 3:
                    clock = '06:44:59'
                add('heartbeat', f'{prefix}-{number:02}', f'{day}T{clock}+08:00')
            if incomplete:
                add('heartbeat', 'S-99', f'{day}T06:58:31+08:00')
            if day == '2026-11-24' and zone == '东区':
                add('heartbeat', 'E-02', f'{day}T06:56:54+08:00')
                add('heartbeat', 'E-01', f'{day}T07:00:00+08:00')
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


def _read_shifts(rows: list[dict]) -> dict[tuple[date, str], ShiftRecords]:
    if not rows:
        raise ValueError('请导入至少一条分区晨检记录，按班次提供计划、名册和心跳。')

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
            elif kind in ('roster', 'heartbeat'):
                if not isinstance(inverter, str) or not inverter.strip() or inverter == '-' or expected != 0:
                    raise ValueError('名册和心跳行须填设备编号，应检台数填 0。')
                if kind == 'roster':
                    if timestamp != '-':
                        raise ValueError('名册行的心跳时间请填 -。')
                    shift.roster.add(inverter)
                else:
                    shift.heartbeats.append((inverter, _local_heartbeat(timestamp, day)))
            else:
                raise ValueError('请将记录类型改为 plan、roster 或 heartbeat。')
        except KeyError as error:
            raise ValueError(f'CSV 第 {line} 行缺少字段 {error.args[0]}，请使用示例表头补齐。') from error
        except ValueError as error:
            raise ValueError(f'CSV 第 {line} 行：{error}') from error
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


def compute(rows: list[dict], options: dict) -> dict:
    zone = options.get('zone', ZONES[0])
    if zone not in ZONES:
        raise ValueError('请选择东区、西区或南区作为交班报告范围。')
    freshness = options.get('freshness_minutes', 15)
    if type(freshness) is not int or not 1 <= freshness <= 120:
        raise ValueError('新鲜度上限请填写 1 至 120 的整数分钟。')
    cutoff = _window_end(options)
    shifts = _read_shifts(rows)
    requested_date = options.get('shift_date', '最新')
    day = max(record_day for record_day, _ in shifts) if requested_date == '最新' else _local_date(requested_date)
    shift = shifts.get((day, zone), ShiftRecords())
    start = datetime.combine(day, SHIFT_START, SHANGHAI)
    end = datetime.combine(day, cutoff, SHANGHAI)
    latest, duplicates, outside = _latest_heartbeats(shift.heartbeats, start, end)

    counts = {'新鲜': 0, '过期': 0, '缺心跳': 0, '名册外': 0}
    details = []
    for inverter in sorted(shift.roster | latest.keys()):
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
        details.append({'逆变器': inverter, '心跳状态': status,
                        '最后心跳（上海）': timestamp.isoformat() if timestamp else None,
                        '心跳归属分钟（上海）': minute.isoformat(timespec='minutes') if minute else None,
                        '距截点分钟': age})

    blockers = []
    if shift.expected is None:
        blockers.append('缺应检计划')
    elif len(shift.roster) != shift.expected:
        blockers.append(f'名册 {len(shift.roster)}/{shift.expected} 台')
    if counts['缺心跳']:
        blockers.append(f"{counts['缺心跳']} 台缺心跳")
    if counts['过期']:
        blockers.append(f"{counts['过期']} 台心跳过期")
    reason = '、'.join(blockers)
    gate = f'拦截 · {reason}' if blockers else '可发布'
    if not details:
        details.append({'逆变器': None, '心跳状态': '无设备明细', '最后心跳（上海）': None,
                        '心跳归属分钟（上海）': None, '距截点分钟': None})
    report_rows = [
        {'晨检日期': day.isoformat(), '区域': zone, '发布门禁': gate,
         '计划应检台数': shift.expected, '去重名册台数': len(shift.roster),
         '截点（上海）': end.isoformat(timespec='minutes'), **row}
        for row in details
    ]
    metrics = {'计划应检台数': shift.expected if shift.expected is not None else '未填报',
               '去重名册台数': len(shift.roster), '新鲜台数': counts['新鲜'], '过期台数': counts['过期'],
               '缺心跳台数': counts['缺心跳'], '名册外设备数': counts['名册外'],
               '同分钟去重条数': duplicates, '窗口外心跳条数': outside}
    notes = [
        f'截点按 Asia/Shanghai 解释。窗口从 06:00 含起点，到 {cutoff:%H:%M} 不含截点。',
        f'同一班次、区域、设备和本地分钟只留最晚一条。截点减最后心跳归属分钟不超过 {freshness} 分钟即为新鲜。',
        '班前签核计划给出应检台数，名册提供逐台预期设备。名册外心跳单列，无法补齐名册或替代应检设备。',
        '每个分区晨班独立复核并生成报告。批准后在通用分发页为对应值班组建立单次投递。',
    ]
    if blockers:
        notes.append('请向值班长核对应检计划和设备名册，补传窗口内的有效心跳后重新计算。')
    headline = f'{day.isoformat()} {zone}晨班｜拦截：{reason}' if blockers else (
        f"{day.isoformat()} {zone}晨班｜{counts['新鲜']} 台应检逆变器齐备且心跳新鲜，可发布"
    )
    return {'headline': headline, 'can_publish': not blockers, 'metrics': metrics, 'rows': report_rows,
            'notes': notes, 'dispatch_recipient': f'{zone}检修接班组', 'chart': {'label': f'{zone}晨班应检设备心跳状态', 'unit': '台',
                                     'points': [{'label': status, 'value': counts[status]}
                                                for status in ('新鲜', '过期', '缺心跳')]}}

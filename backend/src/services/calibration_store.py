"""网关校时版本的登记、查询与失效治理。

设计约束（对应业务口径）：
- 校时记录只追加，乱序到达的旧版本保留审计，不覆盖当前版本；
- 上游校时版本更新后，引用旧依据的待发报告立即失效（``calibration_stale``
  只能 0 → 1，任何并发重算都不能把它改回 0）；
- 已送达报告在 ``report_deliveries.calibration_version`` 保留当时依据；
- 重新复核产生的是新的分析任务与新的批准，旧任务不会恢复投递资格。
"""
import time

from fastapi import HTTPException
from sqlalchemy import insert, select, update

from src.models.database import add_event, engine
from src.models.tables import clock_calibrations, runs, schedules


def sync_calibrations(connection, dataset_id: int, records: list[dict], owner: str, now: float):
    """把数据版本内的 calibration 行登记到校时表。

    同一 (数据版本, 日期, 分区, 批次, 版本) 重复导入时跳过；区间冲突由
    CSV 解析阶段拦截，这里靠唯一约束兜底。
    """
    seen = set()
    payloads = []
    for row in records:
        if row.get('kind') != 'calibration':
            continue
        key = (row['shift_date'], row['zone'], row['batch_id'], row['calibration_version'])
        if key in seen:
            continue
        seen.add(key)
        payloads.append({
            'dataset_id': dataset_id, 'shift_date': row['shift_date'], 'zone': row['zone'],
            'batch_id': row['batch_id'], 'calibration_version': row['calibration_version'],
            'offset_lower_seconds': row['offset_lower_seconds'],
            'offset_upper_seconds': row['offset_upper_seconds'], 'created': now, 'owner': owner,
        })
    for payload in payloads:
        existing = connection.execute(
            select(clock_calibrations.c.id).where(
                clock_calibrations.c.dataset_id == payload['dataset_id'],
                clock_calibrations.c.shift_date == payload['shift_date'],
                clock_calibrations.c.zone == payload['zone'],
                clock_calibrations.c.batch_id == payload['batch_id'],
                clock_calibrations.c.calibration_version == payload['calibration_version'],
            )
        ).scalar()
        if existing is None:
            connection.execute(insert(clock_calibrations).values(**payload))


def list_calibrations(dataset_id: int | None = None):
    statement = select(clock_calibrations).order_by(
        clock_calibrations.c.dataset_id.desc(), clock_calibrations.c.shift_date,
        clock_calibrations.c.zone, clock_calibrations.c.batch_id, clock_calibrations.c.calibration_version)
    if dataset_id is not None:
        statement = statement.where(clock_calibrations.c.dataset_id == dataset_id)
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(statement.limit(500)).mappings()]

    # 当前版本 = 同（晨检日期, 分区, 批次）下的最大版本，跨数据版本比较，
    # 与失效判断口径保持一致；旧版本明确标注，前端可区分依据新旧。
    current = {}
    for row in rows:
        key = (row['shift_date'], row['zone'], row['batch_id'])
        current[key] = max(current.get(key, 0), row['calibration_version'])
    for row in rows:
        key = (row['shift_date'], row['zone'], row['batch_id'])
        row['is_current'] = int(row['calibration_version'] == current[key])
    return rows


def _current_versions(connection, shift_date: str, zone: str) -> dict[str, int]:
    # 新版本通常随新的数据版本导入，因此这里跨数据版本取最大值，
    # 不能限定报告自身的 dataset_id，否则旧数据集里的报告永远感知不到更新。
    statement = select(clock_calibrations.c.batch_id, clock_calibrations.c.calibration_version).where(
        clock_calibrations.c.shift_date == shift_date,
        clock_calibrations.c.zone == zone,
    )
    latest = {}
    for batch, version in connection.execute(statement).all():
        latest[batch] = max(latest.get(batch, 0), version)
    return latest


def invalidate_stale_runs(connection, actor: str = 'system', now: float | None = None) -> int:
    """把依据版本落后于当前校时版本的待发任务标记失效，并停用其投递计划。

    只做 0 → 1 的条件更新；已失效任务即使收到乱序旧版本也不会被恢复。
    返回失效的任务数。
    """
    now = time.time() if now is None else now
    invalidated = 0
    pending = connection.execute(select(runs).where(
        runs.c.calibration_stale == 0, runs.c.calibration_version.is_not(None)
    )).mappings().all()
    for run in pending:
        result = run['result'] or {}
        shift_date = next((row.get('晨检日期') for row in result.get('rows', []) if row.get('晨检日期')), None)
        zone = next((row.get('区域') for row in result.get('rows', []) if row.get('区域')), None)
        if shift_date is None or zone is None:
            continue
        current = _current_versions(connection, shift_date, zone)
        basis = run['calibration_basis'] or {}
        if any(current.get(batch, 0) > version for batch, version in basis.items()):
            changed = connection.execute(update(runs).where(
                runs.c.id == run['id'], runs.c.calibration_stale == 0
            ).values(calibration_stale=1))
            if changed.rowcount:
                invalidated += 1
                add_event(connection, actor, 'calibration_stale', run['id'], now)
                connection.execute(update(schedules).where(
                    schedules.c.run_id == run['id'], schedules.c.active == 1
                ).values(active=0))
                add_event(connection, actor, 'cancel', run['id'], now)
    return invalidated


def assert_run_current(connection, run) -> None:
    """复核或建立投递前校验报告依据仍是当前校时版本。"""
    if run['calibration_stale'] == 1:
        raise HTTPException(409, '上游校时版本已更新，该报告依据失效，请重新运行分析并复核后再投递。')
    result = run['result'] or {}
    shift_date = next((row.get('晨检日期') for row in result.get('rows', []) if row.get('晨检日期')), None)
    zone = next((row.get('区域') for row in result.get('rows', []) if row.get('区域')), None)
    if shift_date is None or zone is None or not run['calibration_basis']:
        return
    current = _current_versions(connection, shift_date, zone)
    basis = run['calibration_basis'] or {}
    if any(current.get(batch, 0) > version for batch, version in basis.items()):
        # 并发重算窗口内的兜底：立即落失效并拒绝本次操作。
        connection.execute(update(runs).where(runs.c.id == run['id'], runs.c.calibration_stale == 0)
                           .values(calibration_stale=1))
        connection.execute(update(schedules).where(schedules.c.run_id == run['id'], schedules.c.active == 1)
                           .values(active=0))
        raise HTTPException(409, '校时依据刚被新版本取代，报告已失效，请重新复核。')


def basis_from_result(result: dict) -> dict[str, int]:
    """从计算结果提取 {批次: 版本} 依据。"""
    return {str(row['校时批次']): int(row['校时版本']) for row in result.get('calibration_basis', [])}

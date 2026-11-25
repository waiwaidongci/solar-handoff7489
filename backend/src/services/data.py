import csv
import hashlib
import io
import json
import math
import time
from pathlib import Path

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from src.models.database import add_event, engine
from src.models.tables import datasets
from src.services.calibration_store import invalidate_stale_runs, sync_calibrations
from src.services.domain import normalize_rows

PROFILE = json.loads(Path(__file__).with_name('domain.json').read_text(encoding='utf-8'))


def convert(value, field):
    if value is None or isinstance(value, bool) or str(value).strip() == '':
        raise ValueError('不能为空')
    kind = field['type']
    if kind == 'str':
        # 校时批次列允许占位符 -：plan/roster 行不涉及批次，
        # 真正的非空约束由 domain 层按记录类型校验。
        if field['key'] == 'batch_id' and str(value).strip() == '-':
            return '-'
        return str(value).strip()
    if kind == 'select':
        if value not in field['choices']:
            raise ValueError('请选择有效选项')
        return value
    number = int(str(value)) if kind == 'int' else float(value)
    if not math.isfinite(number):
        raise ValueError('必须为有限数值')
    if number < field.get('min', -math.inf) or number > field.get('max', math.inf):
        raise ValueError('数值超出可接受范围')
    return number


def csv_records(text):
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff'), newline=''), strict=True)
    columns = PROFILE['columns']
    try:
        fieldnames = reader.fieldnames
    except csv.Error as error:
        raise ValueError('CSV 表头的引号格式有误。') from error
    required = {field['key'] for field in columns}
    calibration_columns = {'batch_id', 'calibration_version', 'offset_lower_seconds', 'offset_upper_seconds'}
    legacy_required = required - calibration_columns
    if not fieldnames or len(fieldnames) != len(set(fieldnames)) or set(fieldnames) not in (required, legacy_required):
        raise ValueError('CSV 表头须为 ' + '、'.join(field['key'] for field in columns) + '；旧版表头也可兼容导入。')
    legacy = set(fieldnames) == legacy_required
    rows = []
    try:
        for row in reader:
            if None in row:
                raise ValueError(f'第 {reader.line_num} 行列数多于表头。')
            if legacy:
                # 旧版 CSV：心跳统一归入默认批次 B1（误差 0 秒、版本 1）；
                # plan/roster 行的批次列保持占位符 -，符合各自的行规则。
                is_heartbeat = row.get('kind') == 'heartbeat'
                row = {**row, 'batch_id': 'B1' if is_heartbeat else '-',
                       'calibration_version': '0',
                       'offset_lower_seconds': '0', 'offset_upper_seconds': '0'}
            normalized = {}
            for field in columns:
                try:
                    normalized[field['key']] = convert(row[field['key']], field)
                except (ValueError, TypeError, OverflowError) as error:
                    raise ValueError(f"第 {reader.line_num} 行 {field['label']} 无效（{error}）。") from error
            rows.append(normalized)
            if len(rows) > 5000:
                raise ValueError('单份数据最多 5000 行。')
    except csv.Error as error:
        raise ValueError('CSV 引号或字段格式有误。') from error
    if not rows:
        raise ValueError('CSV 需要至少一条数据。')
    return normalize_rows(rows)


def parse_options(values):
    fields = PROFILE['options']
    if set(values) - {field['key'] for field in fields}:
        raise ValueError('存在未定义的分析参数。')
    output = {}
    for field in fields:
        try:
            output[field['key']] = convert(values.get(field['key'], field['default']), field)
        except (ValueError, TypeError, OverflowError) as error:
            raise ValueError(f"{field['label']} 无效（{error}）。") from error
    return output


def save_revision(name, records, owner):
    if not name.strip():
        raise ValueError('数据名称不能为空。')
    payload = json.dumps(records, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    checksum = hashlib.sha256(payload.encode()).hexdigest()
    now = time.time()
    existing = select(datasets.c.id).where(datasets.c.checksum == checksum)
    try:
        with engine.begin() as connection:
            found = connection.scalar(existing)
            if found is not None:
                return found, False
            key = connection.execute(insert(datasets).values(name=name.strip(), owner=owner, records=records,
                                                             checksum=checksum, created=now)).inserted_primary_key[0]
            add_event(connection, owner, 'import', key, now)
            sync_calibrations(connection, key, records, owner, now)
            invalidate_stale_runs(connection, actor=owner, now=now)
            return key, True
    except IntegrityError:
        with engine.connect() as connection:
            found = connection.scalar(existing)
            if found is None:
                raise
            return found, False

"""轻量结构迁移：为既有数据库补齐校时治理列与校时记录表。

项目演示库通过 ``metadata.create_all`` 建表，已存在的表不会自动获得新列。
这里做幂等的 ``ADD COLUMN`` / ``CREATE TABLE``，避免要求现场引入完整迁移
框架。列或表已存在时跳过；类型名词在 MySQL 与 SQLite 下都合法。
"""
from sqlalchemy import inspect, text

from src.models.tables import clock_calibrations, metadata

_RUN_COLUMNS = {
    'calibration_version': 'INT NULL',
    'calibration_basis': 'JSON NULL',
    'calibration_stale': "INT NOT NULL DEFAULT 0",
}
_SCHEDULE_COLUMNS = {'calibration_version': 'INT NULL'}
_DELIVERY_COLUMNS = {'calibration_version': 'INT NULL'}
# 校时表补列时使用通用类型名词，避免依赖方言编译细节。
_CALIBRATION_COLUMN_TYPES = {
    'id': 'INT', 'dataset_id': 'INT', 'shift_date': 'VARCHAR(10)', 'zone': 'VARCHAR(20)',
    'batch_id': 'VARCHAR(40)', 'calibration_version': 'INT', 'offset_lower_seconds': 'INT',
    'offset_upper_seconds': 'INT', 'created': 'FLOAT', 'owner': 'VARCHAR(40)',
}


def _missing_columns(inspector, table_name):
    present = {column['name'] for column in inspector.get_columns(table_name)}
    return present


def run_migrations(engine):
    with engine.begin() as connection:
        inspector = inspect(connection)
        existing_tables = set(inspector.get_table_names())

        for table_name, columns in (
            ('analysis_runs', _RUN_COLUMNS),
            ('report_schedules', _SCHEDULE_COLUMNS),
            ('report_deliveries', _DELIVERY_COLUMNS),
        ):
            if table_name not in existing_tables:
                continue
            present = _missing_columns(inspector, table_name)
            for name, ddl in columns.items():
                if name not in present:
                    connection.execute(text(f'ALTER TABLE {table_name} ADD COLUMN {name} {ddl}'))

        if clock_calibrations.name not in existing_tables:
            clock_calibrations.create(bind=connection)
        else:
            present = _missing_columns(inspector, clock_calibrations.name)
            for column in clock_calibrations.columns:
                if column.name not in present:
                    ddl_type = _CALIBRATION_COLUMN_TYPES.get(column.name, 'VARCHAR(255)')
                    connection.execute(text(
                        f'ALTER TABLE {clock_calibrations.name} ADD COLUMN {column.name} {ddl_type} NULL'
                    ))


def ensure_schema(engine):
    run_migrations(engine)
    metadata.create_all(engine)

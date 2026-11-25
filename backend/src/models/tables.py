from sqlalchemy import JSON, Column, Double, Integer, MetaData, String, Table, Text, UniqueConstraint

metadata = MetaData()
users = Table('workspace_users', metadata, Column('username', String(40), primary_key=True),
              Column('role', String(20), nullable=False), Column('password', String(160), nullable=False))
datasets = Table('data_revisions', metadata, Column('id', Integer, primary_key=True),
                 Column('name', String(180), nullable=False), Column('checksum', String(64), nullable=False, unique=True),
                 Column('records', JSON, nullable=False), Column('owner', String(40), nullable=False),
                 Column('created', Double, nullable=False))
runs = Table('analysis_runs', metadata, Column('id', Integer, primary_key=True),
             Column('dataset_id', Integer, nullable=False, index=True), Column('options', JSON, nullable=False),
             Column('state', String(16), nullable=False, index=True), Column('owner', String(40), nullable=False),
             Column('created', Double, nullable=False), Column('started', Double), Column('finished', Double),
             Column('result', JSON), Column('error', Text), Column('decision', String(16)),
             Column('reviewer', String(40)), Column('review_note', Text), Column('reviewed', Double),
             # 校时版本治理：报告绑定复核时的依据版本，stale=1 后只能重新复核。
             Column('calibration_version', Integer), Column('calibration_basis', JSON),
             Column('calibration_stale', Integer, nullable=False, server_default='0'))
# 网关校时记录只追加：同一数据版本内同分区同批次同版本只允许一条区间。
# 乱序到达的旧版本保留审计但不会成为当前版本，更不能恢复投递资格。
clock_calibrations = Table('clock_calibrations', metadata, Column('id', Integer, primary_key=True),
                           Column('dataset_id', Integer, nullable=False, index=True),
                           Column('shift_date', String(10), nullable=False), Column('zone', String(20), nullable=False),
                           Column('batch_id', String(40), nullable=False), Column('calibration_version', Integer, nullable=False),
                           Column('offset_lower_seconds', Integer, nullable=False),
                           Column('offset_upper_seconds', Integer, nullable=False),
                           Column('created', Double, nullable=False), Column('owner', String(40), nullable=False),
                           UniqueConstraint('dataset_id', 'shift_date', 'zone', 'batch_id', 'calibration_version',
                                            name='uq_calibration_dataset_batch_version'))
schedules = Table('report_schedules', metadata, Column('id', Integer, primary_key=True),
                  Column('run_id', Integer, nullable=False), Column('recipient', String(100), nullable=False),
                  Column('cadence', String(16), nullable=False), Column('next_at', Double, nullable=False, index=True),
                  Column('active', Integer, nullable=False), Column('owner', String(40), nullable=False),
                  Column('calibration_version', Integer))
deliveries = Table('report_deliveries', metadata, Column('id', Integer, primary_key=True),
                   Column('schedule_id', Integer, nullable=False), Column('run_id', Integer, nullable=False),
                   Column('recipient', String(100), nullable=False),
                   Column('scheduled', Double, nullable=False), Column('delivered', Double, nullable=False),
                   # 已送达报告保留当时依据：投递时快照校时版本。
                   Column('calibration_version', Integer),
                   UniqueConstraint('schedule_id', 'scheduled'))
audit = Table('operation_audit', metadata, Column('id', Integer, primary_key=True),
              Column('at', Double, nullable=False), Column('actor', String(40), nullable=False),
              Column('action', String(30), nullable=False), Column('subject', Integer, nullable=False))

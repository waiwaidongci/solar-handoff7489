"""共享的最小 SQLAlchemy / FastAPI 内存桩（仅用于无依赖环境的单测）。

覆盖项目实际用到的 select/insert/update/Table/Column/engine 接口，
让纯业务规则（校时版本治理、CSV 解析）可在没有第三方库的环境中验证。
"""
import sys
import types


class FakeColumn:
    def __init__(self, name, *args, **kwargs):
        self.name = name
        self.table = None

    def __eq__(self, other):
        return FakeCondition(lambda row, name=self.name, other=other: row.get(name) == other)

    def __gt__(self, other):
        return FakeCondition(lambda row, name=self.name, other=other:
                             row.get(name) is not None and row[name] > other)

    def __le__(self, other):
        return FakeCondition(lambda row, name=self.name, other=other:
                             row.get(name) is not None and row[name] <= other)

    def __ge__(self, other):
        return FakeCondition(lambda row, name=self.name, other=other:
                             row.get(name) is not None and row[name] >= other)

    def is_(self, other):
        return FakeCondition(lambda row, name=self.name: row.get(name) is other)

    def is_not(self, other):
        return FakeCondition(lambda row, name=self.name: row.get(name) is not other)

    def desc(self):
        return ('desc', self.name)


class FakeCondition:
    def __init__(self, eval_fn):
        self.eval = eval_fn


class FakeTable:
    def __init__(self, name, metadata, *columns, **kwargs):
        self.name = name
        self.columns = [c for c in columns if isinstance(c, FakeColumn)]
        self.c = types.SimpleNamespace(**{c.name: _bound(c, self) for c in self.columns})

    def create(self, bind=None):
        bind.data.setdefault(self.name, [])


def _bound(column, table):
    clone = FakeColumn(column.name)
    clone.table = table
    return clone


class FakeInsert:
    def __init__(self, table):
        self.table = table
        self.values_kwargs = {}

    def values(self, **kwargs):
        self.values_kwargs = kwargs
        return self


class FakeUpdate:
    def __init__(self, table):
        self.table = table
        self.filters = []
        self.values_kwargs = {}
        self.rowcount = 0

    def where(self, *conditions):
        self.filters.extend(conditions)
        return self

    def values(self, **kwargs):
        self.values_kwargs = kwargs
        return self


class FakeSelect:
    def __init__(self, *targets):
        self.targets = targets
        self.filters = []
        self.limit_n = None

    def where(self, *conditions):
        self.filters.extend(conditions)
        return self

    def order_by(self, *items):
        return self

    def limit(self, n):
        self.limit_n = n
        return self


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return [list(r) if isinstance(r, list) else dict(r) for r in self.rows]

    def one_or_none(self):
        return dict(self.rows[0]) if self.rows else None

    def scalar(self):
        return self.rows[0][0] if self.rows else None

    def __iter__(self):
        return iter(self.all())


class FakeConnection:
    def __init__(self, data):
        self.data = data

    def execute(self, statement):
        if isinstance(statement, FakeInsert):
            rows = self.data.setdefault(statement.table.name, [])
            row = dict(statement.values_kwargs)
            row.setdefault('id', max((r.get('id', 0) for r in rows), default=0) + 1)
            rows.append(row)
            return types.SimpleNamespace(inserted_primary_key=[row['id']])
        if isinstance(statement, FakeUpdate):
            changed = 0
            for row in self.data.setdefault(statement.table.name, []):
                if all(condition.eval(row) for condition in statement.filters):
                    row.update(statement.values_kwargs)
                    changed += 1
            statement.rowcount = changed
            return statement
        if isinstance(statement, FakeSelect):
            target = statement.targets[0]
            table = getattr(target, 'table', target)
            rows = [dict(r) for r in self.data.get(table.name, [])]
            rows = [r for r in rows if all(condition.eval(r) for condition in statement.filters)]
            if all(isinstance(target, FakeColumn) for target in statement.targets) and len(statement.targets) >= 1:
                projected = [[r[target.name] for target in statement.targets] for r in rows]
                rows = projected if len(statement.targets) > 1 else [[item[0]] for item in projected]
            if statement.limit_n is not None:
                rows = rows[:statement.limit_n]
            return FakeResult(rows)
        raise AssertionError(f'未支持的语句: {statement!r}')

    def scalar(self, statement):
        return self.execute(statement).scalar()


class _Ctx:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *exc):
        return False


class FakeEngine:
    def __init__(self):
        self.data = {}

    def begin(self):
        return _Ctx(FakeConnection(self.data))

    def connect(self):
        return _Ctx(FakeConnection(self.data))


ENGINE = FakeEngine()


def install():
    if 'sqlalchemy' in sys.modules and getattr(sys.modules['sqlalchemy'], '_is_gb0077_stub', False):
        return
    sa = types.ModuleType('sqlalchemy')
    sa._is_gb0077_stub = True
    for name in ('JSON', 'Double', 'Integer', 'String', 'Text'):
        setattr(sa, name, type(name, (), {'__init__': lambda self, *a, **k: None}))
    sa.Column = FakeColumn
    sa.Table = FakeTable
    sa.MetaData = type('MetaData', (), {'__init__': lambda self: None})
    sa.UniqueConstraint = lambda *a, **k: None
    sa.select = lambda *targets: FakeSelect(*targets)
    sa.insert = lambda table: FakeInsert(table)
    sa.update = lambda table: FakeUpdate(table)
    sa.text = lambda sql: sql
    sa.and_ = lambda *conditions: FakeCondition(
        lambda row, conditions=conditions: all(c.eval(row) for c in conditions))
    sa.or_ = lambda *conditions: FakeCondition(
        lambda row, conditions=conditions: any(c.eval(row) for c in conditions))
    sa.inspect = lambda connection: types.SimpleNamespace(
        get_table_names=lambda: list(connection.data),
        get_columns=lambda name: [{'name': c} for c in connection.data.get('__columns__', {}).get(name, [])])
    sys.modules['sqlalchemy'] = sa
    exc = types.ModuleType('sqlalchemy.exc')
    exc.IntegrityError = type('IntegrityError', (Exception,), {})
    sys.modules['sqlalchemy.exc'] = exc

    database = types.ModuleType('src.models.database')
    database.engine = ENGINE
    database.fetch_one = lambda connection, table, key: next(
        (dict(r) for r in connection.data.get(table.name, []) if r.get('id') == key), None)
    database.add_event = lambda connection, actor, action, subject, at: connection.data.setdefault(
        'operation_audit', []).append({'actor': actor, 'action': action, 'subject': subject, 'at': at})
    sys.modules['src.models.database'] = database

    fastapi = types.ModuleType('fastapi')

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.HTTPException = HTTPException
    sys.modules['fastapi'] = fastapi


def fresh_connection():
    return FakeConnection(ENGINE.data)


def reset_data():
    ENGINE.data.clear()

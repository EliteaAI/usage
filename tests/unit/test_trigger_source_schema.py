"""usage_event.trigger_source: the ALTER that deployed tables need (#6881).

The column shipped without a migration, so on a table that predates it every drained insert
fails with UndefinedColumn and the ledger records nothing. Mirrors the budget_exempt migration.
"""
import types

from fixtures.helpers import bind, fake_module
from usage.methods import admin_tasks, schema
from usage.models.usage_event import UsageEvent


class Connection:
    def __init__(self):
        self.statements = []
        self.commits = 0

    def execute(self, statement, params=None):  # pylint: disable=W0613
        self.statements.append(str(statement))

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_the_alter_is_idempotent_and_nullable(monkeypatch):
    connection = Connection()
    monkeypatch.setattr(schema, "db", types.SimpleNamespace(
        engine=types.SimpleNamespace(connect=lambda: connection),
    ))
    instance = fake_module()
    bind(instance, schema.Method)
    #
    instance.usage_ensure_trigger_source_column()
    #
    assert len(connection.statements) == 1
    assert "ADD COLUMN IF NOT EXISTS trigger_source VARCHAR(16)" in connection.statements[0]
    assert "NOT NULL" not in connection.statements[0]
    assert "DEFAULT" not in connection.statements[0]  # NULL must keep meaning "manual"
    assert connection.commits == 1


def test_the_admin_task_runs_the_alter():
    instance = fake_module()
    bind(instance, admin_tasks.Method)
    calls = []
    instance.usage_ensure_trigger_source_column = lambda: calls.append(1)
    #
    instance.usage_ensure_trigger_source_column_task()
    #
    assert calls == [1]


def test_the_model_column_matches_the_migration():
    column = UsageEvent.__table__.columns[schema.TRIGGER_SOURCE_COLUMN]
    #
    assert column.nullable is True
    assert column.server_default is None
    assert column.type.length == 16

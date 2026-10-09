import pytest
from sqlalchemy.dialects import postgresql

from fixtures.helpers import bind, fake_module
from usage.methods import conversation_totals, mode

PAGE = ["c1", "c2"]


def _sql(statement):
    compiled = statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    return str(compiled).replace("centry.", "")


class Connection:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []

    def execute(self, statement):
        self.statements.append(_sql(statement))
        return iter(self.results.pop(0) if self.results else [])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Engine:
    def __init__(self, *results):
        self.connection = Connection(results)

    def connect(self):
        return self.connection


class FailingEngine:
    def connect(self):
        raise RuntimeError("no database")


@pytest.fixture
def build(monkeypatch):
    def make(engine, usage_mode="observe"):
        monkeypatch.setattr(conversation_totals.db, "engine", engine, raising=False)
        instance = bind(fake_module({"usage": {"mode": usage_mode}}), mode.Method, conversation_totals.Method)
        return instance, engine
    return make


ROW = ("c1", 120, 2_500_000_000, ["opus", "haiku"], True, "run-9")


class TestConversationTotals:
    def test_rows_are_keyed_by_conversation(self, build):
        instance, _ = build(Engine([ROW]))

        totals = instance.usage_read_conversation_totals(2, PAGE, "skill", 174, 2)

        assert totals == {"c1": {
            "tokens": 120, "cost": 2.5, "models": ["haiku", "opus"], "has_error": True,
            "last_run_id": "run-9",
        }}

    def test_one_statement_for_a_page(self, build):
        instance, engine = build(Engine([ROW]))

        instance.usage_read_conversation_totals(2, PAGE, "skill", 174, 2)

        assert len(engine.connection.statements) == 1

    def test_only_metered_rows_of_the_skill_are_counted(self, build):
        instance, engine = build(Engine([]))

        instance.usage_read_conversation_totals(2, PAGE, "skill", 174, 1)

        sql = engine.connection.statements[0]
        assert "usage_event.event_type IN ('llm', 'tool')" in sql
        assert "usage_event.root_entity_type = 'skill'" in sql
        assert "usage_event.root_entity_id = 174" in sql
        assert "coalesce(usage_event.root_entity_project_id, usage_event.project_id) = 1" in sql
        assert "usage_event.conversation_id IN ('c1', 'c2')" in sql
        assert "GROUP BY usage_event.conversation_id" in sql
        assert "HAVING" not in sql

    def test_model_filter_keeps_conversations_that_model_answered_in(self, build):
        instance, engine = build(Engine([]))

        instance.usage_read_conversation_totals(2, PAGE, "skill", 174, 2, model_name="opus")

        assert "HAVING bool_or(usage_event.model_name = 'opus')" in engine.connection.statements[0]

    def test_latest_run_is_taken_by_time(self, build):
        instance, engine = build(Engine([]))

        instance.usage_read_conversation_totals(2, PAGE, "skill", 174, 2)

        assert "array_agg(usage_event.run_id ORDER BY usage_event.ts DESC))[1]" in engine.connection.statements[0]

    def test_large_candidate_lists_are_chunked(self, build, monkeypatch):
        monkeypatch.setattr(conversation_totals, "ID_CHUNK", 2)
        instance, engine = build(Engine([], []))

        instance.usage_read_conversation_totals(2, ["a", "b", "c"], "skill", 174, 2)

        assert len(engine.connection.statements) == 2

    def test_failed_read_is_none_not_empty(self, build):
        instance, _ = build(FailingEngine())

        assert instance.usage_read_conversation_totals(2, PAGE, "skill", 174, 2) is None

    def test_metering_off_is_unknown_usage_not_zero(self, build):
        instance, engine = build(Engine([ROW]), usage_mode="off")

        assert instance.usage_read_conversation_totals(2, PAGE, "skill", 174, 2) is None
        assert engine.connection.statements == []

    def test_no_conversations_reads_nothing(self, build):
        instance, engine = build(Engine())

        assert instance.usage_read_conversation_totals(2, [], "skill", 174, 2) == {}
        assert engine.connection.statements == []


class TestRootEntityModels:
    def test_models_are_sorted_and_scoped_to_the_entity(self, build):
        instance, engine = build(Engine([("opus",), ("haiku",)]))

        models = instance.usage_read_root_entity_models(2, PAGE, "skill", 123, 1)

        assert models == ["haiku", "opus"]
        sql = engine.connection.statements[0]
        assert "usage_event.root_entity_id = 123" in sql
        assert "usage_event.model_name IS NOT NULL" in sql

    def test_only_the_visible_conversations_contribute(self, build):
        instance, engine = build(Engine([]))

        instance.usage_read_root_entity_models(2, PAGE, "skill", 123, 1)

        assert "usage_event.conversation_id IN ('c1', 'c2')" in engine.connection.statements[0]

    def test_models_across_chunks_are_merged_once(self, build, monkeypatch):
        monkeypatch.setattr(conversation_totals, "ID_CHUNK", 1)
        instance, _ = build(Engine([("opus",)], [("opus",), ("gpt",)]))

        assert instance.usage_read_root_entity_models(2, PAGE, "skill", 123, 1) == ["gpt", "opus"]

    def test_failed_read_is_none(self, build):
        instance, _ = build(FailingEngine())

        assert instance.usage_read_root_entity_models(2, PAGE, "skill", 123, 1) is None

    def test_metering_off_offers_no_models(self, build):
        instance, _ = build(Engine([("opus",)]), usage_mode="off")

        assert instance.usage_read_root_entity_models(2, PAGE, "skill", 123, 1) is None

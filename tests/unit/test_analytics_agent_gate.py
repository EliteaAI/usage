"""is_agent_row() must count real agent/pipeline runs, never evaluation calls against them (#6677).

Before this fix, an evaluation row had NULL root_entity_type/entity_type, so it was invisible to
every agent-scoped KPI by accident. Now it carries root_entity_type='application' — the exact
value a genuine agent run has — so the gate has to actively exclude entity_type='evaluation'
rather than rely on the columns being empty. These tests exercise the real SQL expression
(is_agent_row / agent_runs_expr / agent_error_runs_expr) against an in-memory table, not a
reimplementation of the predicate in Python, so a regression in the actual WHERE clause fails
the test.
"""
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, create_engine, select
from sqlalchemy.orm import Session

from usage.methods import _analytics

METADATA = MetaData()

# Only the columns is_agent_row()/agent_runs_expr()/agent_error_runs_expr() actually touch.
FAKE_TABLE = Table(
    "usage_event", METADATA,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String),
    Column("root_entity_type", String),
    Column("entity_type", String),
    Column("is_error", Boolean),
)


class FakeUsageEvent:
    """Stands in for the real ORM class: same attribute names, backed by FAKE_TABLE."""
    run_id = FAKE_TABLE.c.run_id
    root_entity_type = FAKE_TABLE.c.root_entity_type
    entity_type = FAKE_TABLE.c.entity_type
    is_error = FAKE_TABLE.c.is_error


def _rows_matching(session, expr):
    return {row[0] for row in session.execute(select(FAKE_TABLE.c.run_id).where(expr))}


class TestIsAgentRow:
    def setup_method(self, _method):
        self._patch = _analytics.UsageEvent
        _analytics.UsageEvent = FakeUsageEvent
        self.engine = create_engine("sqlite://")
        METADATA.create_all(self.engine)
        self.session = Session(self.engine)

    def teardown_method(self, _method):
        _analytics.UsageEvent = self._patch
        self.session.close()
        METADATA.drop_all(self.engine)

    def _insert(self, run_id, root_entity_type, entity_type, is_error=False):
        self.session.execute(FAKE_TABLE.insert().values(
            run_id=run_id, root_entity_type=root_entity_type, entity_type=entity_type,
            is_error=is_error,
        ))
        self.session.commit()

    def test_matches_application_row_with_entity_type_set(self):
        self._insert("r1", "application", "application")
        assert _rows_matching(self.session, _analytics.is_agent_row()) == {"r1"}

    def test_matches_application_row_with_entity_type_null(self):
        # Pre-fix rows: attribution never ran, so entity_type is NULL — must still count.
        self._insert("r1", "application", None)
        assert _rows_matching(self.session, _analytics.is_agent_row()) == {"r1"}

    def test_matches_pipeline_row(self):
        self._insert("r1", "pipeline", "pipeline")
        assert _rows_matching(self.session, _analytics.is_agent_row()) == {"r1"}

    def test_does_not_match_evaluation_entity_type(self):
        self._insert("r1", "application", "evaluation")
        assert _rows_matching(self.session, _analytics.is_agent_row()) == set()

    def test_does_not_match_evaluation_entity_type_on_pipeline_root(self):
        self._insert("r1", "pipeline", "evaluation")
        assert _rows_matching(self.session, _analytics.is_agent_row()) == set()

    def test_does_not_match_row_with_no_root_entity_type(self):
        self._insert("r1", None, None)
        assert _rows_matching(self.session, _analytics.is_agent_row()) == set()


class TestAgentRunsExpr:
    """agent_runs_expr()/agent_error_runs_expr() must route through is_agent_row(), not
    reimplement the AGENT_ROOT_TYPES check inline — proven by executing against mixed rows."""

    def setup_method(self, _method):
        self._patch = _analytics.UsageEvent
        _analytics.UsageEvent = FakeUsageEvent
        self.engine = create_engine("sqlite://")
        METADATA.create_all(self.engine)
        self.session = Session(self.engine)

    def teardown_method(self, _method):
        _analytics.UsageEvent = self._patch
        self.session.close()
        METADATA.drop_all(self.engine)

    def _insert(self, run_id, root_entity_type, entity_type, is_error=False):
        self.session.execute(FAKE_TABLE.insert().values(
            run_id=run_id, root_entity_type=root_entity_type, entity_type=entity_type,
            is_error=is_error,
        ))
        self.session.commit()

    def _count(self, expr):
        return self.session.execute(select(expr)).scalar()

    def test_counts_distinct_runs_not_calls(self):
        # one genuine agent run, two calls (llm + tool) inside it
        self._insert("run-genuine", "application", "application")
        self._insert("run-genuine", "application", "application")
        assert self._count(_analytics.agent_runs_expr()) == 1

    def test_excludes_evaluation_judge_and_agent_batch_runs(self):
        self._insert("run-genuine", "application", "application")
        self._insert("run-eval-judge", "application", "evaluation")
        self._insert("run-eval-agent-batch", "application", "evaluation")
        assert self._count(_analytics.agent_runs_expr()) == 1

    def test_error_runs_excludes_evaluation_even_when_erroring(self):
        self._insert("run-genuine", "application", "application", is_error=False)
        self._insert("run-genuine-err", "application", "application", is_error=True)
        self._insert("run-eval-err", "application", "evaluation", is_error=True)
        assert self._count(_analytics.agent_error_runs_expr()) == 1

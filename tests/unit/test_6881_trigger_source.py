"""Automated runs (scheduled/webhook/index) are kept out of people metrics, not out of spend (#6881).

The configuring user is billed for a scheduled pipeline, so those rows keep their real user_id.
Before this, the only "human" test was user_id != 0, which counted that user as active, put
them in Users / top adopters, and inflated Activity. trigger_source (NULL = manual) now
separates them. The real expressions run against an in-memory SQLite copy of the columns used.
"""
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.pool import StaticPool

from usage import hooks
from usage.methods import _analytics
from usage.models.usage_event import UsageEvent


class TestCleanAttribution:
    @pytest.mark.parametrize("source", ["scheduled", "webhook", "index"])
    def test_known_automated_sources_are_kept(self, source):
        assert hooks._clean_attribution({"trigger_source": source}) == {"trigger_source": source}

    @pytest.mark.parametrize("source", ["manual", "cron", "x" * 40, 5, ""])
    def test_manual_or_unknown_is_dropped_so_the_row_reads_as_manual(self, source):
        # Dropped, never the row: an unknown label degrades to NULL (= manual)
        assert "trigger_source" not in hooks._clean_attribution({"trigger_source": source, "entity_id": 1})
        assert hooks._clean_attribution({"trigger_source": source, "entity_id": 1})["entity_id"] == 1

    def test_the_column_is_in_the_known_keys(self):
        assert "trigger_source" in hooks.ATTRIBUTION_KEYS
        assert UsageEvent.__table__.c.trigger_source.type.length == hooks.ATTRIBUTION_TEXT_LIMITS["trigger_source"]


ROWS = [
    # user 3 chats manually, and also owns a webhook-triggered pipeline
    (3, "r1", "llm", 10, None),
    (3, "r1", "tool", 0, None),
    (3, "r4", "llm", 20, "webhook"),
    # user 6 only ever ran a schedule and an index
    (6, "r2", "llm", 100, "scheduled"),
    (6, "r2", "tool", 0, "scheduled"),
    (6, "r3", "llm", 7, "index"),
    # the system sentinel
    (0, "r5", "llm", 1, None),
]


@pytest.fixture
def engine(monkeypatch):
    # One shared connection: ATTACH (standing in for the centry schema) is per-connection
    eng = create_engine("sqlite://", poolclass=StaticPool)
    schema = UsageEvent.__table__.schema
    with eng.begin() as conn:
        conn.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
        conn.execute(text(
            f"CREATE TABLE {schema}.usage_event (user_id INT, run_id TEXT, event_type TEXT,"
            " cost_nano_usd INT, trigger_source TEXT)"
        ))
        for u, r, e, c, t in ROWS:
            conn.execute(
                text(f"INSERT INTO {schema}.usage_event VALUES (:u, :r, :e, :c, :t)"),
                {"u": u, "r": r, "e": e, "c": c, "t": t},
            )

    def fetch_all(statement):
        with eng.connect() as conn:
            return list(conn.execute(statement).mappings())

    monkeypatch.setattr(_analytics, "fetch_all", fetch_all)
    return eng


def _scalar(engine, expr, *conditions):
    with engine.connect() as conn:
        return conn.execute(select(expr).select_from(UsageEvent.__table__).where(*conditions)).scalar()


class TestPeopleMetrics:
    def test_active_users_ignores_automated_only_users_and_the_system_actor(self, engine):
        # user 6 ran only automated work; user 0 is the sentinel -> only user 3 is active
        assert _scalar(engine, _analytics.active_users_expr()) == 1

    def test_manual_users_counts_any_user_with_a_manual_row(self, engine):
        # Applied on top of base_filters (which already drops user 0); here only the manual rule
        assert _scalar(engine, _analytics.manual_users_expr()) == 2

    def test_manual_run_filter_keeps_null_rows_only(self, engine):
        assert _scalar(engine, func.count(), _analytics.MANUAL_RUN) == 3


class TestSpendKeepsEverything:
    def test_cost_over_the_same_rows_still_includes_automated_runs(self, engine):
        # Spend is never filtered on trigger_source: automated runs stay billed to their owner
        assert _scalar(engine, func.sum(UsageEvent.cost_nano_usd)) == 138


class TestAutomatedBucket:
    def test_grouped_by_source_with_runs_calls_tools_and_cost(self, engine):
        out = {b["trigger_source"]: b for b in _analytics.automated_activity([])}
        assert set(out) == {"index", "scheduled", "webhook"}
        assert out["scheduled"]["runs"] == 1
        assert out["scheduled"]["llm_calls"] == 1 and out["scheduled"]["tool_runs"] == 1
        assert out["scheduled"]["llm_cost"] == _analytics.cost_usd(100)
        assert out["webhook"]["llm_calls"] == 1 and out["index"]["llm_calls"] == 1

    def test_no_automated_rows_is_an_empty_list(self, engine):
        assert _analytics.automated_activity([_analytics.MANUAL_RUN]) == []

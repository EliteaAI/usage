"""usage_eval_run_breakdown: one eval run's llm usage per conversation_id (#6716).

elitea_core settles each case's tokens and cost from these sums, so the statement runs here against
an in-memory table rather than being re-implemented in Python.
"""
import datetime

import pytest
from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, MetaData, String, Table, create_engine
from sqlalchemy.orm import Session

from usage.methods import _analytics

METADATA = MetaData()

FAKE_TABLE = Table(
    "usage_event", METADATA,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("project_id", Integer),
    Column("ts", DateTime(timezone=True)),
    Column("run_id", String),
    Column("conversation_id", String),
    Column("event_type", String),
    Column("model_name", String),
    *[Column(name, BigInteger, default=0) for name in (
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
        "reasoning_tokens", "billable_input_tokens", "cost_nano_usd")],
    Column("cost_source", String),
    Column("token_source", String),
    Column("is_error", Boolean, default=False),
)


class FakeUsageEvent:  # pylint: disable=R0903
    """Same attribute names as the ORM class, backed by FAKE_TABLE."""


for _column in FAKE_TABLE.c:
    setattr(FakeUsageEvent, _column.name, _column)

RUN = "0b6a3c2e-6f43-4c4e-9f07-3c7d1f0f8a11"
OTHER_RUN = "9d1f7b0c-1111-4c4e-9f07-3c7d1f0f8a11"
T0 = datetime.datetime(2026, 10, 6, 7, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def session(monkeypatch):
    monkeypatch.setattr(_analytics, "UsageEvent", FakeUsageEvent)
    engine = create_engine("sqlite://")
    METADATA.create_all(engine)
    with Session(engine) as db_session:
        monkeypatch.setattr(_analytics, "fetch_all",
                            lambda statement: list(db_session.execute(statement).mappings()))
        yield db_session
    METADATA.drop_all(engine)


def _insert(session, conversation_id, *, run_id=RUN, event_type="llm", project_id=1, minutes=1,
            input_tokens=0, output_tokens=0, cost_nano_usd=0, cost_source="costs-catalog",
            model_name="gpt-4o", token_source="provider", is_error=False):
    session.execute(FAKE_TABLE.insert().values(
        project_id=project_id, ts=T0 + datetime.timedelta(minutes=minutes), run_id=run_id,
        conversation_id=conversation_id, event_type=event_type, model_name=model_name,
        input_tokens=input_tokens, output_tokens=output_tokens, billable_input_tokens=input_tokens,
        cache_read_tokens=0, cache_creation_tokens=0, reasoning_tokens=0,
        cost_nano_usd=cost_nano_usd, cost_source=cost_source, token_source=token_source,
        is_error=is_error,
    ))
    session.commit()


def _breakdown(**kwargs):
    rows = _analytics.eval_run_breakdown(1, RUN, T0, T0 + datetime.timedelta(hours=1), **kwargs)
    return {row["conversation_id"]: row for row in rows}


def test_sums_llm_rows_per_conversation(session):
    _insert(session, "a_0", input_tokens=100, output_tokens=10, cost_nano_usd=500)
    _insert(session, "a_0", input_tokens=50, output_tokens=5, cost_nano_usd=250)
    _insert(session, "j_0_0", input_tokens=7, output_tokens=3, cost_nano_usd=9)
    rows = _breakdown()
    assert set(rows) == {"a_0", "j_0_0"}
    assert (rows["a_0"]["llm_calls"], rows["a_0"]["input_tokens"], rows["a_0"]["output_tokens"],
            rows["a_0"]["cost_nano_usd"], rows["a_0"]["unpriced_calls"]) == (2, 150, 15, 750, 0)


def test_other_runs_projects_tools_and_out_of_window_rows_are_left_out(session):
    _insert(session, "a_0", input_tokens=1)
    _insert(session, "a_0", run_id=OTHER_RUN, input_tokens=1000)
    _insert(session, "a_0", project_id=2, input_tokens=1000)
    _insert(session, "a_0", event_type="tool")
    _insert(session, "a_0", minutes=120, input_tokens=1000)
    assert _breakdown()["a_0"]["input_tokens"] == 1
    assert _breakdown()["a_0"]["llm_calls"] == 1


def test_unpriced_call_with_tokens_is_flagged(session):
    _insert(session, "a_0", input_tokens=10, cost_source="unpriced")
    _insert(session, "a_0", input_tokens=10, cost_source=None)
    _insert(session, "a_1", input_tokens=0, cost_source="unpriced")  # nothing to price
    rows = _breakdown()
    assert rows["a_0"]["unpriced_calls"] == 2
    assert rows["a_1"]["unpriced_calls"] == 0


def test_unparsed_successful_call_is_flagged(session):
    # The proxy could not read the response, so the row's 0/0 is not a reading.
    _insert(session, "a_0", input_tokens=40)
    _insert(session, "a_0", token_source="unparsed")
    _insert(session, "a_1", token_source="unparsed", is_error=True)  # failed call: 0 is real
    rows = _breakdown()
    assert (rows["a_0"]["llm_calls"], rows["a_0"]["unparsed_calls"]) == (2, 1)
    assert rows["a_1"]["unparsed_calls"] == 0


def test_malformed_run_id_is_rejected():
    with pytest.raises(ValueError, match="platform_run_id"):
        _analytics.eval_run_breakdown(1, "not-a-uuid")

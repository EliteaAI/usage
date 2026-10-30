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
    Column("entity_type", String),
    Column("entity_id", Integer),
    Column("entity_name", String),
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
            model_name="gpt-4o", token_source="provider", is_error=False,
            entity=("evaluation", 219, None), billable_input_tokens=None, cache_read_tokens=0,
            cache_creation_tokens=0):
    session.execute(FAKE_TABLE.insert().values(
        project_id=project_id, ts=T0 + datetime.timedelta(minutes=minutes), run_id=run_id,
        conversation_id=conversation_id, event_type=event_type, model_name=model_name,
        input_tokens=input_tokens, output_tokens=output_tokens,
        billable_input_tokens=input_tokens if billable_input_tokens is None else billable_input_tokens,
        cache_read_tokens=cache_read_tokens, cache_creation_tokens=cache_creation_tokens, reasoning_tokens=0,
        cost_nano_usd=cost_nano_usd, cost_source=cost_source, token_source=token_source,
        is_error=is_error, entity_type=entity[0], entity_id=entity[1], entity_name=entity[2],
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


def test_sums_billable_input_and_cache_per_conversation(session):
    """#7300: elitea_core rebuilds cache-inclusive input from these, whatever the provider's convention."""
    _insert(session, "a_0", input_tokens=1_000, billable_input_tokens=600, cache_read_tokens=400)  # inclusive
    _insert(session, "a_0", input_tokens=200, cache_read_tokens=900, cache_creation_tokens=300)  # exclusive
    row = _breakdown()["a_0"]
    assert (row["input_tokens"], row["billable_input_tokens"], row["cache_read_tokens"],
            row["cache_creation_tokens"]) == (1_200, 800, 1_300, 300)


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


# --- by_entity: a dispatched sub-agent's calls carry its own application (#7015 P3) ----------

ORDER_READER = ("application", 53, "RD-OrderReader")
DECISION_READER = ("application", 54, "RD-DecisionReader")


def test_by_entity_splits_a_conversation_by_its_labelled_entity(session):
    _insert(session, "a_0", input_tokens=100, output_tokens=10, cost_nano_usd=500)
    _insert(session, "a_0", input_tokens=40, output_tokens=4, cost_nano_usd=200, entity=ORDER_READER)
    _insert(session, "a_0", input_tokens=60, output_tokens=6, cost_nano_usd=300, entity=ORDER_READER)
    _insert(session, "a_0", input_tokens=30, output_tokens=3, cost_nano_usd=150, entity=DECISION_READER)
    rows = _analytics.eval_run_breakdown(1, RUN, T0, T0 + datetime.timedelta(hours=1), by_entity=True)
    split = {(r["entity_type"], r["entity_id"], r["entity_name"]): r for r in rows}
    assert set(split) == {("evaluation", 219, None), ORDER_READER, DECISION_READER}
    assert (split[ORDER_READER]["llm_calls"], split[ORDER_READER]["input_tokens"],
            split[ORDER_READER]["cost_nano_usd"]) == (2, 100, 500)
    assert split[DECISION_READER]["output_tokens"] == 3
    assert {r["conversation_id"] for r in rows} == {"a_0"}


def test_by_entity_sums_match_the_plain_breakdown(session):
    _insert(session, "a_0", input_tokens=100, cost_nano_usd=500)
    _insert(session, "a_0", input_tokens=40, cost_nano_usd=200, entity=ORDER_READER)
    _insert(session, "a_1", input_tokens=7, cost_nano_usd=9, entity=DECISION_READER)
    _insert(session, "a_1", input_tokens=5, cost_nano_usd=3, entity=(None, None, None))
    plain = _breakdown()
    rows = _analytics.eval_run_breakdown(1, RUN, T0, T0 + datetime.timedelta(hours=1), by_entity=True)
    for conversation_id, row in plain.items():
        mine = [r for r in rows if r["conversation_id"] == conversation_id]
        for key in ("llm_calls", "input_tokens", "cost_nano_usd"):
            assert sum(r[key] for r in mine) == row[key]


def test_an_unlabelled_row_has_a_none_entity(session):
    _insert(session, "a_0", input_tokens=5, entity=(None, None, None))
    rows = _analytics.eval_run_breakdown(1, RUN, T0, T0 + datetime.timedelta(hours=1), by_entity=True)
    assert [(r["entity_type"], r["entity_id"], r["entity_name"], r["input_tokens"]) for r in rows] == \
        [(None, None, None, 5)]


def test_an_application_renamed_mid_run_stays_one_row(session):
    _insert(session, "a_0", input_tokens=40, entity=("application", 53, "RD-OrderReader"))
    _insert(session, "a_0", input_tokens=60, minutes=2, entity=("application", 53, "RD-OrderReader2"))
    rows = _analytics.eval_run_breakdown(1, RUN, T0, T0 + datetime.timedelta(hours=1), by_entity=True)
    assert [(r["entity_id"], r["input_tokens"]) for r in rows] == [(53, 100)]


def test_plain_breakdown_carries_no_entity_keys(session):
    _insert(session, "a_0", input_tokens=1, entity=ORDER_READER)
    assert "entity_name" not in _breakdown()["a_0"]

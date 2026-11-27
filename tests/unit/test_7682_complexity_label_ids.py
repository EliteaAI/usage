"""#7682 BE-2 — the conversation ids of an entity's runs with a complexity label (PR 80 review).

The label is computed in SQL and the result is capped, so the method is checked against a fake connection that
records the statements and the parameters it was given.
"""
import datetime
import types

import pytest

from usage.methods import complexity as jobs

UTC = datetime.timezone.utc
FROM, TO = datetime.datetime(2000, 1, 1, tzinfo=UTC), datetime.datetime(2100, 1, 1, tzinfo=UTC)
LATER = datetime.datetime(2026, 11, 26, 12, tzinfo=UTC)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


@pytest.fixture
def labelled(monkeypatch):
    """Runs the method with scored and not scored rows given; returns (result, statements)."""
    statements = []

    def run(scored=(), unscored=(), labels=("L",)):
        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, statement, params=None):
                text_sql = str(statement)
                statements.append((text_sql, params))
                if "entity_complexity_state" in text_sql:
                    return Result([{"thresholds": {"medium": 10, "high": 30, "extreme": 100}}])
                if "FROM centry.entity_complexity_run r" in text_sql and "NOT EXISTS" not in text_sql:
                    return Result(list(scored))
                return Result(list(unscored))

        monkeypatch.setattr(jobs, "db", types.SimpleNamespace(engine=types.SimpleNamespace(connect=Connection)))
        monkeypatch.setattr(jobs.c, "POSTGRES_SCHEMA", "centry", raising=False)
        method = types.SimpleNamespace(usage_complexity_labelled_conversation_ids=None)
        return jobs.Method.usage_complexity_labelled_conversation_ids(method, 3, 26, FROM, TO, labels)

    return run, statements


@pytest.mark.parametrize("labels", [["bogus"], ["L", "bogus"], [], None, ["l"]])
def test_a_label_outside_the_five_is_rejected(labelled, labels):
    run, statements = labelled
    with pytest.raises(ValueError):
        run(labels=labels)
    assert statements == []


def test_the_scored_labels_are_filtered_in_sql_with_the_stored_thresholds(labelled):
    run, statements = labelled
    run(labels=["H", "X", "L"])
    sql, params = next((s, p) for s, p in statements if "entity_complexity_run r" in s and "CASE" in s)
    assert "= ANY(:labels)" in sql and "LIMIT :limit" in sql
    assert params["labels"] == ["H", "L", "X"]
    assert (params["medium"], params["high"], params["extreme"]) == (10, 30, 100)
    assert params["limit"] == jobs.MAX_LABEL_CONVERSATIONS + 1


def test_only_not_scored_skips_the_scored_query(labelled):
    run, statements = labelled
    run(labels=["not_scored"])
    assert not any("CASE" in sql for sql, _ in statements)
    assert any("NOT EXISTS" in sql for sql, _ in statements)


def test_not_scored_waits_out_the_pending_window_in_sql(labelled):
    run, statements = labelled
    run(labels=["not_scored"])
    sql, params = next((s, p) for s, p in statements if "NOT EXISTS" in s)
    assert "HAVING max(e.ts) < :pending_before" in sql
    window = datetime.datetime.now(UTC) - params["pending_before"]
    assert datetime.timedelta(hours=2) <= window < datetime.timedelta(hours=2, minutes=1)


def test_the_newest_conversations_come_first_and_a_conversation_counts_once(labelled):
    run, _ = labelled
    day = datetime.timedelta(days=1)
    result = run(
        scored=[("a", LATER - 2 * day), ("b", LATER)],
        unscored=[("a", LATER + day), ("c", LATER - day)],
        labels=["L", "not_scored"],
    )
    assert result == {"conversation_ids": ["a", "b", "c"], "truncated": False}


def test_the_ids_are_cut_at_the_cap_and_truncated_says_so(labelled, monkeypatch):
    run, _ = labelled
    monkeypatch.setattr(jobs, "MAX_LABEL_CONVERSATIONS", 3)
    rows = [(f"c{i}", LATER - datetime.timedelta(hours=i)) for i in range(5)]
    result = run(scored=rows)
    assert result == {"conversation_ids": ["c0", "c1", "c2"], "truncated": True}


def test_exactly_the_cap_is_not_truncated(labelled, monkeypatch):
    run, _ = labelled
    monkeypatch.setattr(jobs, "MAX_LABEL_CONVERSATIONS", 3)
    rows = [(f"c{i}", LATER - datetime.timedelta(hours=i)) for i in range(3)]
    assert run(scored=rows) == {"conversation_ids": ["c0", "c1", "c2"], "truncated": False}

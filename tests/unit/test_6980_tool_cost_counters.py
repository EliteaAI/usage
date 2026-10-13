"""#6980: tool_cost rows feed per-feature counters (model_name=<feature>) and never the LLM '' rows.

A retried record (same permit → same idempotency_key and ts) must land and count exactly once,
through the queue/drainer path and through the direct-insert fallback alike.
"""
import types

from fixtures.helpers import bind, fake_module
from usage.methods import drainer, events

from unit.test_drainer import TS, Landing, _Tagged, build, event, insert


def tool_cost(key, feature="web_search", exempt=None, cost=8_000_000, user_id=7):
    row = dict(event(key, cost=cost, event_type="tool_cost", user_id=user_id),
               input_tokens=0, output_tokens=0, tool_name="web_search", model_name="tavily/search",
               meta={"feature": feature, "backend": "tavily", "op": "search", "units": 1, "unit": "credit"})
    if exempt is not None:
        row["budget_exempt"] = exempt
    return row


def strip_returning(rows):
    # The real RETURNING projects neither meta nor budget_exempt: model that
    return [{k: v for k, v in row.items() if k not in ("meta", "budget_exempt", "tool_name")} for row in rows]


class TestDrainerPath:
    def test_feature_counter_rows_use_the_feature_as_model_name(self):
        connection = Landing()
        insert(build(connection), connection, [tool_cost("t1")])
        #
        assert {d["model_name"] for d in connection.upserts} == {"web_search"}
        assert {(d["user_id"], d["call_count"], d["cost_nano_usd"]) for d in connection.upserts} \
            == {(0, 1, 8_000_000), (7, 1, 8_000_000)}

    def test_a_retried_record_lands_once_and_counts_once(self):
        connection = Landing()
        instance = build(connection)
        insert(instance, connection, [tool_cost("tool_cost:n1")])
        insert(instance, connection, [tool_cost("tool_cost:n1")])
        #
        assert sum(d["call_count"] for d in connection.upserts if d["user_id"] == 0) == 1

    def test_meta_is_joined_from_the_input_not_returning(self):
        connection = Landing()
        instance = build(connection)
        rows = [tool_cost("t1")]
        statement = _Tagged(strip_returning(rows))
        real = drainer.insert
        drainer.insert = lambda *a, **k: statement
        try:
            instance.usage_insert_events(connection, rows)
        finally:
            drainer.insert = real
        #
        assert {d["model_name"] for d in connection.upserts} == {"web_search"}

    def test_exempt_tool_cost_lands_but_counts_nothing(self):
        connection = Landing()
        landed = insert(build(connection), connection, [tool_cost("t1", exempt=True)])
        #
        assert len(landed) == 1
        assert connection.upserts == []

    def test_mixed_batch_keeps_llm_deltas_unchanged(self):
        only_llm, mixed = Landing(), Landing()
        insert(build(only_llm), only_llm, [event("a", cost=3000)])
        insert(build(mixed), mixed, [event("a", cost=3000), tool_cost("t1")])
        #
        llm_part = [d for d in mixed.upserts if d["model_name"] == ""]
        assert llm_part == only_llm.upserts

    def test_tool_cost_without_feature_counts_nothing(self):
        row = tool_cost("t1")
        row["meta"] = {"backend": "tavily"}
        connection = Landing()
        insert(build(connection), connection, [row])
        assert connection.upserts == []


class TestDirectPath:
    @staticmethod
    def _write(monkeypatch, connection, row):
        instance = fake_module()
        bind(instance, events.Method)
        instance.usage_apply_counter_deltas = lambda conn, deltas: connection.upserts.extend(deltas)
        monkeypatch.setattr(events, "insert", lambda *a, **k: _Tagged(strip_returning([row])))
        monkeypatch.setattr(events, "event_values", lambda rows: [dict(r) for r in rows])
        monkeypatch.setattr(events, "db", types.SimpleNamespace(
            engine=types.SimpleNamespace(connect=lambda: connection),
        ))
        return instance.usage_write_event(row)

    def test_a_retried_record_lands_once_and_counts_once(self, monkeypatch):
        connection = Landing()
        assert self._write(monkeypatch, connection, tool_cost("tool_cost:n1")) is True
        assert self._write(monkeypatch, connection, tool_cost("tool_cost:n1")) is False
        #
        assert {d["model_name"] for d in connection.upserts} == {"web_search"}
        assert sum(d["call_count"] for d in connection.upserts if d["user_id"] == 0) == 1

    def test_exempt_counts_nothing(self, monkeypatch):
        connection = Landing()
        assert self._write(monkeypatch, connection, tool_cost("t1", exempt=True)) is True
        assert connection.upserts == []

    def test_caller_ts_is_kept(self, monkeypatch):
        connection = Landing()
        self._write(monkeypatch, connection, tool_cost("t1"))
        assert {key[1] for key in connection.landed} == {TS}

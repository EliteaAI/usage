"""#6980 ledger side: tool_cost row build, record RPC, typed feature spend, feature warnings."""
import datetime
import types

import pytest

from fixtures.helpers import bind, fake_module
from usage.methods import mode as mode_module
from usage.methods import spend, tool_cost, warning as warning_module
from unit.test_spend import Engine, FailingEngine

IAT = datetime.datetime(2026, 10, 9, 10, 0, tzinfo=datetime.timezone.utc)


def record(**over):
    base = {
        "ts": IAT, "idempotency_key": "tool_cost:nonce1", "project_id": 3, "user_id": 7,
        "feature": "web_search", "backend": "tavily", "op": "search", "units": 1, "unit": "credit",
        "ok": True, "duration_ms": 120, "tool_name": "web_search",
        "cost_usd": 0.008, "cost_source": "estimated:costs-tool-config",
        "meta": {"credits_source": "reported", "query": "secret customer data", "urls": ["http://x"]},
    }
    base.update(over)
    return base


class TestRowBuild:
    def test_row_shape(self):
        row = tool_cost.tool_cost_row(record(), {"conversation_id": "c1"})
        assert row["event_type"] == "tool_cost"
        assert row["model_name"] == "tavily/search"
        assert row["cost_nano_usd"] == 8_000_000
        assert row["ts"] is IAT and row["idempotency_key"] == "tool_cost:nonce1"
        assert row["conversation_id"] == "c1"
        assert row["meta"]["feature"] == "web_search" and row["meta"]["credits_source"] == "reported"
        assert "budget_exempt" not in row

    def test_no_query_text_or_urls_in_the_ledger(self):
        meta = tool_cost.tool_cost_row(record(), {})["meta"]
        assert "query" not in meta and "urls" not in meta

    def test_unpriced_and_failed(self):
        row = tool_cost.tool_cost_row(record(cost_usd=None, ok=False, units=0), {})
        assert row["cost_source"] == "unpriced" and row["cost_nano_usd"] == 0 and row["is_error"] is True

    def test_exempt_flag(self):
        assert tool_cost.tool_cost_row(record(budget_exempt=True), {})["budget_exempt"] is True


def recorder(mode="enforce", queue_ok=True, landed=True):
    instance = bind(fake_module(config={"usage": {"mode": mode}}), mode_module.Method, tool_cost.Method)
    instance.queued, instance.written = [], []
    instance.usage_enqueue_event = lambda row: (instance.queued.append(row), queue_ok)[1]
    instance.usage_write_event = lambda row: (instance.written.append(row), landed)[1]
    return instance


class TestRecord:
    def test_mode_off_writes_nothing(self):
        instance = recorder(mode="off")
        assert instance.usage_record_tool_cost(record())["recorded"] is False
        assert instance.queued == [] and instance.written == []

    def test_queued_with_caller_ts(self):
        instance = recorder()
        assert instance.usage_record_tool_cost(record())["recorded"] is True
        assert instance.queued[0]["ts"] is IAT

    def test_direct_fallback_reports_duplicate(self):
        instance = recorder(queue_ok=False, landed=False)
        assert instance.usage_record_tool_cost(record()) == {"recorded": True, "duplicate": True}

    def test_bad_attribution_is_dropped_not_raised(self):
        instance = recorder()
        instance.usage_record_tool_cost(record(), attribution="not-a-signed-header")
        assert "conversation_id" not in instance.queued[0]

    def test_missing_permit_fields_raise(self):
        with pytest.raises(ValueError):
            recorder().usage_record_tool_cost(record(ts=None))


@pytest.fixture
def spend_with(monkeypatch):
    spend._feature_spend_cache.clear()  # pylint: disable=W0212
    def make(engine):
        monkeypatch.setattr(spend.db, "engine", engine, raising=False)
        return bind(fake_module(), spend.Method)
    return make


class TestFeatureSpend:
    def test_reads_project_and_member_rows(self, spend_with):
        instance = spend_with(Engine([(0, 50), (7, 20)]))
        out = instance.usage_read_feature_spend(3, 7, "web_search", moment=IAT)
        assert out["available"] is True
        assert (out["project_spent_nano"], out["member_spent_nano"]) == (50, 20)
        assert out["period_start"] == "2026-10-01"

    def test_no_rows_is_available_zero(self, spend_with):
        out = spend_with(Engine([])).usage_read_feature_spend(3, 7, "web_search", moment=IAT)
        assert out["available"] is True and out["project_spent_nano"] == 0

    def test_db_error_is_unavailable_and_not_cached(self, spend_with, monkeypatch):
        instance = spend_with(FailingEngine())
        assert instance.usage_read_feature_spend(3, 7, "web_search", moment=IAT) == {"available": False}
        monkeypatch.setattr(spend.db, "engine", Engine([(0, 5)]), raising=False)
        assert instance.usage_read_feature_spend(3, 7, "web_search", moment=IAT)["available"] is True

    def test_query_pins_the_feature_model_name(self, spend_with):
        engine = Engine([])
        spend_with(engine).usage_read_feature_spend(3, 7, "web_search", moment=IAT)
        assert "model_name" in engine.connection.statements[0]


class TestFeatureWarning:
    @staticmethod
    def build(config, limits, spent, monkeypatch):
        warning_module._feature_warning_cache.clear()  # pylint: disable=W0212
        instance = bind(fake_module(config={"usage": config}), mode_module.Method, warning_module.Method)
        instance.usage_read_feature_spend = lambda *a, **k: spent
        rpc = types.SimpleNamespace(elitea_core_get_effective_feature_limits=lambda **k: limits)
        monkeypatch.setattr(warning_module.context, "rpc_manager",
                            types.SimpleNamespace(timeout=lambda _t: rpc), raising=False)
        return instance

    SPENT = {"available": True, "project_spent_nano": 85 * 10**7, "member_spent_nano": 0}

    def test_project_warning_at_feature_threshold(self, monkeypatch):
        instance = self.build({"mode": "enforce", "features": {"web_search": {"warning_pct": 50}}},
                              {"project_limit_usd": 1.0}, self.SPENT, monkeypatch)
        state = instance.usage_resolve_feature_warning("web_search", 3, 7)
        assert state["should_warn"] and state["scope"] == "project" and state["warning_pct"] == 50
        assert state["feature"] == "web_search"

    def test_falls_back_to_llm_threshold(self, monkeypatch):
        instance = self.build({"mode": "enforce"}, {"project_limit_usd": 1.0}, self.SPENT, monkeypatch)
        assert instance.usage_resolve_feature_warning("web_search", 3, 7)["warning_pct"] == 80

    def test_unavailable_spend_or_observe_is_no_warning(self, monkeypatch):
        instance = self.build({"mode": "enforce"}, {"project_limit_usd": 1.0}, {"available": False}, monkeypatch)
        assert instance.usage_resolve_feature_warning("web_search", 3, 7)["should_warn"] is False
        instance = self.build({"mode": "observe"}, {"project_limit_usd": 1.0}, self.SPENT, monkeypatch)
        assert instance.usage_resolve_feature_warning("web_search", 3, 7)["should_warn"] is False


class TestRowCanOnlyAddSpend:
    """Second guard behind the API: whatever reaches the RPC, a row never lowers spend."""

    @pytest.mark.parametrize("field,value", [
        ("units", -100), ("units", float("nan")), ("units", float("inf")), ("units", "5"), ("units", True),
        ("cost_usd", -0.008), ("cost_usd", float("nan")), ("cost_usd", float("-inf")), ("cost_usd", 10**12),
        ("duration_ms", -1), ("feature", "x" * 65), ("op", ""),
    ])
    def test_invalid_money_or_shape_is_rejected(self, field, value):
        with pytest.raises(ValueError):
            tool_cost.tool_cost_row(record(**{field: value}), {})

    def test_record_rpc_raises_and_writes_nothing(self):
        instance = recorder()
        with pytest.raises(ValueError):
            instance.usage_record_tool_cost(record(units=-100))
        assert instance.queued == [] and instance.written == []

    def test_meta_values_are_typed_scalars_only(self):
        meta = tool_cost.tool_cost_row(record(meta={
            "credits_source": "reported", "depth": {"nested": "x" * 10_000}, "results_count": -1,
            "urls_count": 3, "failed_urls": True,
        }), {})["meta"]
        assert meta["credits_source"] == "reported" and meta["urls_count"] == 3
        assert "depth" not in meta and "results_count" not in meta and "failed_urls" not in meta

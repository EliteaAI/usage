"""Facade dispatch: which backend each usage_* RPC reads, and what it returns when that fails.

Dispatch is the only real logic in this issue, so this is the densest unit file.
"""
import pytest

from usage.methods import mode as mode_module
from usage.rpc import facade

from fixtures.helpers import bind, fake_module
from fixtures.stubs import MissingRpc, RecordingRpc


# name -> (kwargs the caller passes, litellm delegate, zero shape factory)
SPEND_RPCS = {
    "usage_get_project_spend": ({"project_id": 7}, "litellm_get_project_spend", facade.empty_spend),
    "usage_get_user_spend": (
        {"project_id": 7, "user_id": 42}, "litellm_get_user_spend", facade.empty_spend,
    ),
    "usage_get_project_usage_detail": (
        {"project_id": 7}, "litellm_get_project_usage_detail", facade.empty_usage_detail,
    ),
    "usage_get_user_usage_detail": (
        {"project_id": 7, "user_id": 42}, "litellm_get_user_usage_detail",
        facade.empty_usage_detail,
    ),
}

MAP_RPCS = {
    "usage_get_projects_spend": (
        {"project_ids": [1, 2]}, "litellm_get_projects_spend", {1: 0.0, 2: 0.0},
    ),
    "usage_get_users_spend": (
        {"project_id": 7, "user_ids": [4, 5]}, "litellm_get_users_spend", {4: 0.0, 5: 0.0},
    ),
}

ALL_RPCS = dict(SPEND_RPCS)
ALL_RPCS.update({name: (kwargs, delegate, None) for name, (kwargs, delegate, _) in MAP_RPCS.items()})
ALL_RPCS["usage_list_member_spend"] = (
    {"project_id": 7, "period": None}, "litellm_list_member_spend", None,
)


@pytest.fixture
def instance(monkeypatch):
    """A facade bound to a Module stand-in, with the rpc_manager replaced per test."""
    def build(config, rpc=None):
        stand_in = fake_module(config={"usage": config})
        bound = bind(stand_in, mode_module.Method, facade.RPC)
        monkeypatch.setattr(
            facade, "context", type("Ctx", (), {"rpc_manager": rpc or RecordingRpc()})(),
        )
        return bound
    #
    return build


class TestLitellmBranch:
    @pytest.mark.parametrize("name", sorted(ALL_RPCS))
    def test_delegate_called_once_with_the_same_kwargs(self, instance, name):
        kwargs, delegate, _ = ALL_RPCS[name]
        rpc = RecordingRpc(returns={delegate: {"sentinel": True}})
        bound = instance({"spend_source": "litellm"}, rpc)
        #
        getattr(bound, name)(**kwargs)
        #
        assert rpc.names() == [delegate]
        assert rpc.calls[0][1] == kwargs

    @pytest.mark.parametrize("name", sorted(ALL_RPCS))
    def test_delegate_return_value_passes_through_unmodified(self, instance, name):
        kwargs, delegate, _ = ALL_RPCS[name]
        payload = {"spend": 12.5, "available": True}
        rpc = RecordingRpc(returns={delegate: payload})
        bound = instance({"spend_source": "litellm"}, rpc)
        #
        # Identity, not equality: the facade must not copy or reshape the legacy payload.
        assert getattr(bound, name)(**kwargs) is payload


class TestEliteaBranch:
    @pytest.mark.parametrize("name", sorted(ALL_RPCS))
    def test_delegate_is_not_called_at_all(self, instance, name):
        kwargs, _, _ = ALL_RPCS[name]
        rpc = RecordingRpc()
        bound = instance({"spend_source": "elitea"}, rpc)
        #
        getattr(bound, name)(**kwargs)
        #
        assert rpc.names() == []

    @pytest.mark.parametrize("name", sorted(SPEND_RPCS))
    def test_zero_shape_is_unavailable(self, instance, name):
        kwargs, _, factory = SPEND_RPCS[name]
        bound = instance({"spend_source": "elitea"})
        #
        result = getattr(bound, name)(**kwargs)
        #
        assert result == factory()
        assert result["available"] is False

    @pytest.mark.parametrize("name", sorted(MAP_RPCS))
    def test_map_rpcs_return_a_zeroed_key_per_requested_id(self, instance, name):
        kwargs, _, expected = MAP_RPCS[name]
        bound = instance({"spend_source": "elitea"})
        #
        assert getattr(bound, name)(**kwargs) == expected

    def test_member_spend_returns_none_for_the_unreachable_contract(self, instance):
        bound = instance({"spend_source": "elitea"})
        #
        assert bound.usage_list_member_spend(project_id=7) is None


class TestAutoResolution:
    def test_auto_reads_litellm_while_mode_is_off(self, instance):
        rpc = RecordingRpc(returns={"litellm_get_project_spend": {"available": True}})
        bound = instance({"mode": "off", "spend_source": "auto"}, rpc)
        #
        bound.usage_get_project_spend(project_id=7)
        #
        assert rpc.names() == ["litellm_get_project_spend"]

    @pytest.mark.parametrize("mode", ["observe", "enforce"])
    def test_auto_reads_elitea_once_metering_is_on(self, instance, mode):
        rpc = RecordingRpc()
        bound = instance({"mode": mode, "spend_source": "auto"}, rpc)
        #
        bound.usage_get_project_spend(project_id=7)
        #
        # One flag moves metering and reading together: never read an empty counter table
        # while LiteLLM still holds the real numbers, nor the reverse.
        assert rpc.names() == []

    def test_absent_spend_source_behaves_as_auto(self, instance):
        rpc = RecordingRpc(returns={"litellm_get_project_spend": {"available": True}})
        bound = instance({}, rpc)
        #
        bound.usage_get_project_spend(project_id=7)
        #
        assert rpc.names() == ["litellm_get_project_spend"]

    def test_explicit_litellm_wins_over_enforce_mode(self, instance):
        rpc = RecordingRpc(returns={"litellm_get_project_spend": {"available": True}})
        bound = instance({"mode": "enforce", "spend_source": "litellm"}, rpc)
        #
        bound.usage_get_project_spend(project_id=7)
        #
        assert rpc.names() == ["litellm_get_project_spend"]


class TestDelegateFailure:
    @pytest.mark.parametrize("name", sorted(SPEND_RPCS))
    def test_missing_delegate_degrades_to_the_zero_shape(self, instance, name, recording_log):
        kwargs, _, factory = SPEND_RPCS[name]
        bound = instance({"spend_source": "litellm"}, MissingRpc())
        #
        result = getattr(bound, name)(**kwargs)
        #
        assert result == factory()
        assert recording_log.messages("warning")

    @pytest.mark.parametrize("name", sorted(SPEND_RPCS))
    def test_raising_delegate_degrades_and_logs_the_traceback(self, instance, name, recording_log):
        kwargs, delegate, factory = SPEND_RPCS[name]
        rpc = RecordingRpc(raises={delegate: RuntimeError("boom")})
        bound = instance({"spend_source": "litellm"}, rpc)
        #
        result = getattr(bound, name)(**kwargs)
        #
        # A broken spend read must degrade the Usage page, never 500 it.
        assert result == factory()
        assert recording_log.messages("exception")

    def test_map_rpc_failure_still_returns_a_key_per_id(self, instance):
        rpc = RecordingRpc(raises={"litellm_get_projects_spend": RuntimeError("boom")})
        bound = instance({"spend_source": "litellm"}, rpc)
        #
        assert bound.usage_get_projects_spend(project_ids=[1, 2]) == {1: 0.0, 2: 0.0}

    def test_member_spend_failure_returns_none(self, instance):
        rpc = RecordingRpc(raises={"litellm_list_member_spend": RuntimeError("boom")})
        bound = instance({"spend_source": "litellm"}, rpc)
        #
        assert bound.usage_list_member_spend(project_id=7) is None


class TestShapeParity:
    """The tests that make "callers are unchanged" mean something."""

    # Recorded from runtime_interface_litellm read_tag_spend / read_tag_usage_detail.
    LITELLM_SPEND = {
        "tag": "project-7-202609", "period": "202609", "spend": 3.5,
        "prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160,
        "available": True,
    }
    LITELLM_DETAIL = {
        "tag": "project-7-202609", "period": "202609",
        "models": [{"model": "gpt-4o", "spend": 3.5, "total_tokens": 160, "api_requests": 2}],
        "daily": [{"date": "2026-09-01", "spend": 3.5}],
        "spend": 3.5, "total_tokens": 160, "input_tokens": 120, "output_tokens": 40,
        "cache_read_tokens": 0, "cache_creation_tokens": 0, "api_requests": 2,
        "available": True,
    }

    def assert_parity(self, legacy, zero):
        assert set(zero) == set(legacy)
        for key, legacy_value in legacy.items():
            if key == "available":
                continue
            assert type(zero[key]) is type(legacy_value), key  # noqa: E721

    def test_spend_zero_shape_matches_the_legacy_shape(self):
        self.assert_parity(self.LITELLM_SPEND, facade.empty_spend("project-7-202609"))

    def test_usage_detail_zero_shape_matches_the_legacy_shape(self):
        self.assert_parity(self.LITELLM_DETAIL, facade.empty_usage_detail("project-7-202609"))

    def test_period_is_the_current_utc_month(self):
        assert facade.empty_spend()["period"] == facade.current_period()
        assert len(facade.current_period()) == 6

    def test_tag_defaults_to_empty_rather_than_none(self):
        # The UI renders the tag; None would print "None".
        assert facade.empty_spend()["tag"] == ""
        assert facade.empty_usage_detail()["tag"] == ""

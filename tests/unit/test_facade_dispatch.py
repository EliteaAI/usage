"""Facade reads: every spend RPC answers with the zero shape and touches no other plugin.

The shapes matter more than the values here — they are the contract the Usage page consumes,
and #6574 fills them in from usage_event/usage_counter behind these same signatures.
"""
import pytest

from usage.methods import mode as mode_module
from usage.rpc import facade

from fixtures.helpers import bind, fake_module
from fixtures.stubs import RecordingRpc


# name -> (kwargs the caller passes, zero shape factory)
SPEND_RPCS = {
    "usage_get_project_spend": ({"project_id": 7}, facade.empty_spend),
    "usage_get_user_spend": ({"project_id": 7, "user_id": 42}, facade.empty_spend),
    "usage_get_project_usage_detail": ({"project_id": 7}, facade.empty_usage_detail),
    "usage_get_user_usage_detail": (
        {"project_id": 7, "user_id": 42}, facade.empty_usage_detail,
    ),
}

MAP_RPCS = {
    "usage_get_projects_spend": ({"project_ids": [1, 2]}, {1: 0.0, 2: 0.0}),
    "usage_get_users_spend": ({"project_id": 7, "user_ids": [4, 5]}, {4: 0.0, 5: 0.0}),
}

ALL_RPCS = {name: kwargs for name, (kwargs, _) in SPEND_RPCS.items()}
ALL_RPCS.update({name: kwargs for name, (kwargs, _) in MAP_RPCS.items()})
ALL_RPCS["usage_list_member_spend"] = {"project_id": 7, "period": None}


@pytest.fixture
def instance(monkeypatch):
    """A facade bound to a Module stand-in, with a recording rpc_manager in place."""
    def build(config=None, rpc=None):
        stand_in = fake_module(config={"usage": config or {}})
        bound = bind(stand_in, mode_module.Method, facade.RPC)
        # raising=False: the facade has no `context` import any more, and that is the point --
        # a reintroduced delegation would find this stub and be caught by the assertions below.
        monkeypatch.setattr(
            facade, "context", type("Ctx", (), {"rpc_manager": rpc or RecordingRpc()})(),
            raising=False,
        )
        stand_in.context.rpc_manager = rpc or RecordingRpc()
        return bound
    #
    return build


class TestNoDelegation:
    @pytest.mark.parametrize("name", sorted(ALL_RPCS))
    @pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
    def test_no_other_plugin_is_called_in_any_mode(self, instance, name, mode):
        rpc = RecordingRpc()
        bound = instance({"mode": mode}, rpc)
        #
        getattr(bound, name)(**ALL_RPCS[name])
        #
        assert rpc.names() == []


class TestZeroShapes:
    @pytest.mark.parametrize("name", sorted(SPEND_RPCS))
    def test_spend_reads_are_unavailable(self, instance, name):
        kwargs, factory = SPEND_RPCS[name]
        bound = instance()
        #
        result = getattr(bound, name)(**kwargs)
        #
        assert result == factory()
        assert result["available"] is False

    @pytest.mark.parametrize("name", sorted(MAP_RPCS))
    def test_map_rpcs_return_a_zeroed_key_per_requested_id(self, instance, name):
        kwargs, expected = MAP_RPCS[name]
        #
        assert getattr(instance(), name)(**kwargs) == expected

    def test_member_spend_returns_none_for_the_unreachable_contract(self, instance):
        assert instance().usage_list_member_spend(project_id=7) is None


class TestShapeParity:
    """The tests that make "callers are unchanged" mean something."""

    # Recorded from the legacy LiteLLM tag spend / usage detail payloads.
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

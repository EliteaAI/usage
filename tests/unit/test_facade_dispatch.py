"""Facade reads: every spend RPC delegates to its own plugin's method and touches no other.

The zero shapes still matter — they are the contract the Usage page consumes, and a failed
read in methods/spend.py answers with them rather than raising.
"""
import pytest

from usage.methods import mode as mode_module
from usage.rpc import facade

from fixtures.helpers import bind, fake_module
from fixtures.stubs import RecordingRpc


# rpc name -> kwargs the caller passes
ALL_RPCS = {
    "usage_get_project_spend": {"project_id": 7},
    "usage_get_user_spend": {"project_id": 7, "user_id": 42},
    "usage_get_project_usage_detail": {"project_id": 7},
    "usage_get_user_usage_detail": {"project_id": 7, "user_id": 42},
    "usage_get_projects_spend": {"project_ids": [1, 2]},
    "usage_get_users_spend": {"project_id": 7, "user_ids": [4, 5]},
    "usage_list_member_spend": {"project_id": 7, "period": None},
}

# The RPC keeps the public name; the method it delegates to is named differently on purpose,
# because pylon rejects a registry name that is claimed twice.
METHOD_OF = {
    "usage_get_project_spend": "usage_read_project_spend",
    "usage_get_user_spend": "usage_read_user_spend",
    "usage_get_project_usage_detail": "usage_read_project_usage_detail",
    "usage_get_user_usage_detail": "usage_read_user_usage_detail",
    "usage_get_projects_spend": "usage_read_projects_spend",
    "usage_get_users_spend": "usage_read_users_spend",
    "usage_list_member_spend": "usage_read_member_spend_listing",
}


class RecordingMethods:
    """Stands in for spend.Method: records the call instead of running SQL."""

    def __init__(self):
        self.calls = []

    def __call__(self, name):
        def method(**kwargs):
            self.calls.append((name, kwargs))
            return "delegated"
        #
        return method


@pytest.fixture
def instance(monkeypatch):
    """A facade bound to a Module stand-in, with recording methods and rpc_manager in place."""
    def build(config=None, rpc=None, methods=None):
        methods = methods if methods is not None else RecordingMethods()
        stand_in = fake_module(config={"usage": config or {}})
        bound = bind(stand_in, mode_module.Method, facade.RPC)
        #
        for name in ALL_RPCS:
            setattr(bound, METHOD_OF[name], methods(METHOD_OF[name]))
        #
        # raising=False: the facade has no `context` import any more, and that is the point --
        # a reintroduced delegation would find this stub and be caught by the assertions below.
        monkeypatch.setattr(
            facade, "context", type("Ctx", (), {"rpc_manager": rpc or RecordingRpc()})(),
            raising=False,
        )
        stand_in.context.rpc_manager = rpc or RecordingRpc()
        bound.recorded = methods
        #
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
        getattr(bound, f"{name}_rpc")(**ALL_RPCS[name])
        #
        assert rpc.names() == []


class TestDelegation:
    """The _rpc suffix is what keeps the RPC from overwriting the method it calls."""

    @pytest.mark.parametrize("name", sorted(ALL_RPCS))
    def test_the_rpc_calls_its_own_method(self, instance, name):
        bound = instance()
        #
        result = getattr(bound, f"{name}_rpc")(**ALL_RPCS[name])
        #
        assert result == "delegated"
        assert bound.recorded.calls[0][0] == METHOD_OF[name]

    @pytest.mark.parametrize("name", sorted(ALL_RPCS))
    def test_every_caller_kwarg_reaches_the_method(self, instance, name):
        bound = instance()
        #
        getattr(bound, f"{name}_rpc")(**ALL_RPCS[name])
        #
        assert bound.recorded.calls[0][1] == ALL_RPCS[name]

    @pytest.mark.parametrize("name", sorted(ALL_RPCS))
    def test_the_attribute_name_differs_from_the_registered_rpc_name(self, name):
        # Same name on both classes and the later bind wins, so the RPC would call itself
        assert hasattr(facade.RPC, f"{name}_rpc")
        assert not hasattr(facade.RPC, name)

    @pytest.mark.parametrize("name", sorted(ALL_RPCS))
    def test_the_method_name_is_not_the_rpc_name(self, name):
        # pylon raises "Name '<x>' is already set" when a method and an RPC claim one name
        assert METHOD_OF[name] != name


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
        "daily": [
            {"date": "2026-09-01", "spend": 3.5, "total_tokens": 160, "api_requests": 2},
        ],
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

    def test_a_failed_read_is_the_only_unavailable_answer(self):
        # available False means the query failed, not that the project had no traffic
        assert facade.empty_spend()["available"] is False
        assert facade.empty_usage_detail()["available"] is False

"""Budget warning thresholds: when the UI banner fires, and for which scope.

These moved out of runtime_interface_litellm so that any inference plane gets the same warning;
the cases below are the ones that governed behaviour there, restated against the usage gate.
"""
import pytest

from usage.methods import mode as mode_module
from usage.methods import warning as warning_module

from fixtures.helpers import bind, fake_module


class FakeRedis:
    """Only hget is exercised — the warning path reads, never writes."""

    def __init__(self, hashes=None):
        self.hashes = hashes or {}

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)


def module_with(config, limits=None, hashes=None):
    """A Module stand-in wired with a usage config block, a limits ladder and Redis contents."""
    instance = bind(
        fake_module(config={"usage": config}), mode_module.Method, warning_module.Method,
    )
    instance.usage_gate_limits = lambda project_id, user_id=None: limits or {"enabled": False}
    instance.usage_redis_client = lambda: FakeRedis(hashes)
    #
    return instance


@pytest.fixture(autouse=True)
def clear_cache():
    """The warning cache is module-level and would leak state between tests."""
    warning_module._warning_cache.clear()  # pylint: disable=W0212


class TestThresholdConfig:
    @pytest.mark.parametrize("scope", ["project", "personal_project", "user"])
    def test_default_when_nothing_configured(self, scope):
        assert module_with({}).usage_get_warning_threshold(scope) == 80

    def test_each_scope_reads_its_own_key(self):
        instance = module_with({"warning_thresholds": {
            "project_pct": 60, "personal_project_pct": 70, "user_pct": 90,
        }})
        #
        assert instance.usage_get_warning_threshold("project") == 60
        assert instance.usage_get_warning_threshold("personal_project") == 70
        assert instance.usage_get_warning_threshold("user") == 90

    @pytest.mark.parametrize("value", [0, -5, 101, "abc", None])
    def test_out_of_range_or_unparseable_falls_back_to_the_default(self, value):
        # A bad config must degrade to the previous behaviour, never silence warnings.
        instance = module_with({"warning_thresholds": {"project_pct": value}})
        #
        assert instance.usage_get_warning_threshold("project") == 80

    def test_unknown_scope_gets_the_default(self):
        assert module_with({}).usage_get_warning_threshold("nonsense") == 80


class TestWarningForScope:
    def test_below_threshold_is_silent(self):
        instance = module_with({})
        #
        assert instance.usage_warning_for_scope("project", 700, 1000, 80) is None

    def test_at_threshold_warns(self):
        state = module_with({}).usage_warning_for_scope("project", 800, 1000, 80)
        #
        assert state == {
            "scope": "project", "percent_used": 80, "warning_pct": 80, "level": 80,
            "should_warn": True,
        }

    def test_at_or_above_the_limit_is_silent(self):
        # The 429 refusal is the message at that point; a stale reading must not contradict it.
        instance = module_with({})
        #
        assert instance.usage_warning_for_scope("project", 1000, 1000, 80) is None
        assert instance.usage_warning_for_scope("project", 1500, 1000, 80) is None

    def test_zero_limit_has_nothing_to_warn_about(self):
        assert module_with({}).usage_warning_for_scope("project", 0, 0, 80) is None


class TestWarningLevels:
    """Each level re-shows a banner the user dismissed at a lower one, so the steps matter."""

    @pytest.mark.parametrize("pct,expected", [
        (79.9, None), (80, 80), (89.9, 80), (90, 90), (94.9, 90), (95, 95), (99.9, 95),
    ])
    def test_default_threshold_escalates_at_ninety_and_ninety_five(self, pct, expected):
        assert warning_module.warning_level(pct, 80) == expected

    @pytest.mark.parametrize("pct,expected", [(91, None), (92, 92), (94, 92), (95, 95)])
    def test_threshold_above_ninety_skips_the_ninety_level(self, pct, expected):
        # 90 is below the configured threshold, so it must never surface as a level.
        assert warning_module.warning_level(pct, 92) == expected

    def test_threshold_above_ninety_five_is_the_only_level(self):
        assert warning_module.warning_level(96, 97) is None
        assert warning_module.warning_level(99, 97) == 97

    def test_level_travels_in_the_payload(self):
        state = module_with({}).usage_warning_for_scope("project", 960, 1000, 80)
        #
        assert state["level"] == 95 and state["percent_used"] == 96

    def test_shown_percent_never_claims_an_unreached_level(self):
        # Found in E2E: 94.55% rounded to "95%" while the banner stayed at the amber 90 level,
        # and 99.6% would have read "100%" on a warning that only shows below the limit.
        state = module_with({}).usage_warning_for_scope("project", 9455, 10000, 80)
        assert state["level"] == 90 and state["percent_used"] == 94
        near_limit = module_with({}).usage_warning_for_scope("project", 996, 1000, 80)
        assert near_limit["percent_used"] == 99

    def test_threshold_of_one_hundred_never_warns(self):
        # At 100% the refusal is the message, so a 100% threshold is effectively "off".
        assert module_with({}).usage_warning_for_scope("project", 999, 1000, 100) is None

    def test_no_warning_carries_an_empty_level(self):
        assert warning_module.NO_WARNING["level"] is None


class TestDismissibleFlag:
    def test_default_is_dismissible(self):
        assert module_with({}).usage_get_warnings_dismissible() is True

    def test_explicit_false_disables_dismissal(self):
        instance = module_with({"warnings": {"dismissible": False}})
        #
        assert instance.usage_get_warnings_dismissible() is False

    @pytest.mark.parametrize("warnings", [None, {}, {"dismissible": None}, {"dismissible": True}])
    def test_anything_but_false_stays_dismissible(self, warnings):
        # A malformed block must not strip the close button from every user's banner.
        assert module_with({"warnings": warnings}).usage_get_warnings_dismissible() is True


class TestResolveWarning:
    LIMITS = {"enabled": True, "project_limit_nano": 1000, "member_limit_nano": 1000}

    def test_observe_mode_never_warns(self):
        instance = module_with(
            {"mode": "observe"}, self.LIMITS, {"usage:ctr:p:7:202609": {"counter": "900"}},
        )
        #
        assert instance.usage_resolve_budget_warning(7) == warning_module.NO_WARNING

    def test_disabled_budgets_never_warn(self):
        instance = module_with({"mode": "enforce"}, {"enabled": False})
        #
        assert instance.usage_resolve_budget_warning(7) == warning_module.NO_WARNING

    def test_project_scope_warns(self, monkeypatch):
        monkeypatch.setattr(warning_module, "project_hash_key", lambda pid, moment: "P")
        instance = module_with({"mode": "enforce"}, self.LIMITS, {"P": {"counter": "850"}})
        #
        state = instance.usage_resolve_budget_warning(7)
        #
        assert state["should_warn"] is True
        assert state["scope"] == "project"
        assert state["percent_used"] == 85

    def test_reserved_does_not_count_towards_the_percentage(self, monkeypatch):
        # The banner tracks money spent; in-flight reservations may never be billed
        monkeypatch.setattr(warning_module, "project_hash_key", lambda pid, moment: "P")
        instance = module_with(
            {"mode": "enforce"}, self.LIMITS, {"P": {"counter": "400", "reserved": "450"}},
        )
        #
        assert instance.usage_resolve_budget_warning(7) == warning_module.NO_WARNING

    def test_higher_level_scope_wins(self, monkeypatch):
        # Member sits at 80 while the project escalates to 95: returning the member state would
        # let a member-level dismissal hide the project escalation that is about to block them.
        monkeypatch.setattr(warning_module, "project_hash_key", lambda pid, moment: "P")
        monkeypatch.setattr(warning_module, "member_hash_key", lambda pid, uid, moment: "M")
        instance = module_with(
            {"mode": "enforce"}, self.LIMITS, {"P": {"counter": "990"}, "M": {"counter": "850"}},
        )
        #
        state = instance.usage_resolve_budget_warning(7, 3)
        assert state["scope"] == "project" and state["level"] == 95

    def test_member_scope_wins_a_tie(self, monkeypatch):
        # Same level on both: member is the budget that stops this user specifically.
        monkeypatch.setattr(warning_module, "project_hash_key", lambda pid, moment: "P")
        monkeypatch.setattr(warning_module, "member_hash_key", lambda pid, uid, moment: "M")
        instance = module_with(
            {"mode": "enforce"}, self.LIMITS, {"P": {"counter": "860"}, "M": {"counter": "850"}},
        )
        #
        assert instance.usage_resolve_budget_warning(7, 3)["scope"] == "member"

    def test_member_alone_still_warns(self, monkeypatch):
        monkeypatch.setattr(warning_module, "project_hash_key", lambda pid, moment: "P")
        monkeypatch.setattr(warning_module, "member_hash_key", lambda pid, uid, moment: "M")
        instance = module_with(
            {"mode": "enforce"}, self.LIMITS, {"P": {"counter": "100"}, "M": {"counter": "910"}},
        )
        #
        state = instance.usage_resolve_budget_warning(7, 3)
        assert state["scope"] == "member" and state["level"] == 90

    def test_personal_project_reads_its_own_threshold(self, monkeypatch):
        monkeypatch.setattr(warning_module, "project_hash_key", lambda pid, moment: "P")
        limits = dict(self.LIMITS, is_personal_project=True)
        instance = module_with(
            {"mode": "enforce", "warning_thresholds": {
                "project_pct": 90, "personal_project_pct": 80,
            }},
            limits, {"P": {"counter": "850"}},
        )
        #
        assert instance.usage_resolve_budget_warning(7)["warning_pct"] == 80

    def test_unlimited_scope_is_skipped(self, monkeypatch):
        monkeypatch.setattr(warning_module, "project_hash_key", lambda pid, moment: "P")
        instance = module_with(
            {"mode": "enforce"},
            {"enabled": True, "project_limit_nano": None},
            {"P": {"counter": "999999"}},
        )
        #
        assert instance.usage_resolve_budget_warning(7) == warning_module.NO_WARNING

    def test_a_broken_limits_ladder_degrades_to_no_warning(self, recording_log):
        instance = module_with({"mode": "enforce"})
        instance.usage_gate_limits = lambda project_id, user_id=None: 1 / 0
        #
        assert instance.usage_resolve_budget_warning(7) == warning_module.NO_WARNING
        assert recording_log.messages("exception")


class TestCaching:
    def test_second_call_is_served_from_the_cache(self, monkeypatch):
        monkeypatch.setattr(warning_module, "project_hash_key", lambda pid, moment: "P")
        instance = module_with(
            {"mode": "enforce"},
            {"enabled": True, "project_limit_nano": 1000},
            {"P": {"counter": "850"}},
        )
        calls = []
        original = instance.usage_resolve_budget_warning
        instance.usage_resolve_budget_warning = lambda *args: (calls.append(args), original(*args))[1]
        #
        first = instance.usage_get_budget_warning_state(7)
        second = instance.usage_get_budget_warning_state(7)
        #
        assert first == second
        assert len(calls) == 1

    def test_project_and_member_are_cached_separately(self, monkeypatch):
        monkeypatch.setattr(warning_module, "project_hash_key", lambda pid, moment: "P")
        monkeypatch.setattr(warning_module, "member_hash_key", lambda pid, uid, moment: "M")
        instance = module_with(
            {"mode": "enforce"},
            {"enabled": True, "project_limit_nano": 1000, "member_limit_nano": 1000},
            {"P": {"counter": "100"}, "M": {"counter": "850"}},
        )
        #
        assert instance.usage_get_budget_warning_state(7)["should_warn"] is False
        assert instance.usage_get_budget_warning_state(7, 3)["should_warn"] is True

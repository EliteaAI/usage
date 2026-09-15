"""Mode normalization and the gating predicates."""
import pytest

from usage.methods import mode as mode_module

from fixtures.helpers import bind, fake_module


def module_with(config):
    """A Module stand-in whose descriptor config holds the given usage block."""
    return bind(fake_module(config={"usage": config}), mode_module.Method)


class TestNormalizeMode:
    def test_absent_config_is_off(self):
        assert mode_module.normalize_mode(None) == mode_module.MODE_OFF

    def test_explicit_off(self):
        assert mode_module.normalize_mode("off") == mode_module.MODE_OFF

    @pytest.mark.parametrize("value", ["off", "observe", "enforce"])
    def test_valid_values_round_trip(self, value):
        assert mode_module.normalize_mode(value) == value

    def test_case_and_whitespace_tolerated(self):
        assert mode_module.normalize_mode("  Enforce ") == mode_module.MODE_ENFORCE

    def test_unknown_value_falls_back_and_warns(self, recording_log):
        # An operator typo must not brick startup, but it must be visible in the log.
        assert mode_module.normalize_mode("enfrce") == mode_module.MODE_OFF
        assert any("Unknown usage.mode" in message for message in recording_log.messages("warning"))

    def test_yaml_false_is_off_not_the_string_false(self):
        # Unquoted `mode: off` parses as boolean False in YAML 1.1 — the reason config.yml quotes it.
        assert mode_module.normalize_mode(False) == mode_module.MODE_OFF

    def test_yaml_true_is_off_and_warns(self, recording_log):
        assert mode_module.normalize_mode(True) == mode_module.MODE_OFF
        assert recording_log.messages("warning")


class TestPredicates:
    @pytest.mark.parametrize("config,expected", [
        ({}, "off"),
        ({"mode": "off"}, "off"),
        ({"mode": "observe"}, "observe"),
        ({"mode": "enforce"}, "enforce"),
        ({"mode": "nonsense"}, "off"),
    ])
    def test_mode_truth_table(self, config, expected):
        instance = module_with(config)
        #
        assert instance.usage_get_mode() == expected
        assert instance.usage_is_enabled() is (expected != "off")
        assert instance.usage_is_observing() is (expected == "observe")
        assert instance.usage_is_enforcing() is (expected == "enforce")

    def test_empty_usage_block_is_off(self):
        instance = bind(fake_module(config={"usage": None}), mode_module.Method)
        #
        assert instance.usage_get_mode() == "off"

    def test_config_is_reread_so_reconfig_takes_effect(self):
        instance = module_with({"mode": "off"})
        assert instance.usage_get_mode() == "off"
        #
        instance.descriptor.config["usage"]["mode"] = "enforce"
        assert instance.usage_get_mode() == "enforce"

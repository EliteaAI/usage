"""metadata.json / config.yml / admin_schema.json consistency.

Drift between an admin flag's path and the config it claims to edit is invisible until an
operator flips a switch that writes nowhere. Cheap to pin here.
"""
import json
import pathlib

import pytest

SIBLING_PLUGINS = pathlib.Path(__file__).resolve().parents[3]

EXPECTED_PROPERTIES = {
    "usage_mode", "usage_spend_source", "usage_retention_months",
    "usage_partition_ahead_months",
}


def resolve(config, path):
    """Follow a dotted admin-schema path through the parsed config."""
    node = config
    #
    for part in path.split("."):
        assert isinstance(node, dict) and part in node, f"{path} does not resolve"
        node = node[part]
    #
    return node


class TestMetadata:

    def test_has_the_required_fields(self, plugin_metadata):
        assert plugin_metadata["name"] == "usage"
        assert plugin_metadata["module"] == "plugins.usage"
        assert plugin_metadata["version"]

    def test_depends_on_shared_for_the_db_layer(self, plugin_metadata):
        assert plugin_metadata["depends_on"] == ["shared"]

    def test_init_after_names_the_plugins_it_reads(self, plugin_metadata):
        """costs prices events and scheduling carries the partition cron.

        No runtime_interface_* is named on purpose: hooks resolve lazily at call time and pylon
        inits every module before any ready() runs, so ordering against an interface buys nothing
        while making an interface-neutral contract look litellm-specific.
        """
        init_after = plugin_metadata["init_after"]
        #
        assert "costs" in init_after
        assert "scheduling" in init_after
        assert not [name for name in init_after if name.startswith("runtime_interface")]

    def test_does_not_depend_on_an_interface_plugin(self, plugin_metadata):
        """Hooks resolve lazily at call time, so there is no hard coupling in either direction."""
        assert "runtime_interface_litellm" not in plugin_metadata["depends_on"]


class TestTheInterfaceDeclaresTheHooks:
    """enforce mode refuses an interface that does not declare usage_hooks.

    The flag is how usage knows an interface calls the hooks at all, so a sibling that meters
    traffic while forgetting the flag would be refused in enforce and look unmetered in the
    diagnostics. Skipped where the sibling is not installed.
    """

    def test_litellm_declares_usage_hooks(self):
        metadata_path = SIBLING_PLUGINS / "runtime_interface_litellm" / "metadata.json"
        #
        if not metadata_path.exists():
            pytest.skip("runtime_interface_litellm is not installed next to usage")
        #
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        #
        assert metadata.get("usage_hooks") is True


class TestConfigDefaults:

    def test_mode_is_off_out_of_the_box(self, plugin_config):
        """Installing the plugin must change no behaviour until an operator opts in."""
        assert plugin_config["usage"]["mode"] == "off"

    def test_mode_is_a_string_not_a_yaml_boolean(self, plugin_config):
        """Unquoted `off` is boolean false in YAML 1.1; the file must keep it quoted."""
        assert isinstance(plugin_config["usage"]["mode"], str)

    def test_spend_source_follows_the_mode_by_default(self, plugin_config):
        assert plugin_config["usage"]["spend_source"] == "auto"

    def test_partition_lookahead_is_at_least_one_month(self, plugin_config):
        """A month boundary crossed between crons must not land rows with no partition."""
        assert plugin_config["usage"]["partition_ahead_months"] >= 1

    def test_retention_is_declared(self, plugin_config):
        assert plugin_config["usage"]["retention_months"] == 13


class TestAdminSchema:

    def test_targets_this_plugin(self, admin_schema):
        assert admin_schema["plugin"] == "usage"

    def test_exposes_exactly_the_expected_properties(self, admin_schema):
        assert set(admin_schema["properties"]) == EXPECTED_PROPERTIES

    def test_every_path_resolves_in_config(self, admin_schema, plugin_config):
        for name, prop in admin_schema["properties"].items():
            assert "path" in prop, name
            resolve(plugin_config, prop["path"])

    def test_every_default_matches_the_config_value(self, admin_schema, plugin_config):
        for name, prop in admin_schema["properties"].items():
            assert prop["default"] == resolve(plugin_config, prop["path"]), name

    def test_every_property_declares_requires_restart(self, admin_schema):
        for name, prop in admin_schema["properties"].items():
            assert "requires_restart" in prop, name

    def test_nothing_requires_a_restart(self, admin_schema):
        """Every knob here is read per call, so flipping one must not need a pylon restart."""
        for name, prop in admin_schema["properties"].items():
            assert prop["requires_restart"] is False, name

    def test_every_property_is_sectioned(self, admin_schema):
        for name, prop in admin_schema["properties"].items():
            assert prop.get("section") == "Usage", name

    def test_visible_when_references_a_sibling_property(self, admin_schema):
        properties = admin_schema["properties"]
        #
        for name, prop in properties.items():
            visible_when = prop.get("visible_when")
            #
            if visible_when is None:
                continue
            #
            assert visible_when["field"] in properties, name

    @pytest.mark.parametrize("name,values", [
        ("usage_mode", ["off", "observe", "enforce"]),
        ("usage_spend_source", ["auto", "litellm", "elitea"]),
    ])
    def test_enums_match_the_code_constants(self, admin_schema, name, values):
        from usage.methods import mode as mode_module  # pylint: disable=C0415
        #
        assert admin_schema["properties"][name]["enum"] == values
        assert list(mode_module.MODES if name == "usage_mode" else mode_module.SOURCES) == values

    def test_numeric_properties_carry_bounds(self, admin_schema):
        for name in ("usage_retention_months", "usage_partition_ahead_months"):
            value_schema = admin_schema["properties"][name]["value_schema"]
            #
            assert value_schema["minimum"] <= admin_schema["properties"][name]["default"]
            assert value_schema["maximum"] >= admin_schema["properties"][name]["default"]

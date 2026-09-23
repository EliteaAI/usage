"""#6699 — the two mandatory crons must stay non-editable in the Admin Portal.

`MANAGED_SCHEDULES` is what `module.get_managed_schedules()` hands to scheduling's registry;
a binding missing `rpc_func` raises there and is silently swallowed per plugin, leaving the row
editable, so the shape checks below are load-bearing, not decorative.
"""
import ast
from pathlib import Path

from usage.schedule_bindings import MANAGED_SCHEDULES

PLUGIN_ROOT = Path(__file__).parents[2]

# scheduling's PENDING_BINDING marker — a binding that looks like this is not really managed
PENDING_BINDING = {"section": None, "fields": []}


def _register_cron_payloads():
    """The literal dicts passed to scheduling_create_if_not_exists inside module.py."""
    tree = ast.parse((PLUGIN_ROOT / "module.py").read_text())
    #
    payloads = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "scheduling_create_if_not_exists"
        ):
            (arg,) = node.args
            payloads.append(ast.literal_eval(arg))
    #
    return payloads


class TestShape:
    def test_every_entry_has_exactly_managed_by_and_rpc_func(self):
        for entry in MANAGED_SCHEDULES.values():
            assert set(entry) == {"managed_by", "rpc_func"}

    def test_managed_by_names_a_section_and_at_least_one_field(self):
        for entry in MANAGED_SCHEDULES.values():
            managed_by = entry["managed_by"]
            #
            assert managed_by["section"] is not None
            assert managed_by["fields"]

    def test_no_entry_is_the_pending_binding_marker(self):
        for entry in MANAGED_SCHEDULES.values():
            assert entry["managed_by"] != PENDING_BINDING


class TestMatchesTheRegisteredCrons:
    def test_every_managed_name_has_a_registered_cron(self):
        names = {payload["name"] for payload in _register_cron_payloads()}
        #
        assert set(MANAGED_SCHEDULES) <= names

    def test_the_rpc_func_matches_character_for_character(self):
        by_name = {payload["name"]: payload["rpc_func"] for payload in _register_cron_payloads()}
        #
        for name, entry in MANAGED_SCHEDULES.items():
            assert entry["rpc_func"] == by_name[name]

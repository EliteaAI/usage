"""Runtime-interface enumeration and its startup diagnostics.

Explicit hook call sites cost one thing: a future interface plugin can forget them and serve
traffic unmetered. These diagnostics are how that is caught, so they are worth real tests even
though nothing is metered yet.

Enumeration reads the loaded descriptors rather than asking interfaces to register themselves —
an opt-in list would silently under-report whichever plugin forgot to join it.
"""
import types

from fixtures.helpers import bind, fake_module
from usage.methods import interfaces, mode as mode_module


def fake_descriptor(name, url_prefix=None, usage_hooks=False):
    """A loaded-plugin descriptor stand-in, shaped like pylon's."""
    return types.SimpleNamespace(
        name=name,
        metadata={"name": name, "usage_hooks": usage_hooks},
        config={"url_prefix": url_prefix} if url_prefix is not None else {},
    )


def build(config=None, **descriptors):
    """A Module stand-in with the mode and interfaces mixins bound, as pylon binds them."""
    instance = fake_module(config={"usage": config if config is not None else {}})
    instance.context = types.SimpleNamespace(
        module_manager=types.SimpleNamespace(descriptors=descriptors),
    )
    #
    return bind(instance, mode_module.Method, interfaces.Method)


class TestEnumeration:

    def test_an_interface_is_listed_with_its_url_prefix(self):
        instance = build(
            runtime_interface_litellm=fake_descriptor(
                "runtime_interface_litellm", "/llm", usage_hooks=True,
            ),
        )
        #
        assert instance.usage_list_interfaces() == [
            {"name": "runtime_interface_litellm", "url_prefix": "/llm", "usage_hooks": True},
        ]

    def test_a_missing_url_prefix_is_none_rather_than_a_crash(self):
        instance = build(runtime_interface_custom=fake_descriptor("runtime_interface_custom"))
        #
        assert instance.usage_list_interfaces()[0]["url_prefix"] is None

    def test_no_interface_plugin_is_an_empty_list(self):
        """A pylon with no interface plugin at all is a valid deployment."""
        assert build().usage_list_interfaces() == []

    def test_unrelated_plugins_are_not_listed(self):
        instance = build(elitea_core=fake_descriptor("elitea_core"), costs=fake_descriptor("costs"))
        #
        assert instance.usage_list_interfaces() == []

    def test_several_interfaces_are_all_listed(self):
        instance = build(
            runtime_interface_litellm=fake_descriptor(
                "runtime_interface_litellm", "/llm", usage_hooks=True,
            ),
            runtime_interface_custom=fake_descriptor("runtime_interface_custom", "/custom"),
        )
        #
        assert sorted(record["name"] for record in instance.usage_list_interfaces()) == [
            "runtime_interface_custom", "runtime_interface_litellm",
        ]

    def test_an_unavailable_module_manager_is_tolerated(self):
        instance = build()
        instance.context = types.SimpleNamespace()
        #
        assert instance.usage_list_interfaces() == []


class TestHookDeclaration:
    """Whether an interface declares usage_hooks decides what happens in each mode."""

    def test_declared_hooks_are_logged_at_info(self, recording_log):
        instance = build(
            {"mode": "observe"},
            runtime_interface_litellm=fake_descriptor(
                "runtime_interface_litellm", "/llm", usage_hooks=True,
            ),
        )
        #
        assert instance.usage_report_interfaces() == []
        assert any("declares usage hooks" in message for message in recording_log.messages("info"))

    def test_observe_warns_that_traffic_is_unmetered(self, recording_log):
        instance = build(
            {"mode": "observe"},
            runtime_interface_custom=fake_descriptor("runtime_interface_custom", "/custom"),
        )
        #
        assert instance.usage_report_interfaces() == []
        assert any("unmetered" in message for message in recording_log.messages("warning"))

    def test_enforce_records_the_interface_as_refused(self, recording_log):
        instance = build(
            {"mode": "enforce"},
            runtime_interface_custom=fake_descriptor("runtime_interface_custom", "/custom"),
        )
        #
        assert instance.usage_report_interfaces() == ["runtime_interface_custom"]
        assert any("unmetered and ungated" in message
                   for message in recording_log.messages("error"))

    def test_off_mode_is_informational_only(self, recording_log):
        instance = build(
            {"mode": "off"},
            runtime_interface_custom=fake_descriptor("runtime_interface_custom", "/custom"),
        )
        #
        assert instance.usage_report_interfaces() == []
        assert recording_log.messages("error") == []
        assert recording_log.messages("warning") == []

    def test_absent_metadata_reads_as_not_declared(self):
        descriptor = fake_descriptor("runtime_interface_custom", "/custom")
        descriptor.metadata = None
        instance = build({"mode": "enforce"}, runtime_interface_custom=descriptor)
        #
        assert instance.usage_report_interfaces() == ["runtime_interface_custom"]

    def test_only_the_undeclared_interface_is_refused(self):
        instance = build(
            {"mode": "enforce"},
            runtime_interface_litellm=fake_descriptor(
                "runtime_interface_litellm", "/llm", usage_hooks=True,
            ),
            runtime_interface_custom=fake_descriptor("runtime_interface_custom", "/custom"),
        )
        #
        assert instance.usage_report_interfaces() == ["runtime_interface_custom"]


class TestNoInterfaces:

    def test_report_does_not_raise_and_says_so(self, recording_log):
        assert build().usage_report_interfaces() == []
        assert any(
            "no runtime interfaces loaded" in message
            for message in recording_log.messages("info")
        )

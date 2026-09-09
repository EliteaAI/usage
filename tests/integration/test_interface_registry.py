"""The runtime-interface registry and its startup diagnostics.

Explicit hook call sites cost one thing: a future interface plugin can forget them and serve
traffic unmetered. These diagnostics are how that is caught, so they are worth real tests even
though nothing is metered yet.
"""
import sys
import types

import pytest

from fixtures.helpers import bind, fake_module
from usage.methods import interfaces, mode as mode_module


def fake_interface(name, url_prefix=None, usage_hooks=False):
    """An interface plugin stand-in shaped like a pylon Module."""
    return types.SimpleNamespace(
        descriptor=types.SimpleNamespace(
            name=name,
            metadata={"name": name, "usage_hooks": usage_hooks},
            config={"url_prefix": url_prefix} if url_prefix is not None else {},
        ),
    )


@pytest.fixture
def registry(monkeypatch):
    """Replace tools.runtime_interfaces for the duration of one test."""
    def install(*entries):
        monkeypatch.setattr(sys.modules["tools"], "runtime_interfaces", list(entries), raising=False)
    #
    return install


def build(config=None, descriptors=None):
    """A Module stand-in with the mode and interfaces mixins bound, as pylon binds them."""
    instance = fake_module(config={"usage": config if config is not None else {}})
    instance.context = types.SimpleNamespace(
        module_manager=types.SimpleNamespace(descriptors=descriptors or {}),
    )
    #
    return bind(instance, mode_module.Method, interfaces.Method)


class TestEnumeration:

    def test_an_interface_is_listed_with_its_url_prefix(self, registry):
        registry(fake_interface("runtime_interface_litellm", "/llm", usage_hooks=True))
        instance = build()
        #
        assert instance.usage_list_interfaces() == [
            {"name": "runtime_interface_litellm", "url_prefix": "/llm", "usage_hooks": True},
        ]

    def test_a_missing_url_prefix_is_none_rather_than_a_crash(self, registry):
        registry(fake_interface("runtime_interface_custom"))
        instance = build()
        #
        assert instance.usage_list_interfaces()[0]["url_prefix"] is None

    def test_an_absent_registry_is_an_empty_list(self, monkeypatch):
        """A pylon with no interface plugin at all is a valid deployment."""
        monkeypatch.delattr(sys.modules["tools"], "runtime_interfaces", raising=False)
        instance = build()
        #
        assert instance.usage_list_interfaces() == []

    def test_several_interfaces_are_all_listed(self, registry):
        registry(
            fake_interface("runtime_interface_litellm", "/llm", usage_hooks=True),
            fake_interface("runtime_interface_custom", "/custom"),
        )
        instance = build()
        #
        assert [record["name"] for record in instance.usage_list_interfaces()] == [
            "runtime_interface_litellm", "runtime_interface_custom",
        ]


class TestHookDeclaration:
    """Whether an interface declares usage_hooks decides what happens in each mode."""

    def test_declared_hooks_are_logged_at_info(self, registry, recording_log):
        registry(fake_interface("runtime_interface_litellm", "/llm", usage_hooks=True))
        #
        assert build({"mode": "observe"}).usage_report_interfaces() == []
        assert any("declares usage hooks" in message for message in recording_log.messages("info"))

    def test_observe_warns_that_traffic_is_unmetered(self, registry, recording_log):
        registry(fake_interface("runtime_interface_custom", "/custom"))
        #
        refused = build({"mode": "observe"}).usage_report_interfaces()
        #
        assert refused == []
        assert any("unmetered" in message for message in recording_log.messages("warning"))

    def test_enforce_records_the_interface_as_refused(self, registry, recording_log):
        registry(fake_interface("runtime_interface_custom", "/custom"))
        #
        refused = build({"mode": "enforce"}).usage_report_interfaces()
        #
        assert refused == ["runtime_interface_custom"]
        assert any("refused service" in message for message in recording_log.messages("error"))

    def test_off_mode_is_informational_only(self, registry, recording_log):
        registry(fake_interface("runtime_interface_custom", "/custom"))
        #
        refused = build({"mode": "off"}).usage_report_interfaces()
        #
        assert refused == []
        assert recording_log.messages("error") == []
        assert recording_log.messages("warning") == []

    def test_absent_metadata_reads_as_not_declared(self, registry):
        interface = fake_interface("runtime_interface_custom", "/custom")
        interface.descriptor.metadata = None
        registry(interface)
        #
        assert build({"mode": "enforce"}).usage_report_interfaces() == ["runtime_interface_custom"]

    def test_only_the_undeclared_interface_is_refused(self, registry):
        registry(
            fake_interface("runtime_interface_litellm", "/llm", usage_hooks=True),
            fake_interface("runtime_interface_custom", "/custom"),
        )
        #
        assert build({"mode": "enforce"}).usage_report_interfaces() == ["runtime_interface_custom"]


class TestForgotToRegister:
    """A plugin named like an interface that never appended itself to the registry."""

    def test_is_reported(self, registry, recording_log):
        registry(fake_interface("runtime_interface_litellm", "/llm", usage_hooks=True))
        instance = build(
            {"mode": "off"},
            descriptors={"runtime_interface_foo": object(), "runtime_interface_litellm": object()},
        )
        #
        assert instance.usage_unregistered_interfaces() == ["runtime_interface_foo"]
        #
        instance.usage_report_interfaces()
        assert any(
            "never registered itself" in message for message in recording_log.messages("warning")
        )

    def test_unrelated_plugins_are_not_reported(self, registry):
        registry()
        instance = build(descriptors={"elitea_core": object(), "costs": object()})
        #
        assert instance.usage_unregistered_interfaces() == []

    def test_an_unavailable_module_manager_is_tolerated(self, registry):
        registry()
        instance = build()
        instance.context = types.SimpleNamespace()
        #
        assert instance.usage_unregistered_interfaces() == []


class TestEmptyRegistry:

    def test_report_does_not_raise_and_says_so(self, registry, recording_log):
        registry()
        #
        assert build().usage_report_interfaces() == []
        assert any(
            "no runtime interfaces registered" in message
            for message in recording_log.messages("info")
        )

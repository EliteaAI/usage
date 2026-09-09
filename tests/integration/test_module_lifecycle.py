"""Module.init/ready/reconfig/deinit against stubs — order and failure tolerance."""
import queue
import types

from fixtures.helpers import bind
from usage import module as module_module
from usage.methods import interfaces, mode as mode_module, partitions


def build(config=None, rpc=None, descriptors=None):
    """A real Module wired to recording stubs, with the mixins bound as pylon would."""
    calls = []
    #
    descriptor = types.SimpleNamespace(
        config={"usage": config if config is not None else {}},
        name="usage",
        init_all=lambda: calls.append(("init_all", None)),
        register_tool=lambda name, tool: calls.append(("register_tool", name)),
    )
    context = types.SimpleNamespace(
        rpc_manager=rpc if rpc is not None else types.SimpleNamespace(
            timeout=lambda _t: types.SimpleNamespace(
                scheduling_create_if_not_exists=lambda payload: calls.append(("cron", payload)),
            ),
        ),
        module_manager=types.SimpleNamespace(descriptors=descriptors or {}),
    )
    #
    instance = module_module.Module(context, descriptor)
    bind(instance, mode_module.Method, partitions.Method, interfaces.Method)
    #
    instance.usage_ensure_partitions = lambda *a, **k: calls.append(("ensure_partitions", None))
    #
    return instance, calls


class TestInit:
    """init() registers the models and the hook tool; it provisions nothing itself."""

    def test_order_is_init_all_then_models_then_tool(self):
        instance, _ = build()
        order = []
        #
        original_init_all = instance.descriptor.init_all
        instance.descriptor.init_all = lambda: (order.append("init_all"), original_init_all())[1]
        original_register = instance.descriptor.register_tool
        instance.descriptor.register_tool = lambda name, tool: (
            order.append(f"register_tool:{name}"), original_register(name, tool),
        )[1]
        #
        instance.init()
        #
        assert order == ["init_all", "register_tool:usage_hooks"]

    def test_models_are_registered_in_the_shared_metadata(self):
        """shared.ready() and the admin create_tables task both read that one metadata."""
        instance, _ = build()
        #
        instance.init()
        #
        from usage.models.usage_counter import UsageCounter
        from usage.models.usage_event import UsageEvent
        #
        metadata = UsageEvent.__table__.metadata
        #
        assert UsageEvent.__table__ in metadata.tables.values()
        assert UsageCounter.__table__ in metadata.tables.values()

    def test_never_provisions_the_schema_itself(self):
        """The apply_shared_metadata flag governs creation only if usage never bypasses it."""
        source = (module_module.__file__ or "")
        #
        with open(source, encoding="utf-8") as handle:
            body = handle.read()
        #
        assert "create_all" not in body

    def test_partitions_are_not_touched_during_init(self):
        """The parent does not exist yet at init() time — shared.ready() creates it."""
        instance, calls = build()
        #
        instance.init()
        #
        assert [name for name, _ in calls if name == "ensure_partitions"] == []

    def test_registers_itself_as_the_hook_provider(self):
        """Interfaces resolve tools.usage_hooks lazily; the tool must be the Module itself."""
        registered = []
        instance, _ = build()
        instance.descriptor.register_tool = lambda name, tool: registered.append((name, tool))
        #
        instance.init()
        #
        assert registered == [("usage_hooks", instance)]

    def test_hooks_are_reachable_on_the_registered_tool(self):
        instance, _ = build()
        #
        assert instance.begin_llm_call(
            project_id=1, user_id=2, model_name="m", endpoint="/v1", headers={},
        ) is None
        marker = iter(())
        assert instance.meter_llm_response(None, None, marker) is marker


class TestReady:
    """ready() is diagnostics plus cron; neither may block startup."""

    def test_ensures_partitions_after_shared_applied_the_metadata(self):
        instance, calls = build()
        #
        instance.ready()
        #
        assert [name for name, _ in calls if name == "ensure_partitions"] == ["ensure_partitions"]

    def test_enumerates_interfaces_and_registers_the_cron(self):
        instance, calls = build()
        reported = []
        instance.usage_report_interfaces = lambda: reported.append("reported") or []
        #
        instance.ready()
        #
        assert reported == ["reported"]
        assert [name for name, _ in calls if name == "cron"] == ["cron"]

    def test_cron_targets_the_registered_rpc_name(self):
        instance, calls = build()
        #
        instance.ready()
        #
        payload = [args for name, args in calls if name == "cron"][0]
        #
        assert payload["rpc_func"] == "usage_ensure_partitions"
        assert payload["active"] is True

    def test_missing_scheduler_does_not_block_startup(self, recording_log):
        """A pylon without the scheduling plugin is a valid deployment."""
        def raiser(_timeout):
            raise queue.Empty()
        #
        instance, _ = build(rpc=types.SimpleNamespace(timeout=raiser))
        #
        instance.ready()
        #
        assert any("cron not registered" in message for message in recording_log.messages("warning"))

    def test_broken_scheduler_does_not_block_startup(self, recording_log):
        def raiser(_timeout):
            raise RuntimeError("boom")
        #
        instance, _ = build(rpc=types.SimpleNamespace(timeout=raiser))
        #
        instance.ready()
        #
        assert any(
            "failed to register partition cron" in message
            for message in recording_log.messages("warning")
        )

    def test_warns_when_a_mode_is_set_but_nothing_is_metered_yet(self, recording_log):
        """An operator who flips the flag before the drainer lands must be told plainly."""
        instance, _ = build(config={"mode": "observe"})
        #
        instance.ready()
        #
        assert any(
            "metering is not implemented yet" in message
            for message in recording_log.messages("warning")
        )

    def test_off_mode_emits_no_such_warning(self, recording_log):
        instance, _ = build(config={"mode": "off"})
        #
        instance.ready()
        #
        assert not any(
            "metering is not implemented yet" in message
            for message in recording_log.messages("warning")
        )


class TestReconfig:
    """The admin flag is requires_restart: false, so a live re-read must work."""

    def test_reads_the_new_mode(self, recording_log):
        instance, _ = build(config={"mode": "off"})
        #
        instance.descriptor.config["usage"]["mode"] = "enforce"
        instance.reconfig()
        #
        assert instance.usage_get_mode() == "enforce"
        assert any("usage reconfigured" in message for message in recording_log.messages("info"))

    def test_flipping_to_observe_at_runtime_warns_nothing_is_metered(self, recording_log):
        # The admin flag flips without a restart, so ready()'s one-shot warning would never
        # be seen by the operator who actually flipped it.
        instance, _ = build(config={"mode": "off"})
        #
        instance.descriptor.config["usage"]["mode"] = "observe"
        instance.reconfig()
        #
        assert any(
            "metering is not implemented yet" in message
            for message in recording_log.messages("warning")
        )

    def test_flipping_back_to_off_does_not_warn(self, recording_log):
        instance, _ = build(config={"mode": "observe"})
        #
        instance.descriptor.config["usage"]["mode"] = "off"
        instance.reconfig()
        #
        assert not any(
            "metering is not implemented yet" in message
            for message in recording_log.messages("warning")
        )

    def test_does_not_raise_on_an_empty_config(self):
        instance, _ = build(config=None)
        #
        instance.reconfig()


class TestDeinit:

    def test_does_not_raise(self):
        instance, _ = build()
        #
        instance.deinit()

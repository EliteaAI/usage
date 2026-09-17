"""Module.init/ready/reconfig/deinit against stubs — order and failure tolerance."""
import queue
import types

from fixtures.helpers import bind
from usage import module as module_module
from usage.methods import interfaces, mode as mode_module, partitions
from usage.sources import registry


def build(config=None, rpc=None, descriptors=None):
    """A real Module wired to recording stubs, with the mixins bound as pylon would."""
    calls = []
    #
    descriptor = types.SimpleNamespace(
        config={"usage": config if config is not None else {}},
        name="usage",
        metadata={"name": "usage", "version": "0.9"},
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
    instance.usage_start_workers = lambda *a, **k: calls.append(("start_workers", None))
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

    def test_the_wire_dialects_are_registered(self):
        # Nothing else calls register_defaults() in a live pylon, so without this init() the
        # registry is empty at runtime and every single call is recorded as `unparsed`.
        registry.clear()
        instance, _ = build()
        #
        instance.init()
        #
        assert "openai.chat" in registry.all()
        assert "ai_dial.chat" in registry.all()

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

    def test_the_interface_surface_is_reachable_on_the_registered_tool(self):
        """An interface plugin only ever sees this tool; a missing name is an AttributeError
        on every LLM call, which no unit test binding off the module class would catch."""
        instance, _ = build()
        #
        proxy_target = {"endpoint": "/v1/chat/completions", "headers": {}, "json": {}}
        proxy_auth = {}
        #
        # mode defaults to off here, so this is also the zero-cost path
        instance.prepare_llm_call(proxy_target, proxy_auth, "m", 1)
        marker = iter(())
        #
        assert proxy_auth == {}
        assert instance.meter_llm_call(proxy_target, proxy_auth, None, marker) is marker


class TestReady:
    """ready() is diagnostics plus cron; neither may block startup."""

    def test_ensures_partitions_after_shared_applied_the_metadata(self):
        instance, calls = build()
        #
        instance.ready()
        #
        assert [name for name, _ in calls if name == "ensure_partitions"] == ["ensure_partitions"]

    def test_openapi_registration_happens_or_the_endpoint_is_invisible_to_swagger(self):
        from tools import openapi_registry  # pylint: disable=C0415
        openapi_registry.registered.clear()
        #
        build()[0].ready()
        #
        assert openapi_registry.registered[-1]["plugin_name"] == "usage"

    def test_the_usage_tag_is_registered_not_just_the_plugin(self):
        # A second _register_openapi definition once shadowed the first, so the plugin still
        # registered while the usage/usage tag silently vanished from swagger. plugin_name alone
        # could not tell the difference; the tag can.
        from tools import openapi_registry  # pylint: disable=C0415
        openapi_registry.registered.clear()
        #
        build()[0].ready()
        #
        tags = openapi_registry.registered[-1].get("tags") or []
        #
        assert [tag["name"] for tag in tags] == ["usage/usage"]

    def test_only_one_openapi_registration_happens(self):
        # ready() called _register_openapi twice after a merge; a duplicate registration is
        # harmless today but says the callsite list is wrong
        from tools import openapi_registry  # pylint: disable=C0415
        openapi_registry.registered.clear()
        #
        build()[0].ready()
        #
        assert len(openapi_registry.registered) == 1

    def test_enumerates_interfaces_and_registers_the_cron(self):
        instance, calls = build()
        reported = []
        instance.usage_report_interfaces = lambda: reported.append("reported") or []
        #
        instance.ready()
        #
        assert reported == ["reported"]
        assert [name for name, _ in calls if name == "cron"] == ["cron", "cron"]

    def test_cron_targets_the_registered_rpc_name(self):
        instance, calls = build()
        #
        instance.ready()
        #
        payloads = [args for name, args in calls if name == "cron"]
        #
        assert [payload["rpc_func"] for payload in payloads] == [
            "usage_ensure_partitions", "usage_reconcile_counters",
        ]
        assert all(payload["active"] is True for payload in payloads)

    def test_the_reconcile_cron_applies_repairs_not_just_reports(self):
        # Report-only was the safe starting point; once the repair path was hardened (locking,
        # batched isolation, loud non-deduped failures) the cron must actually apply the fix
        instance, calls = build()
        #
        instance.ready()
        #
        payloads = {args["rpc_func"]: args for name, args in calls if name == "cron"}
        assert payloads["usage_reconcile_counters"]["rpc_kwargs"] == {"apply": True}

    def test_missing_scheduler_does_not_block_startup(self, recording_log):
        """A pylon without the scheduling plugin is a valid deployment."""
        def raiser(_timeout):
            raise queue.Empty()
        #
        instance, _ = build(rpc=types.SimpleNamespace(timeout=raiser))
        #
        instance.ready()
        #
        assert any("crons not registered" in message for message in recording_log.messages("warning"))

    def test_broken_scheduler_does_not_block_startup(self, recording_log):
        def raiser(_timeout):
            raise RuntimeError("boom")
        #
        instance, _ = build(rpc=types.SimpleNamespace(timeout=raiser))
        #
        instance.ready()
        #
        assert any(
            "failed to register crons" in message
            for message in recording_log.messages("warning")
        )

    def test_observe_says_nothing_when_every_interface_is_metered(self, recording_log):
        """Metering and gating both work now, so a healthy mode is a quiet mode."""
        instance, _ = build(config={"mode": "observe"})
        #
        instance.ready()
        #
        assert not recording_log.messages("warning")
        assert not recording_log.messages("error")

    def test_enforce_errors_when_an_interface_is_unmetered(self, recording_log):
        # Decision 5: the interface keeps serving, so the enforcement gap is only ever
        # visible in the log. Silence here would mean silently ungated traffic.
        instance, _ = build(config={"mode": "enforce"})
        instance.usage_report_interfaces = lambda: ["runtime_interface_legacy"]
        #
        instance.ready()
        #
        assert any(
            "unmetered and ungated" in message
            for message in recording_log.messages("error")
        )
        assert any(
            "runtime_interface_legacy" in record[2] for record in recording_log.records
        )

    def test_enforce_is_quiet_when_every_interface_declares_hooks(self, recording_log):
        instance, _ = build(config={"mode": "enforce"})
        instance.usage_report_interfaces = lambda: []
        #
        instance.ready()
        #
        assert not recording_log.messages("error")

    def test_off_mode_says_nothing_at_all(self, recording_log):
        instance, _ = build(config={"mode": "off"})
        instance.usage_report_interfaces = lambda: ["runtime_interface_legacy"]
        #
        instance.ready()
        #
        assert not recording_log.messages("warning")
        assert not recording_log.messages("error")


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

    def test_flipping_to_enforce_at_runtime_reports_unmetered_interfaces(self, recording_log):
        # The admin flag flips without a restart, so ready()'s one-shot report would never
        # be seen by the operator who actually flipped it.
        instance, _ = build(config={"mode": "off"})
        instance.usage_report_interfaces = lambda: ["runtime_interface_legacy"]
        #
        instance.descriptor.config["usage"]["mode"] = "enforce"
        instance.reconfig()
        #
        assert any(
            "unmetered and ungated" in message
            for message in recording_log.messages("error")
        )

    def test_flipping_back_to_off_is_quiet(self, recording_log):
        instance, _ = build(config={"mode": "enforce"})
        instance.usage_report_interfaces = lambda: ["runtime_interface_legacy"]
        #
        instance.descriptor.config["usage"]["mode"] = "off"
        instance.reconfig()
        #
        assert not recording_log.messages("error")

    def test_does_not_raise_on_an_empty_config(self):
        instance, _ = build(config=None)
        #
        instance.reconfig()


class TestDeinit:

    def test_does_not_raise(self):
        instance, _ = build()
        #
        instance.deinit()

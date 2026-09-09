"""The frozen hook contract. Bodies land with the drainer and the gate; signatures are law now."""
import inspect

import pytest

from usage import hooks


BEGIN_PARAMS = ["project_id", "user_id", "model_name", "endpoint", "headers"]


class TestBeginLlmCall:
    def test_signature_is_exactly_the_contract(self):
        # An interface plugin written against this must keep compiling; a rename has to
        # break here rather than at runtime in someone else's route.
        assert list(inspect.signature(hooks.begin_llm_call).parameters) == BEGIN_PARAMS

    def test_no_parameter_has_a_default(self):
        parameters = inspect.signature(hooks.begin_llm_call).parameters
        #
        assert all(p.default is inspect.Parameter.empty for p in parameters.values())

    def test_no_var_kwargs_so_a_typo_is_caught(self):
        kinds = [p.kind for p in inspect.signature(hooks.begin_llm_call).parameters.values()]
        #
        assert inspect.Parameter.VAR_KEYWORD not in kinds

    def test_returns_none_while_there_is_no_gate(self):
        assert hooks.begin_llm_call(
            project_id=7, user_id=42, model_name="gpt-4o",
            endpoint="/v1/chat/completions", headers={},
        ) is None

    def test_unexpected_kwarg_raises_rather_than_being_swallowed(self):
        with pytest.raises(TypeError):
            hooks.begin_llm_call(
                project_id=7, user_id=42, model_name="gpt-4o",
                endpoint="/v1/chat/completions", headers={}, tenant_id=1,
            )

    def test_missing_kwarg_raises(self):
        with pytest.raises(TypeError):
            hooks.begin_llm_call(project_id=7, user_id=42)


class TestMeterLlmResponse:
    def test_signature_is_exactly_the_contract(self):
        assert list(inspect.signature(hooks.meter_llm_response).parameters) \
            == ["ctx", "response", "iterator"]

    def test_returns_the_iterator_itself(self):
        iterator = iter([b"chunk"])
        #
        # Identity, not equality: no wrapping and no buffering while there is nothing to meter.
        assert hooks.meter_llm_response(None, object(), iterator) is iterator

    def test_returns_the_iterator_even_with_a_context(self):
        iterator = iter([b"chunk"])
        ctx = hooks.UsageContext(project_id=7, user_id=42)
        #
        assert hooks.meter_llm_response(ctx, object(), iterator) is iterator

    def test_the_iterator_is_not_consumed(self):
        iterator = iter([b"a", b"b"])
        hooks.meter_llm_response(None, object(), iterator)
        #
        assert list(iterator) == [b"a", b"b"]


class TestUsageContext:
    def test_denied_defaults_to_false_so_the_interface_snippet_is_safe(self):
        ctx = hooks.UsageContext()
        #
        assert ctx.denied is False
        assert ctx.response is None

    def test_carries_the_call_identity(self):
        ctx = hooks.UsageContext(
            project_id=7, user_id=42, model_name="gpt-4o", endpoint="/v1/chat/completions",
        )
        #
        assert (ctx.project_id, ctx.user_id, ctx.model_name) == (7, 42, "gpt-4o")

    def test_a_denial_carries_the_response_the_interface_returns_as_is(self):
        response = object()
        ctx = hooks.UsageContext(denied=True, response=response)
        #
        assert ctx.denied is True
        assert ctx.response is response

    def test_every_contract_field_exists(self):
        fields = {field.name for field in hooks.dataclasses.fields(hooks.UsageContext)}
        #
        assert fields == {
            "project_id", "user_id", "model_name", "endpoint", "denied", "response",
        }

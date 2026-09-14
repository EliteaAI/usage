"""Test helper functions."""
import importlib
import types


def fake_module(config=None, **attrs):
    """A stand-in Module instance carrying a descriptor config and the mixin methods.

    Pylon binds Method/RPC classes onto the Module instance at init_all(); tests reproduce that
    by copying the callables onto a plain object.
    """
    instance = types.SimpleNamespace(
        descriptor=types.SimpleNamespace(config=config if config is not None else {}),
        context=types.SimpleNamespace(),
    )
    #
    for name, value in attrs.items():
        setattr(instance, name, value)
    #
    return instance


def bind(instance, *resource_classes):
    """Bind every public callable of the given Method/RPC classes onto instance.

    Underscore-prefixed attributes are skipped on purpose: Pylon binds only what @web.method
    and @web.rpc declare, so a private helper reached through self fails at runtime. Binding
    it here would hide exactly that bug.
    """
    for resource in resource_classes:
        for name in dir(resource):
            if name.startswith("_"):
                continue
            #
            attribute = getattr(resource, name)
            #
            if callable(attribute):
                setattr(instance, name, attribute.__get__(instance, type(instance)))
    #
    return instance


def load(module_name):
    """Import a usage submodule by dotted name below the plugin package."""
    return importlib.import_module(f"usage.{module_name}")

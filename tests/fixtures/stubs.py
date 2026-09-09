"""Reusable stubs for RPC and database interaction."""
import queue


class RecordingRpc:
    """Stands in for context.rpc_manager, recording every call it is asked to make."""

    def __init__(self, returns=None, raises=None):
        self.returns = returns or {}
        self.raises = raises or {}
        self.calls = []

    def timeout(self, seconds):  # pylint: disable=W0613
        return self

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            #
            if name in self.raises:
                raise self.raises[name]
            #
            return self.returns.get(name)
        return call

    def names(self):
        return [name for name, _ in self.calls]


class MissingRpc(RecordingRpc):
    """An rpc_manager where nothing is registered — raises queue.Empty like pylon does."""

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            raise queue.Empty()
        return call


class _Result:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class RecordingConnection:
    """Captures executed statements without a database.

    Parameterised calls are treated as probe queries (the parent-exists check) rather than
    DDL, so they stay out of `statements` and the DDL assertions keep their indexes.
    """

    def __init__(self, parent_exists=True):
        self.statements = []
        self.queries = []
        self.commits = 0
        self.parent_exists = parent_exists

    def execute(self, statement, params=None):
        if params is not None:
            self.queries.append((str(statement), params))
            return _Result((1,) if self.parent_exists else None)
        #
        self.statements.append(str(statement))
        return _Result(None)

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class RecordingEngine:
    """Hands out one RecordingConnection and remembers it."""

    def __init__(self, parent_exists=True):
        self.connection = RecordingConnection(parent_exists=parent_exists)

    def connect(self):
        return self.connection

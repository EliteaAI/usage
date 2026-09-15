"""An in-memory Redis stand-in, with the priming Lua script hand-ported to Python.

The real script runs inside Redis, so unit tests cannot execute it. It is re-expressed here
against the same KEYS/ARGV contract: if the Python port and the Lua ever disagree the port is
wrong, so keep them side by side.
"""
import time

from usage.methods import gate


class RecordingRedis:
    """Enough of redis-py for the gate, the drainer and the lease."""

    def __init__(self):
        self.hashes = {}
        self.lists = {}
        self.strings = {}
        self.expiries = {}
        self.calls = []

    # -- hashes

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = str(value)

    def hincrby(self, key, field, amount):
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = str(int(bucket.get(field, 0)) + int(amount))
        #
        return int(bucket[field])

    def counter(self, key):
        return int(self.hashes.get(key, {}).get("counter", 0))

    # -- lists

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        #
        return len(self.lists[key])

    def lpop(self, key, count=None):
        queue = self.lists.setdefault(key, [])
        #
        if count is None:
            return queue.pop(0) if queue else None
        #
        taken, self.lists[key] = queue[:count], queue[count:]
        #
        return taken

    # -- strings and leases

    def set(self, key, value, nx=False, ex=None):  # pylint: disable=C0103
        self.calls.append(("set", key, value, ex))
        #
        if nx and key in self.strings and not self._expired(key):
            return None
        #
        self.strings[key] = value
        self.expiries[key] = None if ex is None else time.time() + ex
        #
        return True

    def get(self, key):
        return None if self._expired(key) else self.strings.get(key)

    def expire(self, key, seconds):
        self.expiries[key] = time.time() + seconds
        #
        return 1

    def force_lease_expiry(self, key):
        """Age a lease out without sleeping through its TTL."""
        self.expiries[key] = time.time() - 1

    def _expired(self, key):
        deadline = self.expiries.get(key)
        #
        if deadline is not None and deadline <= time.time():
            self.strings.pop(key, None)
            self.expiries.pop(key, None)
            #
            return True
        #
        return False

    # -- eval

    def eval(self, script, numkeys, *args):  # pylint: disable=W0622
        keys, argv = list(args[:numkeys]), list(args[numkeys:])
        self.calls.append(("eval", keys, argv))
        #
        if script == gate.PRIME_LUA:
            return self._prime(keys, argv)
        #
        raise AssertionError("unknown script")

    def _prime(self, keys, argv):
        hash_key, persisted = keys[0], int(argv[0])
        current = self.hashes.get(hash_key, {}).get("counter")
        #
        if current is None or int(current) < persisted:
            self.hset(hash_key, "counter", persisted)
            #
            return 1
        #
        return 0

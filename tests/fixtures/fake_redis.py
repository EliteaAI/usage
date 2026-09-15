"""An in-memory Redis stand-in, with both gate Lua scripts hand-ported to Python.

The real scripts run inside Redis, so unit tests cannot execute them. They are re-expressed
here against the same KEYS/ARGV contract, which is what the gate tests actually exercise:
if the Python port and the Lua ever disagree the port is wrong, so keep them side by side.
"""
import time

from usage.methods import gate


class RecordingRedis:
    """Enough of redis-py for the gate, the drainer, the reaper and the lease."""

    def __init__(self):
        self.hashes = {}
        self.zsets = {}
        self.sets = {}
        self.lists = {}
        self.strings = {}
        self.expiries = {}
        self.calls = []

    # -- hashes

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hmget(self, key, *fields):
        bucket = self.hashes.get(key, {})
        #
        return [bucket.get(field) for field in fields]

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = str(value)

    def hsetnx(self, key, field, value):
        self.calls.append(("hsetnx", key, field, value))
        bucket = self.hashes.setdefault(key, {})
        #
        if field in bucket:
            return 0
        #
        bucket[field] = str(value)
        #
        return 1

    def hincrby(self, key, field, amount):
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = str(int(bucket.get(field, 0)) + int(amount))
        #
        return int(bucket[field])

    def counter(self, key):
        return int(self.hashes.get(key, {}).get("counter", 0))

    def reserved(self, key):
        return int(self.hashes.get(key, {}).get("reserved", 0))

    # -- zsets

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    def zrem(self, key, member):
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def zrangebyscore(self, key, minimum, maximum, start=0, num=None):
        members = [
            member for member, score in sorted(
                self.zsets.get(key, {}).items(), key=lambda item: item[1],
            )
            if minimum <= score <= maximum
        ]
        #
        return members[start:None if num is None else start + num]

    # -- sets

    def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def srem(self, key, member):
        bucket = self.sets.setdefault(key, set())
        #
        if member not in bucket:
            return 0
        #
        bucket.discard(member)
        #
        return 1

    # -- lists

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        #
        return len(self.lists[key])

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
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
        if script == gate.GATE_LUA:
            return self._gate(keys, argv)
        #
        if script == gate.SETTLE_LUA:
            return self._settle(keys, argv)
        #
        if script == gate.PRIME_LUA:
            return self._prime(keys, argv)
        #
        raise AssertionError("unknown script")

    def _prime(self, keys, argv):
        hash_key, persisted = keys[0], int(argv[0])
        self.expire(hash_key, int(argv[1]))
        current = self.hashes.get(hash_key, {}).get("counter")
        #
        if current is None or int(current) < persisted:
            self.hset(hash_key, "counter", persisted)
            #
            return 1
        #
        return 0

    def _gate(self, keys, argv):
        project_hash, member_hash, resv, index = keys
        estimate = int(argv[0])
        project_limit, member_limit = int(argv[1]), int(argv[2])
        #
        outstanding = self.counter(project_hash) + self.reserved(project_hash)
        #
        if project_limit >= 0 and outstanding + estimate > project_limit:
            return [0, "project"]
        #
        if member_hash and member_limit >= 0:
            member_outstanding = self.counter(member_hash) + self.reserved(member_hash)
            #
            if member_outstanding + estimate > member_limit:
                return [0, "member"]
        #
        self.hincrby(project_hash, "reserved", estimate)
        #
        if member_hash:
            self.hincrby(member_hash, "reserved", estimate)
        #
        self.zadd(resv, {argv[5]: int(argv[3])})
        self.sadd(index, argv[4])
        #
        ttl = int(argv[6])
        self.expire(project_hash, ttl)
        #
        if member_hash:
            self.expire(member_hash, ttl)
        #
        self.expire(resv, ttl)
        #
        return [1, argv[5]]

    def _release(self, key, estimate):
        """Mirrors the Lua floor: an evicted key must not leave negative reserved behind."""
        if self.hincrby(key, "reserved", -estimate) < 0:
            self.hset(key, "reserved", 0)

    def _settle(self, keys, argv):
        resv, project_hash, member_hash = keys
        removed = self.zrem(resv, argv[0])
        #
        if removed == 1:
            self._release(project_hash, int(argv[1]))
            #
            if member_hash:
                self._release(member_hash, int(argv[1]))
        #
        actual = int(argv[2])
        #
        if actual > 0:
            self.hincrby(project_hash, "counter", actual)
            #
            if member_hash:
                self.hincrby(member_hash, "counter", actual)
        #
        return removed

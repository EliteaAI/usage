# usage

Platform-owned metering and enforcement of LLM and tool usage.

Owns two tables in the `centry` schema:

| Table | Purpose |
|---|---|
| `usage_event` | fact table, one row per LLM call or tool call, monthly RANGE-partitioned on `ts` |
| `usage_counter` | small hot ledger the enforcement gate reads, grain `(project, user, period, model)` |

`usage_counter` sentinels: `user_id = 0` is the project aggregate, `model_name = ''` is all models.
Every read of these tables goes through this plugin, so no other plugin needs to know that.

## Mode

`usage.mode` is `off` (default), `observe` or `enforce`. In `off` the plugin creates its schema and
changes no behaviour: the `usage_*` spend RPCs delegate to the legacy LiteLLM tag aggregates.

## Contract with interface plugins

`usage` publishes two hooks as the `usage_hooks` tool; each `runtime_interface_*` plugin contributes
exactly two call sites on its proxy route and nothing else:

```python
usage_ctx = usage_hooks.begin_llm_call(project_id, user_id, model_name, endpoint, headers)
if usage_ctx is not None and usage_ctx.denied:
    return usage_ctx.response
...
iterator = usage_hooks.meter_llm_response(usage_ctx, response, iterator)
```

The hooks are resolved lazily at call time, so there is no `init_after` coupling in either direction.
`model_name` is the raw requested name, before any interface-side rewriting.

## Request headers metering relies on

Agent runs from pylon_indexer reach `/llm/v1` with these headers. Any proxy between the indexer
and pylon_main (customer ingress, WAF, API gateway) must pass them through unchanged:

| Header | Sent by | Used for | If it is stripped |
|---|---|---|---|
| `X-Project-Id` | indexer | project the call is billed to | billed to the caller's resolved project instead |
| `X-Elitea-Run-Id` | indexer | groups the LLM and tool rows of one run | rows carry no run id |
| `X-Elitea-Attribution` | indexer | conversation and agent (leaf/root) of the call | row is billed, but agent analytics show nothing for it |

Nothing is logged when a header is simply absent: a script calling `/llm/v1` directly never
sends them, so absence is normal. If a customer reports "the predict ran but agent analytics show
0", check first that these headers survive their proxies.

`X-Elitea-Attribution` is base64url JSON carrying a `sig` field:
HMAC-SHA256 over `"<billed project id>\n<canonical JSON of the other fields>"`. The key is
`HMAC(event_node.hmac_key, "usage-attribution-v1")`, i.e. derived from `INDEXER_HMAC_KEY`, which
both pylons already share and no user can read. A missing or bad signature drops the agent
labels (the row and its cost are kept) and logs a warning — this is what stops a caller from
billing its spend to someone else's agent. The two pylons must therefore run with the same
`INDEXER_HMAC_KEY`, set to a real random value.

## Aggregation SQL lives here

All SQL against `usage_event` and `usage_counter` lives in this plugin and is exposed as named RPCs
returning already-aggregated, server-side-paged rows. Deliberately **not** a generic query DSL:
arbitrary dimension combinations cannot be index-proven, so an unusual filter would become a
full-partition scan. A report that needs a new number gets a new named RPC here.

## Schema creation

Both tables are provisioned the same way every other plugin's are: `init()` imports the models so
they register in the shared metadata, and whoever applies that metadata creates them — `shared.ready()`
when `apply_shared_metadata` is on, the `admin.create_tables` task when it is off. This plugin never
calls `create_all()` itself, so the flag governs it by construction.

`usage_event` is monthly RANGE-partitioned; SQLAlchemy emits `PARTITION BY RANGE (ts)` from
`postgresql_partition_by` in `__table_args__`. Child partitions are the one thing SQLAlchemy cannot
express, so `usage_ensure_partitions()` creates them from `ready()` and from a daily cron. It no-ops
with a warning when the parent is absent — i.e. when the metadata has not been applied yet.

## Tests

```bash
python tests/run_tests.py -v
```

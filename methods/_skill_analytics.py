#!/usr/bin/python3
# coding=utf-8

#   Copyright 2026 EPAM Systems
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

""" Skills analytics over usage_event (#6929) """

from sqlalchemy import Date, and_, asc, case, desc, distinct, func, nulls_last, or_, select

from pylon.core.tools import log  # pylint: disable=E0611,E0401

from . import _analytics as an

SKILL_EVENT_TYPES = (an.EVENT_LLM, an.EVENT_TOOL, an.EVENT_SKILL)
TRIGGER_MANUAL = "manual"
SORT_COLUMNS = (
    "runs", "in_agent_uses", "users", "agents_using", "avg_duration_ms", "errors",
    "total_tokens", "cost", "last_used", "skill_name",
)
DEFAULT_SORT = "runs"
DETAIL_USERS_LIMIT = 50
DETAIL_AGENTS_LIMIT = 20
RECENT_RUNS_LIMIT = 20


def skill_rows(project_id, dt_from, dt_to, skill_ref=None):
    event = an.UsageEvent
    is_use = event.event_type == an.EVENT_SKILL
    skill_id = case((is_use, event.entity_id), else_=event.root_entity_id)
    conditions = an.base_filters(project_id, dt_from, dt_to, event_types=SKILL_EVENT_TYPES) + [
        or_(is_use, event.root_entity_type == an.ENTITY_TYPE_SKILL),
        skill_id.isnot(None),
    ]
    if skill_ref is not None:
        skill_project_id, ref_skill_id = skill_ref
        conditions.append(or_(
            and_(is_use, event.entity_id == ref_skill_id),
            and_(~is_use, event.root_entity_id == ref_skill_id),
        ))
        conditions.append(an.root_project_expr() == skill_project_id)
    return select(
        skill_id.label("skill_id"),
        an.root_project_expr().label("skill_project_id"),
        case((is_use, 1), else_=0).label("is_use"),
        case((is_use, event.entity_version_id), else_=event.root_entity_version_id).label("version_id"),
        case((or_(is_use, an.is_run_row()), event.entity_name), else_=None).label("skill_name"),
        event.run_id,
        event.conversation_id,
        event.user_id,
        event.user_email,
        event.root_entity_type,
        event.root_entity_id,
        event.event_type,
        event.model_name,
        event.trigger_source,
        event.duration_ms,
        event.is_error,
        event.ts,
        event.meta["source"].as_string().label("source"),
        event.meta["est_body_tokens"].as_integer().label("est_body_tokens"),
        event.billable_input_tokens,
        event.input_tokens,
        event.output_tokens,
        event.cache_read_tokens,
        event.cache_creation_tokens,
        an.total_tokens_expr().label("total_tokens"),
        event.cost_nano_usd,
        *(column.label(f"{key}_nano") for key, column in an.COST_SPLIT_COLUMNS.items()),
    ).where(*conditions).subquery("skill_rows")


class _Measures:
    def __init__(self, rows):
        self.rows = rows
        self.is_run = rows.c.is_use == 0
        self.is_use = rows.c.is_use == 1

    def runs(self):
        return func.count(distinct(case((self.is_run, self.rows.c.run_id), else_=None)))

    def in_agent_uses(self):
        return func.coalesce(func.sum(self.rows.c.is_use), 0)

    def users(self):
        return func.count(distinct(self.rows.c.user_id))

    def agents_using(self):
        return func.count(distinct(case(
            (and_(self.is_use, self.rows.c.root_entity_type.in_(an.AGENT_ROOT_TYPES)), self.rows.c.root_entity_id),
            else_=None,
        )))

    def avg_duration_ms(self):
        return func.avg(case((self.is_run, self.rows.c.duration_ms), else_=None))

    def errors(self):
        return func.count(distinct(case(
            (and_(self.is_run, self.rows.c.is_error.is_(True)), self.rows.c.run_id), else_=None,
        )))

    def run_sum(self, column):
        return func.coalesce(func.sum(case((self.is_run, func.coalesce(column, 0)), else_=0)), 0)

    def avg_est_body_tokens(self):
        return func.avg(case((self.is_use, self.rows.c.est_body_tokens), else_=None))

    def last_used(self):
        return func.max(self.rows.c.ts)

    def skill_name(self):
        return func.regexp_replace(func.max(self.rows.c.skill_name), r" \([^()]*\)$", "")

    def totals(self):
        c = self.rows.c
        return [
            self.runs().label("runs"),
            self.in_agent_uses().label("in_agent_uses"),
            self.users().label("users"),
            self.agents_using().label("agents_using"),
            self.avg_duration_ms().label("avg_duration_ms"),
            self.errors().label("errors"),
            self.run_sum(c.billable_input_tokens).label("input_tokens"),
            self.run_sum(c.output_tokens).label("output_tokens"),
            self.run_sum(c.cache_read_tokens).label("cache_read_tokens"),
            self.run_sum(c.cache_creation_tokens).label("cache_creation_tokens"),
            self.run_sum(c.total_tokens).label("total_tokens"),
            self.run_sum(c.cost_nano_usd).label("cost_nano"),
            *(self.run_sum(c[f"{key}_nano"]).label(f"{key}_nano") for key in an.COST_SPLIT_COLUMNS),
            self.avg_est_body_tokens().label("avg_est_body_tokens"),
            self.last_used().label("last_used"),
        ]


def _iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return an.as_utc(value).isoformat()


def _totals(row):
    runs = int(row.get("runs") or 0)
    errors = int(row.get("errors") or 0)
    return {
        "runs": runs,
        "in_agent_uses": int(row.get("in_agent_uses") or 0),
        "users": int(row.get("users") or 0),
        "agents_using": int(row.get("agents_using") or 0),
        "avg_duration_ms": an.rounded(row.get("avg_duration_ms"), 1),
        "errors": errors,
        "error_rate": round(errors / runs * 100, 2) if runs else 0,
        "input_tokens": int(row.get("input_tokens") or 0),
        "output_tokens": int(row.get("output_tokens") or 0),
        "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
        "cache_creation_tokens": int(row.get("cache_creation_tokens") or 0),
        "total_tokens": int(row.get("total_tokens") or 0),
        "cost": an.cost_usd(row.get("cost_nano")),
        **{key: an.cost_usd(row.get(f"{key}_nano")) for key in an.COST_SPLIT_COLUMNS},
        "avg_est_body_tokens": an.rounded(row.get("avg_est_body_tokens"), 1),
        "last_used": _iso(row.get("last_used")),
    }


def skill_meta(project_id, skill_refs=(), conversation_uuids=()):
    refs = sorted({(int(p), int(i)) for p, i in skill_refs if p is not None and i is not None})
    uuids = sorted({str(u) for u in conversation_uuids if u})
    if not refs and not uuids:
        return {}, {}
    from tools import rpc_tools  # pylint: disable=C0415,E0401
    try:
        response = rpc_tools.RpcMixin().rpc.timeout(5).elitea_core_usage_entity_meta(
            project_id, skill_refs=[list(ref) for ref in refs], conversation_uuids=uuids,
        ) or {}
    except:  # pylint: disable=W0702
        log.warning("usage: skill names unavailable for project %s", project_id)
        return None, {}
    skills = response.get("scoped_skills")
    return (
        None if skills is None else {(item["project_id"], item["id"]): item for item in skills},
        {item["uuid"]: item["id"] for item in response.get("conversations") or []},
    )


def _identity(skill_id, skill_project_id, stored_name, skills):
    meta = None if skills is None else skills.get((skill_project_id, skill_id))
    return {
        "skill_id": skill_id,
        "skill_project_id": skill_project_id,
        "skill_name": (meta or {}).get("name") or stored_name or f"Skill #{skill_id}",
        "deleted": skills is not None and meta is None,
        "is_public": bool((meta or {}).get("is_public")),
        "icon_meta": (meta or {}).get("icon_meta") or {},
        "versions": (meta or {}).get("versions") or [],
    }


def skill_leaderboard(project_id, dt_from, dt_to, search="", sort_by=DEFAULT_SORT, sort_order="desc",
                      limit=20, offset=0):
    rows = skill_rows(project_id, dt_from, dt_to)
    measures = _Measures(rows)
    columns = measures.totals()
    by_label = {column.name: column for column in columns}
    name = measures.skill_name().label("skill_name")
    grouped = select(rows.c.skill_id, rows.c.skill_project_id, name, *columns).group_by(
        rows.c.skill_id, rows.c.skill_project_id,
    )
    summary = an.fetch_one(select(func.count().label("skills_used")).select_from(grouped.subquery())) or {}
    totals = an.fetch_one(select(*columns)) or {}
    if search:
        grouped = grouped.having(name.ilike(f"%{search}%"))
    total_row = an.fetch_one(select(func.count().label("total")).select_from(grouped.subquery()))
    sort_column = name if sort_by == "skill_name" else by_label.get(
        "cost_nano" if sort_by == "cost" else sort_by, by_label[DEFAULT_SORT],
    )
    order = desc if sort_order == "desc" else asc
    page = an.fetch_all(grouped.order_by(
        nulls_last(order(sort_column)), rows.c.skill_project_id, rows.c.skill_id,
    ).limit(limit).offset(offset))
    skills, _ = skill_meta(project_id, [(r["skill_project_id"], r["skill_id"]) for r in page])
    period = _totals(totals)
    return {
        "total": int((total_row or {}).get("total") or 0),
        "summary": {
            "skills_used": int(summary.get("skills_used") or 0),
            "runs": period["runs"],
            "in_agent_uses": period["in_agent_uses"],
            "total_tokens": period["total_tokens"],
            "cost": period["cost"],
        },
        "rows": [
            {
                **_identity(r["skill_id"], r["skill_project_id"], r["skill_name"], skills),
                **_totals(r),
            }
            for r in page
        ],
    }


def _grouped(measures, *keys, extra=(), order=None, limit=None):
    statement = select(*keys, *extra, *measures.totals()).group_by(*keys)
    if order is not None:
        statement = statement.order_by(*order)
    if limit is not None:
        statement = statement.limit(limit)
    return an.fetch_all(statement)


def skill_detail(project_id, skill_id, skill_project_id, dt_from, dt_to):
    rows = skill_rows(project_id, dt_from, dt_to, skill_ref=(skill_project_id, skill_id))
    measures = _Measures(rows)
    c = rows.c
    head = an.fetch_one(select(
        measures.skill_name().label("skill_name"), func.count().label("rows"), *measures.totals(),
    ))
    if not head or not head["rows"]:
        return None

    day = func.date(c.ts, type_=Date).label("day")
    daily = _grouped(measures, day, order=[day])
    versions = _grouped(measures, c.version_id, order=[desc(measures.runs()), c.version_id])
    models = an.fetch_all(select(
        c.model_name,
        measures.runs().label("runs"),
        func.count().label("calls"),
        measures.run_sum(c.total_tokens).label("total_tokens"),
        measures.run_sum(c.cost_nano_usd).label("cost_nano"),
    ).where(measures.is_run, c.event_type == an.EVENT_LLM).group_by(c.model_name).order_by(
        desc(func.count()), c.model_name,
    ))
    users = _grouped(
        measures, c.user_id, extra=[func.max(c.user_email).label("user_email")],
        order=[desc(measures.runs()), desc(measures.in_agent_uses()), c.user_id], limit=DETAIL_USERS_LIMIT,
    )
    triggers = _grouped(measures, c.trigger_source, order=[c.trigger_source])
    agents = an.fetch_all(select(
        c.root_entity_type,
        c.root_entity_id,
        func.count().label("uses"),
        func.count(distinct(c.run_id)).label("runs"),
        func.count(distinct(c.user_id)).label("users"),
        func.max(c.ts).label("last_used"),
    ).where(measures.is_use, c.root_entity_type.in_(an.AGENT_ROOT_TYPES)).group_by(
        c.root_entity_type, c.root_entity_id,
    ).order_by(desc(func.count()), c.root_entity_id).limit(DETAIL_AGENTS_LIMIT))
    sources = an.fetch_all(select(c.source, func.count().label("uses")).where(measures.is_use).group_by(
        c.source,
    ).order_by(c.source))
    started = func.min(c.ts)
    recent = an.fetch_all(select(
        c.run_id,
        func.max(c.conversation_id).label("conversation_id"),
        started.label("started_at"),
        func.max(c.user_id).label("user_id"),
        func.max(c.version_id).label("version_id"),
        func.max(c.model_name).label("model_name"),
        func.max(c.trigger_source).label("trigger_source"),
        measures.run_sum(c.total_tokens).label("total_tokens"),
        measures.run_sum(c.cost_nano_usd).label("cost_nano"),
        measures.run_sum(c.duration_ms).label("duration_ms"),
        func.max(case((c.is_error.is_(True), 1), else_=0)).label("is_error"),
    ).where(measures.is_run).group_by(c.run_id).order_by(desc(started)).limit(RECENT_RUNS_LIMIT))

    skills, conversations = skill_meta(
        project_id, [(skill_project_id, skill_id)], [r["conversation_id"] for r in recent],
    )
    identity = _identity(skill_id, skill_project_id, head["skill_name"], skills)
    version_names = {v.get("id"): v.get("name") for v in identity["versions"]}
    model_names = an.model_display_names(project_id) if models or recent else {}
    agent_meta = an.application_meta(project_id, refs=[(skill_project_id, a["root_entity_id"]) for a in agents])
    emails = an.label_users(
        [r["user_id"] for r in [*users, *recent]],
        {r["user_id"]: r["user_email"] for r in users if r["user_id"] is not None},
    )

    def version_label(version_id):
        if version_id is None:
            return None
        return version_names.get(version_id) or f"Version #{version_id}"

    return {
        **identity,
        "kpis": _totals(head),
        "daily": [
            {"date": r["day"].isoformat() if r["day"] else None, **_totals(r)}
            for r in daily
        ],
        "by_version": [
            {"version_id": r["version_id"], "version_name": version_label(r["version_id"]), **_totals(r)}
            for r in versions
        ],
        "by_model": [
            {
                "model_name": r["model_name"],
                "display_name": model_names.get(r["model_name"]) or r["model_name"],
                "runs": int(r["runs"] or 0),
                "calls": int(r["calls"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
                "cost": an.cost_usd(r["cost_nano"]),
            }
            for r in models
        ],
        "by_user": [
            {"user_id": r["user_id"], "user_email": emails.get(r["user_id"]), **_totals(r)}
            for r in users
        ],
        "by_trigger_source": [
            {"trigger_source": r["trigger_source"] or TRIGGER_MANUAL, **_totals(r)}
            for r in triggers
        ],
        "agents": [
            {
                "entity_id": a["root_entity_id"],
                "entity_project_id": skill_project_id,
                "entity_name": (
                    agent_meta.get((skill_project_id, a["root_entity_id"]), {}).get("name")
                    or f"Agent #{a['root_entity_id']}"
                ),
                "kind": agent_meta.get((skill_project_id, a["root_entity_id"]), {}).get("kind"),
                "deleted": (skill_project_id, a["root_entity_id"]) not in agent_meta,
                "uses": int(a["uses"] or 0),
                "runs": int(a["runs"] or 0),
                "users": int(a["users"] or 0),
                "last_used": _iso(a["last_used"]),
            }
            for a in agents
        ],
        "by_source": [
            {"source": r["source"] or "unknown", "uses": int(r["uses"] or 0)}
            for r in sources
        ],
        "recent_runs": [
            {
                "run_id": r["run_id"],
                "conversation_uuid": r["conversation_id"],
                "conversation_id": conversations.get(r["conversation_id"]),
                "started_at": _iso(r["started_at"]),
                "user_id": r["user_id"],
                "user_email": emails.get(r["user_id"]),
                "version_id": r["version_id"],
                "version_name": version_label(r["version_id"]),
                "model_name": model_names.get(r["model_name"]) or r["model_name"],
                "trigger_source": r["trigger_source"] or TRIGGER_MANUAL,
                "total_tokens": int(r["total_tokens"] or 0),
                "cost": an.cost_usd(r["cost_nano"]),
                "duration_ms": int(r["duration_ms"] or 0),
                "is_error": bool(r["is_error"]),
            }
            for r in recent
        ],
    }

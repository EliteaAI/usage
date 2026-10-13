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

from sqlalchemy import Date, Integer, String, and_, asc, case, cast, desc, distinct, func, nulls_last, or_, select

from pylon.core.tools import log  # pylint: disable=E0611,E0401

from . import _analytics as an

SKILL_EVENT_TYPES = (an.EVENT_LLM, an.EVENT_TOOL, an.EVENT_SKILL)
TRIGGER_MANUAL = "manual"
UNKNOWN_SOURCE = "unknown"
SORT_COLUMNS = (
    "runs", "in_agent_uses", "users", "agents_using", "avg_duration_ms", "errors",
    "total_tokens", "cost", "last_used", "skill_name",
)
DEFAULT_SORT = "runs"
DETAIL_USERS_LIMIT = 50
DETAIL_AGENTS_LIMIT = 20
RECENT_RUNS_LIMIT = 20
VERSION_SUFFIX_PATTERN = r" \([^()]*\)$"
PROJECT_ID_PATTERN = r"^[1-9][0-9]{0,8}$"


def is_skill_use(event):
    return event.event_type == an.EVENT_SKILL


def resolve_skill_id(event):
    return case((is_skill_use(event), event.entity_id), else_=event.root_entity_id)


def resolve_skill_project_id(event):
    stored = cast(event.meta["skill_project_id"].as_string(), String)
    is_project_id = and_(stored != "", func.regexp_replace(stored, PROJECT_ID_PATTERN, "") == "")
    own_project_id = case((is_project_id, cast(stored, Integer)), else_=None)
    return func.coalesce(case((is_skill_use(event), own_project_id), else_=None), an.root_project_expr())


def build_skill_row_conditions(project_id, dt_from, dt_to, skill_ref=None):
    event = an.UsageEvent
    conditions = an.base_filters(project_id, dt_from, dt_to, event_types=SKILL_EVENT_TYPES) + [
        or_(is_skill_use(event), event.root_entity_type == an.ENTITY_TYPE_SKILL),
        resolve_skill_id(event).isnot(None),
    ]
    if skill_ref is not None:
        conditions.extend(build_single_skill_conditions(*skill_ref))
    return conditions


def build_single_skill_conditions(skill_project_id, skill_id):
    event = an.UsageEvent
    return [
        or_(
            and_(is_skill_use(event), event.entity_id == skill_id),
            and_(~is_skill_use(event), event.root_entity_id == skill_id),
        ),
        resolve_skill_project_id(event) == skill_project_id,
    ]


def select_skill_rows(project_id, dt_from, dt_to, skill_ref=None):
    event = an.UsageEvent
    is_use = is_skill_use(event)
    return select(
        resolve_skill_id(event).label("skill_id"),
        resolve_skill_project_id(event).label("skill_project_id"),
        an.root_project_expr().label("root_project_id"),
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
    ).where(*build_skill_row_conditions(project_id, dt_from, dt_to, skill_ref)).subquery("skill_rows")


class SkillMeasures:
    def __init__(self, rows):
        self.rows = rows
        self.is_run = rows.c.is_use == 0
        self.is_use = rows.c.is_use == 1

    def count_runs(self):
        return func.count(distinct(case((self.is_run, self.rows.c.run_id), else_=None)))

    def count_in_agent_uses(self):
        return func.coalesce(func.sum(self.rows.c.is_use), 0)

    def count_users(self):
        return func.count(distinct(self.rows.c.user_id))

    def count_agents_using(self):
        return func.count(distinct(case(
            (and_(self.is_use, self.rows.c.root_entity_type.in_(an.AGENT_ROOT_TYPES)), self.rows.c.root_entity_id),
            else_=None,
        )))

    def average_run_duration_ms(self):
        return func.avg(case((self.is_run, self.rows.c.duration_ms), else_=None))

    def count_failed_runs(self):
        return func.count(distinct(case(
            (and_(self.is_run, self.rows.c.is_error.is_(True)), self.rows.c.run_id), else_=None,
        )))

    def sum_over_runs(self, column):
        return func.coalesce(func.sum(case((self.is_run, func.coalesce(column, 0)), else_=0)), 0)

    def average_body_tokens(self):
        return func.avg(case((self.is_use, self.rows.c.est_body_tokens), else_=None))

    def find_last_used(self):
        return func.max(self.rows.c.ts)

    def pick_skill_name(self):
        return func.regexp_replace(func.max(self.rows.c.skill_name), VERSION_SUFFIX_PATTERN, "")

    def label_totals(self):
        c = self.rows.c
        return [
            self.count_runs().label("runs"),
            self.count_in_agent_uses().label("in_agent_uses"),
            self.count_users().label("users"),
            self.count_agents_using().label("agents_using"),
            self.average_run_duration_ms().label("avg_duration_ms"),
            self.count_failed_runs().label("errors"),
            self.sum_over_runs(c.billable_input_tokens).label("input_tokens"),
            self.sum_over_runs(c.output_tokens).label("output_tokens"),
            self.sum_over_runs(c.cache_read_tokens).label("cache_read_tokens"),
            self.sum_over_runs(c.cache_creation_tokens).label("cache_creation_tokens"),
            self.sum_over_runs(c.total_tokens).label("total_tokens"),
            self.sum_over_runs(c.cost_nano_usd).label("cost_nano"),
            *(self.sum_over_runs(c[f"{key}_nano"]).label(f"{key}_nano") for key in an.COST_SPLIT_COLUMNS),
            self.average_body_tokens().label("avg_est_body_tokens"),
            self.find_last_used().label("last_used"),
        ]


def format_iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return an.as_utc(value).isoformat()


def summarize_totals(row):
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
        "last_used": format_iso(row.get("last_used")),
    }


def fetch_skill_meta(project_id, skill_refs=(), conversation_uuids=()):
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


def describe_skill(skill_id, skill_project_id, stored_name, skills):
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


def resolve_sort_column(sort_by, name_column, columns_by_label):
    if sort_by == "skill_name":
        return name_column
    label = "cost_nano" if sort_by == "cost" else sort_by
    return columns_by_label.get(label, columns_by_label[DEFAULT_SORT])


def count_grouped(statement):
    row = an.fetch_one(select(func.count().label("count")).select_from(statement.subquery()))
    return int((row or {}).get("count") or 0)


def build_skill_leaderboard(project_id, dt_from, dt_to, search="", sort_by=DEFAULT_SORT, sort_order="desc",
                            limit=20, offset=0):
    rows = select_skill_rows(project_id, dt_from, dt_to)
    measures = SkillMeasures(rows)
    columns = measures.label_totals()
    name = measures.pick_skill_name().label("skill_name")
    per_skill = select(rows.c.skill_id, rows.c.skill_project_id, name, *columns).group_by(
        rows.c.skill_id, rows.c.skill_project_id,
    )
    skills_used = count_grouped(per_skill)
    period = summarize_totals(an.fetch_one(select(*columns)) or {})
    matching = per_skill.having(name.ilike(f"%{search}%")) if search else per_skill
    sort_column = resolve_sort_column(sort_by, name, {column.name: column for column in columns})
    order = desc if sort_order == "desc" else asc
    page = an.fetch_all(matching.order_by(
        nulls_last(order(sort_column)), rows.c.skill_project_id, rows.c.skill_id,
    ).limit(limit).offset(offset))
    skills, _ = fetch_skill_meta(project_id, [(r["skill_project_id"], r["skill_id"]) for r in page])
    return {
        "total": count_grouped(matching),
        "summary": {
            "skills_used": skills_used,
            "runs": period["runs"],
            "in_agent_uses": period["in_agent_uses"],
            "total_tokens": period["total_tokens"],
            "cost": period["cost"],
        },
        "rows": [
            {**describe_skill(r["skill_id"], r["skill_project_id"], r["skill_name"], skills), **summarize_totals(r)}
            for r in page
        ],
    }


def fetch_totals_by(measures, *keys, extra=(), order=None, limit=None):
    statement = select(*keys, *extra, *measures.label_totals()).group_by(*keys)
    if order is not None:
        statement = statement.order_by(*order)
    if limit is not None:
        statement = statement.limit(limit)
    return an.fetch_all(statement)


def fetch_detail_head(measures):
    return an.fetch_one(select(
        measures.pick_skill_name().label("skill_name"), func.count().label("rows"), *measures.label_totals(),
    ))


def fetch_daily_trend(measures):
    day = func.date(measures.rows.c.ts, type_=Date).label("day")
    return fetch_totals_by(measures, day, order=[day])


def fetch_version_breakdown(measures):
    version_id = measures.rows.c.version_id
    return fetch_totals_by(measures, version_id, order=[desc(measures.count_runs()), version_id])


def fetch_model_breakdown(measures):
    c = measures.rows.c
    return an.fetch_all(select(
        c.model_name,
        measures.count_runs().label("runs"),
        func.count().label("calls"),
        measures.sum_over_runs(c.total_tokens).label("total_tokens"),
        measures.sum_over_runs(c.cost_nano_usd).label("cost_nano"),
    ).where(measures.is_run, c.event_type == an.EVENT_LLM).group_by(c.model_name).order_by(
        desc(func.count()), c.model_name,
    ))


def fetch_user_breakdown(measures):
    c = measures.rows.c
    return fetch_totals_by(
        measures, c.user_id, extra=[func.max(c.user_email).label("user_email")],
        order=[desc(measures.count_runs()), desc(measures.count_in_agent_uses()), c.user_id],
        limit=DETAIL_USERS_LIMIT,
    )


def fetch_trigger_breakdown(measures):
    trigger_source = measures.rows.c.trigger_source
    return fetch_totals_by(measures, trigger_source, order=[trigger_source])


def fetch_agent_uses(measures):
    c = measures.rows.c
    return an.fetch_all(select(
        c.root_entity_type,
        c.root_entity_id,
        c.root_project_id,
        func.count().label("uses"),
        func.count(distinct(c.run_id)).label("runs"),
        func.count(distinct(c.user_id)).label("users"),
        func.max(c.ts).label("last_used"),
    ).where(measures.is_use, c.root_entity_type.in_(an.AGENT_ROOT_TYPES)).group_by(
        c.root_entity_type, c.root_entity_id, c.root_project_id,
    ).order_by(desc(func.count()), c.root_entity_id).limit(DETAIL_AGENTS_LIMIT))


def fetch_source_split(measures):
    source = measures.rows.c.source
    return an.fetch_all(
        select(source, func.count().label("uses")).where(measures.is_use).group_by(source).order_by(source)
    )


def fetch_recent_runs(measures):
    c = measures.rows.c
    started = func.min(c.ts)
    return an.fetch_all(select(
        c.run_id,
        func.max(c.conversation_id).label("conversation_id"),
        started.label("started_at"),
        func.max(c.user_id).label("user_id"),
        func.max(c.version_id).label("version_id"),
        func.max(c.model_name).label("model_name"),
        func.max(c.trigger_source).label("trigger_source"),
        measures.sum_over_runs(c.total_tokens).label("total_tokens"),
        measures.sum_over_runs(c.cost_nano_usd).label("cost_nano"),
        measures.sum_over_runs(c.duration_ms).label("duration_ms"),
        func.max(case((c.is_error.is_(True), 1), else_=0)).label("is_error"),
    ).where(measures.is_run).group_by(c.run_id).order_by(desc(started)).limit(RECENT_RUNS_LIMIT))


def label_version(version_id, version_names):
    if version_id is None:
        return None
    return version_names.get(version_id) or f"Version #{version_id}"


def label_model(model_name, model_names):
    return model_names.get(model_name) or model_name


def format_model_breakdown(rows, model_names):
    return [
        {
            "model_name": r["model_name"],
            "display_name": label_model(r["model_name"], model_names),
            "runs": int(r["runs"] or 0),
            "calls": int(r["calls"] or 0),
            "total_tokens": int(r["total_tokens"] or 0),
            "cost": an.cost_usd(r["cost_nano"]),
        }
        for r in rows
    ]


def format_agent_uses(rows, agents):
    return [
        {
            "entity_id": a["root_entity_id"],
            "entity_project_id": a["root_project_id"],
            "entity_name": agents.get(agent_ref(a), {}).get("name") or f"Agent #{a['root_entity_id']}",
            "kind": agents.get(agent_ref(a), {}).get("kind"),
            "deleted": agent_ref(a) not in agents,
            "uses": int(a["uses"] or 0),
            "runs": int(a["runs"] or 0),
            "users": int(a["users"] or 0),
            "last_used": format_iso(a["last_used"]),
        }
        for a in rows
    ]


def agent_ref(agent_use):
    return agent_use["root_project_id"], agent_use["root_entity_id"]


def format_recent_runs(rows, conversations, emails, version_names, model_names):
    return [
        {
            "run_id": r["run_id"],
            "conversation_uuid": r["conversation_id"],
            "conversation_id": conversations.get(r["conversation_id"]),
            "started_at": format_iso(r["started_at"]),
            "user_id": r["user_id"],
            "user_email": emails.get(r["user_id"]),
            "version_id": r["version_id"],
            "version_name": label_version(r["version_id"], version_names),
            "model_name": label_model(r["model_name"], model_names),
            "trigger_source": r["trigger_source"] or TRIGGER_MANUAL,
            "total_tokens": int(r["total_tokens"] or 0),
            "cost": an.cost_usd(r["cost_nano"]),
            "duration_ms": int(r["duration_ms"] or 0),
            "is_error": bool(r["is_error"]),
        }
        for r in rows
    ]


def resolve_user_emails(users, recent):
    return an.label_users(
        [r["user_id"] for r in [*users, *recent]],
        {r["user_id"]: r["user_email"] for r in users if r["user_id"] is not None},
    )


def build_skill_detail(project_id, skill_id, skill_project_id, dt_from, dt_to):
    measures = SkillMeasures(select_skill_rows(project_id, dt_from, dt_to, skill_ref=(skill_project_id, skill_id)))
    head = fetch_detail_head(measures)
    if not head or not head["rows"]:
        return None

    daily = fetch_daily_trend(measures)
    versions = fetch_version_breakdown(measures)
    models = fetch_model_breakdown(measures)
    users = fetch_user_breakdown(measures)
    triggers = fetch_trigger_breakdown(measures)
    agents = fetch_agent_uses(measures)
    sources = fetch_source_split(measures)
    recent = fetch_recent_runs(measures)

    skills, conversations = fetch_skill_meta(
        project_id, [(skill_project_id, skill_id)], [r["conversation_id"] for r in recent],
    )
    identity = describe_skill(skill_id, skill_project_id, head["skill_name"], skills)
    version_names = {v.get("id"): v.get("name") for v in identity["versions"]}
    model_names = an.model_display_names(project_id) if models or recent else {}
    agent_meta = an.application_meta(project_id, refs=[agent_ref(a) for a in agents])
    emails = resolve_user_emails(users, recent)

    return {
        **identity,
        "kpis": summarize_totals(head),
        "daily": [{"date": r["day"].isoformat() if r["day"] else None, **summarize_totals(r)} for r in daily],
        "by_version": [
            {
                "version_id": r["version_id"],
                "version_name": label_version(r["version_id"], version_names),
                **summarize_totals(r),
            }
            for r in versions
        ],
        "by_model": format_model_breakdown(models, model_names),
        "by_user": [
            {"user_id": r["user_id"], "user_email": emails.get(r["user_id"]), **summarize_totals(r)} for r in users
        ],
        "by_trigger_source": [
            {"trigger_source": r["trigger_source"] or TRIGGER_MANUAL, **summarize_totals(r)} for r in triggers
        ],
        "agents": format_agent_uses(agents, agent_meta),
        "by_source": [{"source": r["source"] or UNKNOWN_SOURCE, "uses": int(r["uses"] or 0)} for r in sources],
        "recent_runs": format_recent_runs(recent, conversations, emails, version_names, model_names),
    }

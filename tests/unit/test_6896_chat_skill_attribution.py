"""#6896 - a Catalog chat skill used inside an agent run counts for the Catalog skill."""
import pytest

from usage.methods import _skill_analytics as sa

from unit.test_6929_skills_analytics import (  # noqa: F401
    AGENT_ID,
    PROJECT_ID,
    PUBLIC_PROJECT_ID,
    SKILL_ID,
    WINDOW,
    by_skill,
    engine,
    leaderboard,
    meta,
    use,
)

CATALOG_LOAD_META = {"source": "load_skill", "outcome": "loaded", "est_body_tokens": 30,
                     "skill_project_id": PUBLIC_PROJECT_ID}


def catalog_detail():
    return sa.build_skill_detail(PROJECT_ID, SKILL_ID, PUBLIC_PROJECT_ID, *WINDOW)


class TestCatalogChatSkillUse:
    def test_the_use_counts_for_the_catalog_skill_not_the_local_one(self, engine, meta):  # noqa: F811
        use(engine, meta=CATALOG_LOAD_META)

        rows = by_skill(leaderboard())

        assert set(rows) == {(PUBLIC_PROJECT_ID, SKILL_ID)}
        assert rows[(PUBLIC_PROJECT_ID, SKILL_ID)]["in_agent_uses"] == 1

    def test_an_own_skill_use_still_counts_for_the_runs_project(self, engine, meta):  # noqa: F811
        use(engine)

        assert set(by_skill(leaderboard())) == {(PROJECT_ID, SKILL_ID)}

    def test_the_catalog_skill_detail_finds_the_use(self, engine, meta):  # noqa: F811
        use(engine, meta=CATALOG_LOAD_META)

        result = catalog_detail()

        assert result["kpis"]["in_agent_uses"] == 1

    def test_the_agent_using_it_is_named_from_its_own_project(self, engine, meta):  # noqa: F811
        use(engine, meta=CATALOG_LOAD_META)

        [agent] = catalog_detail()["agents"]

        assert (agent["entity_id"], agent["entity_project_id"], agent["entity_name"], agent["deleted"]) == (
            AGENT_ID, PROJECT_ID, "reviewer", False,
        )

    def test_a_skill_run_row_is_never_moved_by_meta(self, engine, meta):  # noqa: F811
        use(engine, event_type="llm", root_entity_type="skill", root_entity_id=SKILL_ID,
            root_entity_project_id=PROJECT_ID, meta={"skill_project_id": PUBLIC_PROJECT_ID})

        assert set(by_skill(leaderboard())) == {(PROJECT_ID, SKILL_ID)}



@pytest.mark.parametrize("stored", ["abc", 1.5, "", 1_000_000_000, 99999999999, None])
def test_a_stored_project_id_that_is_not_a_project_falls_back_to_the_runs_project(engine, meta, stored):  # noqa: F811
    use(engine, meta={"source": "load_skill", "outcome": "loaded", "skill_project_id": stored})

    assert set(by_skill(leaderboard())) == {(PROJECT_ID, SKILL_ID)}


def test_the_largest_project_id_the_writer_records_is_read_back(engine, meta):  # noqa: F811
    use(engine, meta={"source": "load_skill", "outcome": "loaded", "skill_project_id": 999_999_999})

    assert set(by_skill(leaderboard())) == {(999_999_999, SKILL_ID)}

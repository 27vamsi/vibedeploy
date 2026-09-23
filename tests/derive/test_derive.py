"""M4's acceptance check. Readme.md section 8 M4.

  - `flat_todo` and `crm_chain` match hand-written expected models.
  - `messy` stops and asks the right questions.
  - Every table gets a plain-English line, answered or not.

Derivation is pure: it takes the graph and the answers and returns JSON, so
none of this needs a database beyond the `graphs` fixture that M3 already
provides.
"""

from __future__ import annotations

import pytest

from buildjob.derive import (
    Answers,
    MAX_PATH_LENGTH,
    derive,
    is_membership_table,
    paths_to_principal,
)
from tests.derive.expected_models import APP_ID, CRM_CHAIN, FLAT_TODO, SCHEMA_HASH

OWN_DATA = Answers(
    audience="me", size="small", visibility="own_data", sensitive=False
)


def run(graph, answers=OWN_DATA):
    return derive(graph, answers, app_id=APP_ID, schema_hash=SCHEMA_HASH)


# --------------------------------------------------------------------------
# The two clean fixtures
# --------------------------------------------------------------------------


async def test_flat_todo_matches_the_expected_model(graphs):
    result = run(graphs["flat_todo"])
    assert result.questions == ()
    assert result.refusals == ()
    assert result.model == FLAT_TODO


async def test_crm_chain_matches_the_expected_model(graphs):
    result = run(
        graphs["crm_chain"],
        Answers(
            audience="team",
            size="small",
            visibility="own_data",
            sensitive=True,
            follow_ups={
                "unlinked.plans": "read_only_shared",
                "unlinked.audit_log": "admin_only",
            },
        ),
    )
    assert result.questions == ()
    assert result.refusals == ()
    assert result.model == CRM_CHAIN


async def test_the_chain_is_three_hops_and_stays_within_the_limit(graphs):
    """`invoices -> orders -> customers -> users` is what fk_chain exists for."""
    paths = paths_to_principal(graphs["crm_chain"], "invoices", "users")
    assert len(paths) == 1
    assert len(paths[0]) == 3 <= MAX_PATH_LENGTH
    assert [hop["to"] for hop in paths[0]] == ["orders", "customers", "users"]


async def test_an_unlinked_table_is_asked_about_when_nothing_is_sensitive(graphs):
    result = run(graphs["crm_chain"])
    assert {q.id for q in result.questions} == {
        "unlinked.plans",
        "unlinked.audit_log",
    }
    assert result.model is None
    for question in result.questions:
        assert question.options == ("shared", "read_only_shared", "admin_only")


async def test_a_sensitive_app_closes_unlinked_tables_instead_of_asking(graphs):
    """Readme.md section 10: "If Q4 is Yes, unclear tables default to
    admin_only." Taken as decide-and-close, because admin_only is the one
    answer that cannot leak."""
    result = run(
        graphs["crm_chain"],
        Answers(
            audience="team", size="small", visibility="own_data", sensitive=True
        ),
    )
    assert result.questions == ()
    assert result.model["tables"]["plans"] == {"template": "admin_only"}
    assert result.model["tables"]["audit_log"] == {"template": "admin_only"}


async def test_everyone_sees_everything_makes_every_table_shared(graphs):
    """Readme.md section 11 step 3."""
    result = run(
        graphs["crm_chain"],
        Answers(
            audience="team", size="small", visibility="everyone", sensitive=False
        ),
    )
    assert result.questions == ()
    assert all(
        entry == {"template": "shared"} for entry in result.model["tables"].values()
    )
    assert result.model["admin_enabled"] is False


# --------------------------------------------------------------------------
# messy: stop and ask
# --------------------------------------------------------------------------


async def test_messy_asks_which_table_holds_the_people_first(graphs):
    """Two principal candidates (`accounts` and `profiles`), so it must ask.

    Nothing else can be decided until it is answered, which is why this is the
    only question in the first pass.
    """
    result = run(graphs["messy"])
    assert result.model is None
    assert len(result.questions) == 1

    question = result.questions[0]
    assert question.id == "principal_table"
    assert question.text == "Which table holds the people who log in?"
    assert "accounts" in question.options and "profiles" in question.options
    # legacy_docs has a two-column primary key, so it can never be the login
    # table and is not offered.
    assert "legacy_docs" not in question.options

    # Section 11 step 5 still holds while blocked.
    assert len(result.explanation) == len(graphs["messy"]["tables"])


async def test_messy_asks_the_rest_once_the_principal_is_known(graphs):
    result = run(
        graphs["messy"],
        Answers(
            audience="team",
            size="small",
            visibility="own_data",
            sensitive=False,
            follow_ups={"principal_table": "accounts"},
        ),
    )
    assert result.model is None

    assert {q.id for q in result.questions} == {
        # Two columns point at accounts.
        "owner.projects",
        # Three ways to reach accounts: direct, or through either project column.
        "owner.tasks",
        # Reachable from nothing.
        "unlinked.profiles",
        "unlinked.settings",
        "unlinked.legacy_docs",
        # Composite FK reaches legacy_docs, which reaches nobody.
        "unlinked.legacy_notes",
    }

    projects = next(q for q in result.questions if q.id == "owner.projects")
    assert projects.text == "Who owns a row in `projects`: `created_by` or `owner_id`?"
    assert projects.options == ("created_by", "owner_id")

    tasks = next(q for q in result.questions if q.id == "owner.tasks")
    assert tasks.options == (
        "assignee_id",
        "project_id.created_by",
        "project_id.owner_id",
    )


async def test_messy_refuses_the_membership_table(graphs):
    """Readme.md section 11 step 4: membership access is not supported yet."""
    result = run(
        graphs["messy"],
        Answers(
            audience="team",
            size="small",
            visibility="own_data",
            sensitive=False,
            follow_ups={"principal_table": "accounts"},
        ),
    )
    assert [r.table for r in result.refusals] == ["project_members"]
    assert "membership" in result.refusals[0].reason
    assert result.refusals[0].reason in result.explanation

    assert is_membership_table(graphs["messy"], "project_members", "accounts")
    # A plain child table with a composite foreign key is not a junction.
    assert not is_membership_table(graphs["messy"], "legacy_notes", "accounts")


async def test_messy_never_completes_because_of_the_membership_table(graphs):
    """Every question answered, and it still refuses. That is the point."""
    result = run(
        graphs["messy"],
        Answers(
            audience="team",
            size="small",
            visibility="own_data",
            sensitive=False,
            follow_ups={
                "principal_table": "accounts",
                "owner.projects": "owner_id",
                "owner.tasks": "assignee_id",
                "unlinked.profiles": "admin_only",
                "unlinked.settings": "read_only_shared",
                "unlinked.legacy_docs": "admin_only",
                "unlinked.legacy_notes": "admin_only",
            },
        ),
    )
    assert result.questions == ()
    assert [r.table for r in result.refusals] == ["project_members"]
    assert result.model is None


async def test_a_nullable_owner_makes_those_rows_admin_only(graphs):
    """Readme.md section 11 step 4 and section 25 "NULL owners".

    `tasks.assignee_id` is nullable, so rows with no assignee belong to nobody
    and only an admin may see them. It is stated in the confirmation line, not
    asked, because the policy expression already yields nothing for NULL and
    there is no other answer to offer.
    """
    graph = graphs["messy"]
    answers = Answers(
        audience="team",
        size="small",
        visibility="own_data",
        sensitive=True,
        follow_ups={
            "principal_table": "accounts",
            "owner.projects": "owner_id",
            "owner.tasks": "assignee_id",
        },
    )
    result = derive(graph, answers, app_id=APP_ID, schema_hash=SCHEMA_HASH)

    tasks = next(
        line for line in result.explanation if line.startswith("Someone can see and change a row in `tasks`")
    )
    assert "Rows where `tasks.assignee_id` is empty are admin-only." in tasks


# --------------------------------------------------------------------------
# Shape, ordering and the things the Readme insists on
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["flat_todo", "crm_chain", "messy"])
async def test_every_table_gets_a_plain_english_line(graphs, name):
    result = run(graphs[name])
    assert len(result.explanation) == len(graphs[name]["tables"])
    for line in result.explanation:
        assert line.endswith(".") and len(line.split()) >= 5


async def test_derivation_is_deterministic(graphs):
    first = run(graphs["crm_chain"])
    second = run(graphs["crm_chain"])
    assert first == second


async def test_no_template_outside_the_five(graphs):
    allowed = {
        "owner_column",
        "fk_chain",
        "shared",
        "read_only_shared",
        "admin_only",
    }
    for name in ("flat_todo", "crm_chain"):
        answers = Answers(
            audience="team", size="small", visibility="own_data", sensitive=True
        )
        model = derive(
            graphs[name], answers, app_id=APP_ID, schema_hash=SCHEMA_HASH
        ).model
        assert {t["template"] for t in model["tables"].values()} <= allowed


async def test_a_composite_link_is_spelled_differently_on_purpose():
    """Contract 7.2's `column`/`to_column` cannot describe a two-column key.

    A composite hop carries `columns`/`to_columns` *instead of* the singular
    pair, so a reader that only knows the contract shape raises rather than
    quietly joining on the first column alone.
    """
    graph = {
        "version": 1,
        "tables": {
            "accounts": _table(["id"], primary_key=["id"]),
            "docs": _table(
                ["tenant_id", "doc_no", "owner_id"],
                primary_key=["tenant_id", "doc_no"],
                foreign_keys=[
                    {
                        "name": "docs_owner_id_fkey",
                        "columns": ["owner_id"],
                        "references": "accounts",
                        "referenced_columns": ["id"],
                    }
                ],
            ),
            "notes": _table(
                ["id", "tenant_id", "doc_no"],
                primary_key=["id"],
                foreign_keys=[
                    {
                        "name": "notes_tenant_id_doc_no_fkey",
                        "columns": ["tenant_id", "doc_no"],
                        "references": "docs",
                        "referenced_columns": ["tenant_id", "doc_no"],
                    }
                ],
            ),
        },
        "enums": {},
        "views": {},
        "functions": [],
        "policies": {},
    }
    answers = Answers(
        audience="team", size="small", visibility="own_data", sensitive=False
    )
    model = derive(graph, answers, app_id=APP_ID, schema_hash=SCHEMA_HASH).model

    hop = model["tables"]["notes"]["path"][0]
    assert hop["columns"] == ["tenant_id", "doc_no"]
    assert hop["to_columns"] == ["tenant_id", "doc_no"]
    assert "column" not in hop and "to_column" not in hop

    line = next(l for l in model["explanation"] if l.startswith("Someone can see and change a row in `notes`"))
    assert "`notes.(tenant_id, doc_no)` leads to `docs.(tenant_id, doc_no)`" in line


def _table(columns, *, primary_key, foreign_keys=()):
    return {
        "columns": [
            {"name": name, "type": "uuid", "nullable": False, "default": None}
            for name in columns
        ],
        "primary_key": list(primary_key),
        "unique": [],
        "checks": [],
        "foreign_keys": [dict(fk) for fk in foreign_keys],
        "rls_enabled": False,
        "rls_forced": False,
    }

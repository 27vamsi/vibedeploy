"""M3's acceptance check: fixtures match hand-written expected graphs.

Readme.md section 8 M3. The expected graphs in `expected_graphs.py` were written
from the SQL by hand. Nothing here compares the introspector against itself.
"""

from __future__ import annotations

import copy

import pytest

from buildjob.introspect import schema_hash
from tests.derive.expected_graphs import BY_FIXTURE


@pytest.mark.parametrize("name", sorted(BY_FIXTURE))
async def test_the_graph_matches_the_hand_written_one(graphs, name):
    assert graphs[name] == BY_FIXTURE[name]


async def test_the_hash_ignores_what_the_scratch_schema_was_called(graphs):
    """Same SQL, two different app ids, one hash.

    If this fails, `schema_hash` cannot be used to decide "the schema changed,
    re-verify", and contract 7.2 is broken.
    """
    assert graphs["flat_todo"] == graphs["flat_todo_elsewhere"]
    assert schema_hash(graphs["flat_todo"]) == schema_hash(
        graphs["flat_todo_elsewhere"]
    )


async def test_different_schemas_hash_differently(graphs):
    hashes = {name: schema_hash(graph) for name, graph in graphs.items()}
    assert len({hashes["flat_todo"], hashes["crm_chain"], hashes["messy"]}) == 3


async def test_one_changed_column_changes_the_hash(graphs):
    before = schema_hash(graphs["flat_todo"])
    changed = copy.deepcopy(graphs["flat_todo"])
    changed["tables"]["todos"]["columns"].append(
        {"name": "secret", "type": "text", "nullable": True, "default": None}
    )
    assert schema_hash(changed) != before


async def test_the_hash_is_stable_across_calls(graphs):
    assert schema_hash(graphs["messy"]) == schema_hash(graphs["messy"])
    assert schema_hash(graphs["messy"]).startswith("sha256:")


async def test_messy_carries_everything_derivation_must_refuse_on(graphs):
    """The reasons M4 has to stop and ask are all visible in the graph.

    Readme.md section 11 and section 25. Derivation is not written yet; this
    only proves the facts it needs are here to be read.
    """
    graph = graphs["messy"]

    # Two columns on `projects` point at `accounts`, so "the owner" is ambiguous.
    owners = [
        fk["columns"][0]
        for fk in graph["tables"]["projects"]["foreign_keys"]
        if fk["references"] == "accounts"
    ]
    assert sorted(owners) == ["created_by", "owner_id"]

    # `project_members` is a junction table: PK is exactly its two FK columns.
    members = graph["tables"]["project_members"]
    assert members["primary_key"] == ["project_id", "account_id"]

    # A nullable FK on the path means those rows are admin-only.
    assignee = next(
        c for c in graph["tables"]["tasks"]["columns"] if c["name"] == "assignee_id"
    )
    assert assignee["nullable"] is True

    # `settings` is reachable from nothing.
    assert graph["tables"]["settings"]["foreign_keys"] == []

    # Composite FK: the join has to use both columns.
    legacy = graph["tables"]["legacy_notes"]["foreign_keys"][0]
    assert legacy["columns"] == ["tenant_id", "doc_no"]
    assert legacy["referenced_columns"] == ["tenant_id", "doc_no"]

    # A view without security_invoker runs as its owner and skips policies.
    assert graph["views"]["reports"]["security_invoker"] is False
    assert graph["views"]["safe_reports"]["security_invoker"] is True

    # A SECURITY DEFINER function that is not ours is a hole.
    assert graph["functions"] == [
        {"name": "bump_task_count", "arguments": "p uuid", "security_definer": True}
    ]


async def test_nothing_is_flagged_before_the_kernel_runs(graphs):
    """Sanity: the graph is read pre-kernel, so RLS is off and there are no
    policies. If this ever passes trivially because the fixture changed, the
    equality tests above would already have caught it."""
    for name in ("flat_todo", "crm_chain", "messy"):
        graph = graphs[name]
        assert graph["policies"] == {}
        assert all(not t["rls_enabled"] for t in graph["tables"].values())
        assert all(not t["rls_forced"] for t in graph["tables"].values())

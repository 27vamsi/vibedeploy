"""The acts-for escape tests. Readme.md section 8 M11, section 24.

**Permanently protected.** Never delete or skip one of these to make CI green.
They are the whole claim of the second half of the product: an agent acting for
Alice cannot reach Bob's rows, by any route, including ones nobody thought of
when the policy was written.

Every expectation comes from the seed plan. None of them come from reading a
policy back — a test that asks the thing under test what it should expect proves
only that it is self-consistent.

The agent here is not restrained by the gateway's own policy: these all run with
a permissive policy on purpose. What stops them is the database.
"""

from __future__ import annotations

import uuid

import pytest

from gateway.connectors.postgres import Creds, PostgresConnector, SchemaCatalog
from gateway.errors import Denied
from gateway.policy import Limits
from tests.gateway.conftest import creds_for


def _keys(rows, column="id"):
    return {str(row[column]) for row in rows}


def _one(built, table, persona):
    row = built.plan.one_owned_by(table, persona)
    assert row is not None, f"the seed plan has no {table} owned by {persona}"
    return row


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def test_a_query_with_no_filters_returns_only_alices_rows(
    crm, connector, alice
):
    """The unfiltered read. This is the same shape as the app endpoint that
    does `SELECT * FROM invoices` with no WHERE clause, and it has to end the
    same way."""
    result = await connector.execute(alice, "db_query", {"table": "invoices"}, None)

    assert result.ok
    expected = {str(key[0]) for key in crm.plan.owned_by("invoices", "A")}
    assert _keys(result.rows) == expected
    assert expected, "the fixture must actually contain rows of Alice's"

    forbidden = {str(key[0]) for key in crm.plan.owned_by("invoices", "B")}
    assert forbidden and not (_keys(result.rows) & forbidden)


async def test_asking_for_bobs_row_by_id_returns_nothing(crm, connector, alice):
    """Naming the row does not help. The filter is applied on top of the
    policy, never instead of it."""
    bob = _one(crm, "invoices", "B")
    result = await connector.execute(
        alice,
        "db_query",
        {
            "table": "invoices",
            "filters": [{"column": "id", "op": "=", "value": str(bob.key[0])}],
        },
        None,
    )

    assert result.ok
    assert result.rows == []


async def test_an_agent_sees_exactly_what_its_person_sees(crm, connector):
    """Section 13's parity check, from the other side. Two people, one agent
    each, and neither sees a row of the other's."""
    for persona in ("A", "B"):
        creds = creds_for(crm, persona)
        result = await connector.execute(creds, "db_query", {"table": "customers"}, None)
        expected = {str(key[0]) for key in crm.plan.owned_by("customers", persona)}
        assert _keys(result.rows) == expected


async def test_a_read_never_returns_more_than_the_policys_limit(crm, catalog):
    connector = PostgresConnector(catalog=catalog, limits=Limits(max_rows_read=1))
    creds = creds_for(crm, "A")
    result = await connector.execute(creds, "db_query", {"table": "customers"}, None)
    assert len(result.rows) == 1


# ---------------------------------------------------------------------------
# Writing at somebody else
# ---------------------------------------------------------------------------


async def test_updating_bobs_row_changes_nothing_and_says_so(crm, connector, alice):
    bob = _one(crm, "customers", "B")
    dry = await connector.dry_run(
        alice,
        "db_update",
        {
            "table": "customers",
            "filters": [{"column": "id", "op": "=", "value": str(bob.key[0])}],
            "values": {"name": "renamed by an agent"},
        },
    )

    assert dry.affected == 0
    assert dry.note == "No rows you can access matched."
    assert dry.diff == []


async def test_deleting_bobs_row_changes_nothing_and_says_so(crm, connector, alice):
    bob = _one(crm, "customers", "B")
    dry = await connector.dry_run(
        alice,
        "db_delete",
        {
            "table": "customers",
            "filters": [{"column": "id", "op": "=", "value": str(bob.key[0])}],
        },
    )

    assert dry.affected == 0
    assert dry.note == "No rows you can access matched."


async def test_bobs_row_really_is_still_there_afterwards(crm, gateway_admin):
    """The probes above are rolled back, so this asks the database directly,
    as the admin, whether anything moved."""
    bob = _one(crm, "customers", "B")
    row = await gateway_admin.fetchrow(
        f'SELECT name FROM "{crm.roles.schema}".customers WHERE id = $1', bob.key[0]
    )
    assert row is not None
    assert row["name"] != "renamed by an agent"


async def test_creating_a_row_owned_by_bob_is_refused_by_the_database(
    crm, connector, alice
):
    bob_id = crm.plan.user_ids["B"]
    with pytest.raises(Denied) as refusal:
        await connector.dry_run(
            alice,
            "db_create",
            {"table": "customers", "values": {"rep_id": str(bob_id), "name": "theirs"}},
        )

    assert refusal.value.code == "database_refused"
    assert refusal.value.reason


async def test_reassigning_alices_own_row_to_bob_is_refused(crm, connector, alice):
    """Section 25, "owner reassignment". The row is hers to change, and this
    is still not a change she may make: WITH CHECK is evaluated against the
    row as it would be *after* the update."""
    mine = _one(crm, "customers", "A")
    bob_id = crm.plan.user_ids["B"]

    with pytest.raises(Denied) as refusal:
        await connector.dry_run(
            alice,
            "db_update",
            {
                "table": "customers",
                "filters": [{"column": "id", "op": "=", "value": str(mine.key[0])}],
                "values": {"rep_id": str(bob_id)},
            },
        )

    assert refusal.value.code == "database_refused"


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


UNKNOWN_NAMES = [
    "nonexistent",
    "pg_shadow",
    "users; DROP TABLE users",
    'customers" --',
    "SELECT",
    "vd_control.audit_log",
    "public.users",
    "",
]


@pytest.mark.parametrize("table", UNKNOWN_NAMES)
async def test_a_table_the_schema_does_not_have_is_refused(catalog, table):
    """Refused before a connection is opened, which is why the credentials
    here point nowhere. If validation ever moved after connecting, this test
    would fail with a connection error instead of a refusal."""
    connector = PostgresConnector(catalog=catalog, limits=Limits())
    nowhere = Creds(
        dsn="postgresql://nobody:nobody@127.0.0.1:1/nothing",
        schema=catalog.schema,
        user_id=str(uuid.uuid4()),
        role="member",
    )

    with pytest.raises(Denied) as refusal:
        await connector.dry_run(
            nowhere, "db_update", {"table": table, "values": {"name": "x"}}
        )
    assert refusal.value.code == "unknown_table"


@pytest.mark.parametrize(
    "column", ["nonexistent", "id; DROP TABLE customers", 'name" = "x', "ctid", "oid"]
)
async def test_a_column_the_table_does_not_have_is_refused(catalog, column):
    connector = PostgresConnector(catalog=catalog, limits=Limits())
    nowhere = Creds(
        dsn="postgresql://nobody:nobody@127.0.0.1:1/nothing",
        schema=catalog.schema,
        user_id=str(uuid.uuid4()),
        role="member",
    )

    with pytest.raises(Denied) as refusal:
        await connector.dry_run(
            nowhere,
            "db_query",
            {
                "table": "customers",
                "filters": [{"column": column, "op": "=", "value": "x"}],
            },
        )
    assert refusal.value.code == "unknown_column"


async def test_a_comparison_nobody_offered_is_refused(catalog, alice):
    connector = PostgresConnector(catalog=catalog, limits=Limits())
    with pytest.raises(Denied) as refusal:
        await connector.dry_run(
            alice,
            "db_query",
            {
                "table": "customers",
                "filters": [{"column": "name", "op": "~ '.*' OR 1=1 --", "value": "x"}],
            },
        )
    assert refusal.value.code == "unknown_operator"


async def test_the_allowlist_is_the_schema_that_was_proved(crm, catalog):
    """The tables an agent may name are exactly the ones the deployment
    introspected, so a table added behind our back is not reachable until it
    has been through a deploy."""
    assert set(catalog.tables) == set(crm.graph["tables"])
    assert "audit_log" in catalog.tables, "even admin-only tables are nameable"


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


async def test_changing_more_rows_than_allowed_is_refused_at_dry_run(crm, catalog):
    connector = PostgresConnector(catalog=catalog, limits=Limits(max_rows_changed=1))
    alice = creds_for(crm, "A")

    with pytest.raises(Denied) as refusal:
        await connector.dry_run(
            alice, "db_update", {"table": "customers", "values": {"name": "all of them"}}
        )

    assert refusal.value.code == "limit_exceeded"
    assert "1" in refusal.value.reason


async def test_the_refused_bulk_update_did_not_happen(crm, gateway_admin):
    rows = await gateway_admin.fetch(
        f'SELECT name FROM "{crm.roles.schema}".customers'
    )
    assert not any(row["name"] == "all of them" for row in rows)


# ---------------------------------------------------------------------------
# The dry run is a dry run
# ---------------------------------------------------------------------------


async def test_a_dry_run_leaves_the_database_alone(crm, connector, gateway_admin):
    alice = creds_for(crm, "A")
    mine = _one(crm, "customers", "A")

    dry = await connector.dry_run(
        alice,
        "db_update",
        {
            "table": "customers",
            "filters": [{"column": "id", "op": "=", "value": str(mine.key[0])}],
            "values": {"name": "only in the dry run"},
        },
    )
    assert dry.affected == 1
    assert dry.diff_hash

    name = await gateway_admin.fetchval(
        f'SELECT name FROM "{crm.roles.schema}".customers WHERE id = $1', mine.key[0]
    )
    assert name != "only in the dry run"


async def test_the_same_dry_run_hashes_the_same_way(crm, connector):
    alice = creds_for(crm, "A")
    mine = _one(crm, "customers", "A")
    args = {
        "table": "customers",
        "filters": [{"column": "id", "op": "=", "value": str(mine.key[0])}],
        "values": {"name": "stable"},
    }

    first = await connector.dry_run(alice, "db_update", args)
    second = await connector.dry_run(alice, "db_update", args)
    assert first.diff_hash == second.diff_hash


async def test_executing_something_other_than_what_was_approved_is_refused(
    crm, connector
):
    """Section 19.4: an approval is bound to the diff hash. `execute` is handed
    the dry run it is allowed to perform and will not do anything else."""
    from gateway.connectors.base import DryRun

    alice = creds_for(crm, "A")
    mine = _one(crm, "customers", "A")
    args = {
        "table": "customers",
        "filters": [{"column": "id", "op": "=", "value": str(mine.key[0])}],
        "values": {"name": "changed"},
    }
    stale = DryRun(affected=1, diff_hash="sha256:something-else")

    result = await connector.execute(alice, "db_update", args, stale)
    assert not result.ok
    assert "changed since" in (result.reason or "")


# ---------------------------------------------------------------------------
# Tools an agent is never even shown
# ---------------------------------------------------------------------------


async def test_a_forbidden_tool_is_not_listed_and_is_not_callable(catalog, alice):
    from gateway.policy import load

    policy = load(
        "agent: reader\nacts_for: a@example.com\n"
        "postgres:\n  default: {read: auto}\n",
        version=1,
    )
    connector = PostgresConnector(catalog=catalog, limits=Limits())

    listed = {tool.name for tool in connector.tools(policy)}
    assert listed == {"db_query"}
    assert not policy.allows("postgres", "db_delete", "customers")


async def test_there_is_no_tool_that_takes_sql(catalog):
    connector = PostgresConnector(catalog=catalog, limits=Limits())
    for tool in connector.tools(_permissive()):
        assert "sql" not in tool.input_schema.get("properties", {})
        assert "query" not in tool.input_schema.get("properties", {})
    assert set(connector.TOOLS) == {"db_query", "db_create", "db_update", "db_delete"}


def _permissive():
    from gateway.policy import load

    return load(
        "agent: everything\nacts_for: a@example.com\n"
        "postgres:\n  default: {read: auto, create: auto, update: auto, delete: approve}\n",
        version=1,
    )


def test_the_catalog_refuses_a_schema_name_it_did_not_introspect(crm):
    with pytest.raises(Denied):
        SchemaCatalog.from_graph("not the schema; DROP DATABASE x", crm.graph)

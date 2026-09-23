"""Fake data, and the plan that made it. Readme.md section 12.

Runs in the scratch database as the **migrator**, before any attack. Two things
matter more than the data itself:

  - **The plan is the answer key.** Readme.md section 3 rule 10: what a persona
    is allowed to see is recorded here, from the inserts we chose to do. The
    attack never reads a policy back to work out what it should expect, because
    a test that derives its expectations from the thing under test proves
    nothing.
  - **Every persona gets a full chain.** A row owned by A is only useful if its
    parents, and its parents' parents, are A's too. Otherwise a chain policy
    could be broken in both directions at once and still look right.

Personas are A and B (members), ADMIN, and NONE, who has no row anywhere and
never sets an identity.
"""

from __future__ import annotations

import datetime as dt
import decimal
import itertools
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import asyncpg

from kernel.render import quote_ident

MEMBERS = ("A", "B")
PERSONAS = ("A", "B", "ADMIN", "NONE")
ROWS_PER_MEMBER = 2
CHECK_RETRIES = 20


class SeedError(RuntimeError):
    """Seeding could not produce honest data, so nothing may be proven."""


@dataclass(frozen=True)
class SeededRow:
    table: str
    key: tuple
    owner: str | None
    values: dict[str, Any]


@dataclass
class SeedPlan:
    """What was inserted and who owns it. The only source of expectations."""

    principal: str
    key_column: str
    user_ids: dict[str, Any] = field(default_factory=dict)
    rows: dict[str, list[SeededRow]] = field(default_factory=dict)

    def keys(self, table: str) -> set[tuple]:
        return {row.key for row in self.rows[table]}

    def owned_by(self, table: str, persona: str) -> set[tuple]:
        return {row.key for row in self.rows[table] if row.owner == persona}

    def ownerless(self, table: str) -> set[tuple]:
        """Rows nobody owns: unowned tables, and NULL owner columns."""
        return {row.key for row in self.rows[table] if row.owner is None}

    def one_owned_by(self, table: str, persona: str) -> SeededRow | None:
        for row in self.rows[table]:
            if row.owner == persona:
                return row
        return None


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------


class Values:
    """Type-correct, unique-by-construction column values.

    A counter rather than randomness: a seeded database that cannot be
    reproduced cannot be used to explain a failure.
    """

    def __init__(self, enums: Mapping[str, Sequence[str]]):
        self._enums = enums
        self._counter = itertools.count(1)

    def next_int(self) -> int:
        return next(self._counter)

    def for_column(self, column: Mapping[str, Any]) -> Any:
        name, type_name = column["name"], column["type"]
        n = self.next_int()

        if type_name.endswith("[]"):
            inner = dict(column, type=type_name[:-2])
            return [self.for_column(inner)]
        if type_name in self._enums:
            labels = self._enums[type_name]
            return labels[n % len(labels)]

        base = type_name.split("(")[0].strip()
        if base == "uuid":
            return uuid.uuid4()
        if base in ("text", "character varying", "character", "citext", "name"):
            return f"{name}-{n}"
        if base in ("smallint", "integer", "bigint"):
            return n
        if base in ("numeric", "decimal"):
            return decimal.Decimal(n)
        if base in ("real", "double precision"):
            return float(n)
        if base == "boolean":
            return n % 2 == 0
        if base == "timestamp with time zone":
            return dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(
                minutes=n
            )
        if base == "timestamp without time zone":
            return dt.datetime(2026, 1, 1) + dt.timedelta(minutes=n)
        if base == "date":
            return dt.date(2026, 1, 1) + dt.timedelta(days=n % 365)
        if base in ("json", "jsonb"):
            return f'{{"n": {n}}}'
        if base == "inet":
            return f"10.0.0.{n % 255}"
        raise SeedError(
            f"`{column['name']}` is of type `{type_name}`, which the seeder"
            " cannot generate a value for, so this app cannot be verified yet."
        )


# --------------------------------------------------------------------------
# Order
# --------------------------------------------------------------------------


def insert_order(graph: Mapping[str, Any], tables: Sequence[str]) -> list[str]:
    """Parents before children. Self-references are ignored (section 11 step 2).

    A remaining cycle is reported rather than guessed at: Readme.md section 12
    step 2 allows deferrable constraints for that case, and until that is built
    a cycle blocks the deploy instead of producing data nobody can vouch for.
    """
    wanted = set(tables)
    parents = {
        table: {
            fk["references"]
            for fk in graph["tables"][table]["foreign_keys"]
            if fk["references"] != table and fk["references"] in wanted
        }
        for table in tables
    }
    ordered: list[str] = []
    done: set[str] = set()
    while len(ordered) < len(tables):
        ready = sorted(t for t in tables if t not in done and parents[t] <= done)
        if not ready:
            stuck = sorted(set(tables) - done)
            raise SeedError(
                "These tables reference each other in a loop, which is not"
                f" supported yet: {', '.join(stuck)}."
            )
        ordered.extend(ready)
        done.update(ready)
    return ordered


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def _fk_for(entry: Mapping[str, Any], column: str) -> Mapping[str, Any] | None:
    for fk in entry["foreign_keys"]:
        if column in fk["columns"]:
            return fk
    return None


def _first_hop_columns(model_entry: Mapping[str, Any]) -> list[str]:
    hop = model_entry["path"][0]
    return [hop["column"]] if "column" in hop else list(hop["columns"])


async def seed(
    conn: asyncpg.Connection,
    roles,
    graph: Mapping[str, Any],
    model: Mapping[str, Any],
) -> SeedPlan:
    """Insert the whole fixture and return the plan that describes it."""
    principal = model["principal"]["table"]
    key_column = model["principal"]["key"]
    plan = SeedPlan(principal=principal, key_column=key_column)
    values = Values(graph["enums"])

    for table in insert_order(graph, sorted(model["tables"])):
        plan.rows[table] = []
        entry = model["tables"][table]
        if table == principal:
            await _seed_principal(conn, roles, graph, plan, values, table)
        else:
            await _seed_table(conn, roles, graph, model, plan, values, table, entry)
    return plan


async def _seed_principal(conn, roles, graph, plan, values, table) -> None:
    """One row per person who can log in, including the admin."""
    for persona in ("A", "B", "ADMIN"):
        row = await _insert(
            conn, roles, graph, plan, values, table, overrides={}, owner=persona
        )
        plan.user_ids[persona] = row.values[plan.key_column]


async def _seed_table(conn, roles, graph, model, plan, values, table, entry) -> None:
    template = entry["template"]

    if template in ("shared", "read_only_shared", "admin_only"):
        # Nobody owns these, so there is no persona to build a chain for.
        for _ in range(ROWS_PER_MEMBER):
            await _insert(
                conn, roles, graph, plan, values, table, overrides={}, owner=None
            )
        return

    for persona in MEMBERS:
        for index in range(ROWS_PER_MEMBER):
            overrides = _ownership_overrides(
                graph, plan, table, entry, persona, index
            )
            await _insert(
                conn,
                roles,
                graph,
                plan,
                values,
                table,
                overrides=overrides,
                owner=persona,
            )

    if entry.get("admin_only_rows"):
        # A nullable owner column means rows that belong to nobody. Section 25
        # "NULL owners": they must be visible to an admin and to no one else,
        # so the plan has to contain one to prove it.
        column = entry["admin_only_rows"]
        await _insert(
            conn,
            roles,
            graph,
            plan,
            values,
            table,
            overrides={column: None},
            owner=None,
        )


def _ownership_overrides(graph, plan, table, entry, persona, index) -> dict:
    """The columns that decide who owns the row we are about to insert."""
    if entry["template"] == "owner_column":
        return {entry["column"]: plan.user_ids[persona]}

    columns = _first_hop_columns(entry)
    hop = entry["path"][0]
    parent_columns = (
        [hop["to_column"]] if "to_column" in hop else list(hop["to_columns"])
    )
    candidates = [r for r in plan.rows[hop["to"]] if r.owner == persona]
    if not candidates:
        raise SeedError(
            f"`{table}` needs a row in `{hop['to']}` owned by {persona} to hang"
            " off, and there is none."
        )
    parent = candidates[index % len(candidates)]
    return {
        child: parent.values[parent_column]
        for child, parent_column in zip(columns, parent_columns, strict=True)
    }


def build_row(
    graph: Mapping[str, Any],
    model: Mapping[str, Any],
    plan: SeedPlan,
    values: Values,
    table: str,
    persona: str | None = None,
    *,
    index: int = 0,
) -> dict:
    """A row that *would* belong to `persona`, built but not inserted.

    The attack suite needs this to try writing a row into someone else's name.
    It has to be built the same way the seeder builds one, or an insert could
    be refused for an unrelated reason and be mistaken for the policy working.
    """
    entry = model["tables"][table]
    if persona is not None and entry["template"] in ("owner_column", "fk_chain"):
        overrides = _ownership_overrides(graph, plan, table, entry, persona, index)
    else:
        overrides = {}
    return _build_row(
        graph, plan, values, table, graph["tables"][table], overrides, persona
    )


async def _insert(
    conn, roles, graph, plan, values, table, *, overrides: dict, owner: str | None
) -> SeededRow:
    entry = graph["tables"][table]
    schema = quote_ident(roles.schema)
    target = f"{schema}.{quote_ident(table)}"

    last: Exception | None = None
    for _ in range(CHECK_RETRIES):
        row_values = _build_row(graph, plan, values, table, entry, overrides, owner)
        columns = sorted(row_values)
        try:
            if columns:
                placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
                sql = (
                    f"INSERT INTO {target} "
                    f"({', '.join(quote_ident(c) for c in columns)}) "
                    f"VALUES ({placeholders}) RETURNING *"
                )
                record = await conn.fetchrow(sql, *(row_values[c] for c in columns))
            else:
                record = await conn.fetchrow(
                    f"INSERT INTO {target} DEFAULT VALUES RETURNING *"
                )
        except (asyncpg.CheckViolationError, asyncpg.UniqueViolationError) as exc:
            # Section 12 step 4: try other values, then name the constraint
            # rather than silently seeding something that does not fit.
            last = exc
            continue

        stored = dict(record)
        seeded = SeededRow(
            table=table,
            key=tuple(stored[c] for c in entry["primary_key"]),
            owner=owner,
            values=stored,
        )
        plan.rows[table].append(seeded)
        return seeded

    raise SeedError(
        f"Could not find values for `{table}` that satisfy"
        f" `{getattr(last, 'constraint_name', 'its constraints')}` after"
        f" {CHECK_RETRIES} tries."
    )


def _build_row(graph, plan, values, table, entry, overrides, owner) -> dict:
    row: dict[str, Any] = {}
    for column in entry["columns"]:
        name = column["name"]
        if name in overrides:
            row[name] = overrides[name]
            continue

        fk = _fk_for(entry, name)
        if fk is not None:
            chosen = _parent_value(plan, fk, name, owner)
            if chosen is None and not column["nullable"]:
                raise SeedError(
                    f"`{table}.{name}` must point at a row in"
                    f" `{fk['references']}` and there is none to point at."
                )
            row[name] = chosen
            continue

        if column["default"] is not None:
            # Let the database apply it: that is what the app would get, and it
            # keeps sequences and generated keys out of our hands.
            continue
        row[name] = values.for_column(column)
    return row


def _parent_value(plan, fk, column, owner):
    """Point a foreign key at a real parent, preferring the same persona's."""
    parent_table = fk["references"]
    parent_column = fk["referenced_columns"][fk["columns"].index(column)]
    rows = plan.rows.get(parent_table) or []
    if not rows:
        return None
    preferred = [r for r in rows if r.owner == owner] or rows
    return preferred[0].values[parent_column]

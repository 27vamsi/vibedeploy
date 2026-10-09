"""The Postgres connector. Readme.md section 20.1, contract 7.7.

This is the whole second half of the product in one file, and the reason it is
short is that it does not enforce anything. It connects as the app's `agent`
role — NOBYPASSRLS, NOINHERIT, owner of nothing — sets the acts-for person's
identity for the transaction, and then writes ordinary SQL. Every "Bob's row is
not yours" in the tests next to this file is decided by the same RLS policies
that decide it for the app. There is no second security model here to get wrong.

What this file *is* responsible for is making sure the SQL it writes says only
what an agent is allowed to say:

  - **Names come from the allowlist, never from the agent.** Tables and columns
    are looked up in the schema the deployment introspected and proved. A name
    that is not in it is refused before a connection is even opened, which is
    why a table called `users; DROP TABLE users` is not a clever attack, it is
    just a name nobody has.
  - **Values are never concatenated.** Every value is bound, and bound as text
    with the column's own type cast around it, so the database parses it. An
    agent's JSON `"2026-01-01"` becomes a timestamp because Postgres says so,
    and a malformed one is a database refusal rather than a Python crash.
  - **A dry run is a real run that was rolled back.** Not an estimate, not a
    plan: the actual statement, with `RETURNING *`, inside a transaction that
    ends in ROLLBACK. That is the only way "what would happen" can be trusted,
    because it is what happened.
  - **`execute` is handed the dry run it may perform.** If the diff it produces
    is not the one that was approved, it rolls back and says so (19.4).

The one thing worth staring at is `_change`, which both `dry_run` and `execute`
call. They differ only in whether the caller commits. If they were two separate
implementations they would drift, and the drift would be invisible: the dry run
would keep showing the old answer while execution did something else.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

import asyncpg

from gateway.audit import canonical
from gateway.connectors.base import DryRun, Result, ToolSpec, UndoResult
from gateway.errors import Denied
from gateway.policy import AgentPolicy, Limits
from gateway.risk import score
from kernel.render import quote_ident

# Section 20.1's list, and nothing else is ever added to it. In particular
# there is no tool that takes SQL, a WHERE fragment or a column expression.
COMPARISONS = {"=": "=", "!=": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
OPS = tuple(COMPARISONS) + ("in", "is_null")

# The words a builder reads when an agent aimed at somebody else. Fixed, so the
# same thing is always reported the same way.
NOTHING_MATCHED = "No rows you can access matched."
NOTHING_TO_CHANGE = "This only reads; nothing would change."

UNDO_WINDOW = timedelta(hours=24)

# A schema name we are willing to put in a statement. Quoting already makes any
# name inert, but a schema that does not look like one we created means we have
# been handed the wrong thing, and being loud about it is better than quoting it
# and querying nothing.
_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")

# `format_type` output: `uuid`, `numeric(12,2)`, `timestamp with time zone`,
# `text[]`. Types are not agent input — they come from our own introspection —
# but they are the only thing in a statement that is not either quoted or bound,
# so they are checked rather than trusted.
_TYPE_NAME = re.compile(r"[a-z_][a-z0-9_ ]*(\([0-9, ]+\))?(\[\])*\Z")


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    name: str
    type: str


@dataclass(frozen=True)
class Table:
    name: str
    columns: Mapping[str, Column]
    primary_key: tuple[str, ...]


@dataclass(frozen=True)
class SchemaCatalog:
    """Every name an agent is allowed to say, and nothing else.

    Built from the graph a deployment introspected and then proved, so a table
    somebody added behind our back is unreachable until it has been through a
    deploy. Admin-only tables are in here too: being nameable is not being
    readable, and what an agent may actually do with `audit_log` is decided by
    its policy and then by the database.
    """

    schema: str
    tables: Mapping[str, Table]
    enums: frozenset[str]

    @classmethod
    def from_graph(cls, schema: str, graph: Mapping[str, Any]) -> "SchemaCatalog":
        if (
            not isinstance(schema, str)
            or not _SCHEMA_NAME.fullmatch(schema)
            or len(schema.encode()) > 63
        ):
            raise Denied(
                "unknown_schema",
                "That is not the name of a schema this deployment introspected.",
            )
        tables = {
            name: Table(
                name=name,
                columns={
                    column["name"]: Column(name=column["name"], type=column["type"])
                    for column in entry["columns"]
                },
                primary_key=tuple(entry.get("primary_key") or ()),
            )
            for name, entry in graph["tables"].items()
        }
        return cls(
            schema=schema,
            tables=tables,
            enums=frozenset(graph.get("enums") or ()),
        )

    def table(self, name: Any) -> Table:
        if not isinstance(name, str) or name not in self.tables:
            raise Denied(
                "unknown_table",
                f"There is no table called `{name}` in this app. You can use:"
                f" {', '.join(sorted(self.tables))}.",
            )
        return self.tables[name]

    def column(self, table: Table, name: Any) -> Column:
        if not isinstance(name, str) or name not in table.columns:
            raise Denied(
                "unknown_column",
                f"`{table.name}` has no column called `{name}`. It has:"
                f" {', '.join(table.columns)}.",
            )
        return table.columns[name]

    def cast(self, column: Column) -> str:
        """The SQL type to parse a bound text value into."""
        base, suffix = column.type, ""
        while base.endswith("[]"):
            base, suffix = base[:-2], suffix + "[]"
        if base in self.enums:
            return f"{quote_ident(self.schema)}.{quote_ident(base)}{suffix}"
        if not _TYPE_NAME.fullmatch(column.type):
            raise Denied(
                "unsupported_type",
                f"`{column.name}` is a `{column.type}`, which agents cannot"
                " safely be given a value for.",
            )
        return column.type

    def qualified(self, table: Table) -> str:
        return f"{quote_ident(self.schema)}.{quote_ident(table.name)}"


@dataclass(frozen=True)
class Creds:
    """One connection's worth of access: the app's agent role, as one person.

    Nothing in here can be widened by anything below. The role is the app's
    `agent`, the identity is set per transaction, and the gateway never sees
    the runtime or migrator secret at all.
    """

    dsn: str
    schema: str
    user_id: str
    role: str = "member"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Row values as something that can live in JSONB and be compared later."""
    return json.loads(canonical(value))


def _as_text(value: Any) -> str | None:
    """Every bound value goes to Postgres as text and is cast there.

    An agent's arguments arrive as JSON, so they are strings, numbers, booleans
    and nulls whatever the column actually is. Letting the database parse them
    means a bad date is a database refusal with a message, rather than a driver
    type error halfway through building a statement.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return canonical(value)
    return str(value)


class _Params:
    """Bound values, in order. The only way a value reaches a statement."""

    def __init__(self) -> None:
        self.values: list[Any] = []

    def add(self, value: Any) -> str:
        self.values.append(value)
        return f"${len(self.values)}"


def _refused(exc: asyncpg.PostgresError) -> Denied:
    if isinstance(exc, asyncpg.InsufficientPrivilegeError):
        detail = "That is not a row you are allowed to write."
    else:
        detail = getattr(exc, "message", None) or str(exc)
    return Denied("database_refused", f"The database refused this. {detail}")


# ---------------------------------------------------------------------------
# The connector
# ---------------------------------------------------------------------------


class PostgresConnector:
    """Contract 7.7, against one app's schema."""

    name = "postgres"
    TOOLS = ("db_query", "db_create", "db_update", "db_delete")

    def __init__(self, *, catalog: SchemaCatalog, limits: Limits):
        self.catalog = catalog
        self.limits = limits

    # -- what an agent is shown ---------------------------------------------

    def tools(self, policy: AgentPolicy) -> list[ToolSpec]:
        """Only tools the policy leaves open, and only on the tables it leaves
        open. A forbidden tool is not listed, so an agent is never told what it
        is not allowed to do (contract 7.5)."""
        specs = []
        for tool in self.TOOLS:
            tables = [
                name
                for name in sorted(self.catalog.tables)
                if policy.allows(self.name, tool, name)
            ]
            if tables:
                specs.append(
                    ToolSpec(
                        name=tool,
                        connector=self.name,
                        description=_DESCRIPTIONS[tool],
                        input_schema=_input_schema(tool, tables),
                    )
                )
        return specs

    def risk(self, tool: str, args: dict[str, Any]) -> str:
        """Before the dry run, so the row count is not known yet. The pipeline
        scores `db_update` again once it is."""
        return score(tool)

    async def scoped_credentials(self, session: Any) -> Creds:
        """The agent role for this app, pinned to the person the session acts
        for. The session is the only thing that decides who that is."""
        return Creds(
            dsn=session.dsn,
            schema=session.schema,
            user_id=str(session.acts_for),
            role=session.role,
        )

    # -- the two calls that do something ------------------------------------

    async def dry_run(self, creds: Creds, tool: str, args: dict[str, Any]) -> DryRun:
        table = self._checked(tool, args)
        if tool == "db_query":
            # A read is not run twice. Nothing would change, and saying so
            # costs the person's database nothing.
            return DryRun(affected=0, diff_hash=_hash(tool, table.name, []),
                          note=NOTHING_TO_CHANGE)

        conn = await self._connect(creds)
        tx = conn.transaction()
        await tx.start()
        try:
            await self._identify(conn, creds)
            return await self._change(conn, tool, args, table)
        except asyncpg.PostgresError as exc:
            raise _refused(exc) from exc
        finally:
            await tx.rollback()
            await conn.close()

    async def execute(
        self, creds: Creds, tool: str, args: dict[str, Any], expected: DryRun | None
    ) -> Result:
        table = self._checked(tool, args)
        if tool == "db_query":
            return await self._read(creds, args, table)

        if expected is None or not expected.diff_hash:
            return Result(
                ok=False,
                reason="This has not been checked yet, so it will not be run.",
            )

        conn = await self._connect(creds)
        tx = conn.transaction()
        await tx.start()
        try:
            await self._identify(conn, creds)
            actual = await self._change(conn, tool, args, table)
            if (
                actual.diff_hash != expected.diff_hash
                or actual.affected != expected.affected
            ):
                # 19.4: the approval was for what the dry run showed. It is not
                # approval for whatever this run happens to find.
                await tx.rollback()
                return Result(
                    ok=False,
                    reason=(
                        "The data has changed since this was checked, so nothing"
                        " was done. It needs looking at again."
                    ),
                )
            await tx.commit()
        except asyncpg.PostgresError as exc:
            await tx.rollback()
            raise _refused(exc) from exc
        except BaseException:
            await tx.rollback()
            raise
        finally:
            await conn.close()

        rows = actual.before if tool == "db_delete" else actual.after
        # `note` is set when nothing matched, which is what an agent aiming at
        # somebody else's rows looks like from in here. Carrying it out means
        # the action says so, rather than reporting a silent success.
        return Result(
            ok=True, affected=actual.affected, rows=rows, reason=actual.note
        )

    # -- afterwards ----------------------------------------------------------

    async def verify(
        self, creds: Creds, tool: str, args: dict[str, Any], result: Result
    ) -> bool:
        """Re-read the rows in a new transaction, as the same person. A write
        that cannot be seen afterwards did not happen."""
        if tool == "db_query":
            return True
        if not result.ok:
            return False
        table = self.catalog.table(args.get("table"))

        conn = await self._connect(creds)
        try:
            async with conn.transaction():
                await self._identify(conn, creds)
                current = await self._by_keys(conn, table, result.rows)
        except asyncpg.PostgresError:
            return False
        finally:
            await conn.close()

        if tool == "db_delete":
            return not current
        return _matches(current, result.rows, table.primary_key)

    async def undo(self, creds: Creds, action: Any) -> UndoResult:
        """Compensating writes, as the acts-for person, so undo obeys the same
        rules the original did. Only if nothing has moved since."""
        tool = getattr(action, "tool", None)
        if tool == "db_query":
            return UndoResult.impossible("A read changed nothing.")
        if tool not in self.TOOLS:
            return UndoResult.impossible(f"`{tool}` is not something this undoes.")

        created = getattr(action, "created_at", None)
        if created is not None and datetime.now(timezone.utc) - created > UNDO_WINDOW:
            return UndoResult.impossible(
                "This is more than 24 hours old. Undo is only offered within a"
                " day, because after that too much may have been built on top."
            )

        args = getattr(action, "args", None) or {}
        stored = getattr(action, "dry_run", None) or {}
        before = stored.get("before") or []
        after = stored.get("after") or []
        try:
            table = self.catalog.table(args.get("table"))
        except Denied as exc:
            return UndoResult.impossible(exc.reason)
        if not table.primary_key:
            return UndoResult.impossible(
                f"`{table.name}` has no primary key, so its rows cannot be found again."
            )

        conn = await self._connect(creds)
        tx = conn.transaction()
        await tx.start()
        try:
            await self._identify(conn, creds)
            if tool == "db_delete":
                if await self._by_keys(conn, table, before):
                    await tx.rollback()
                    return UndoResult(ok=False, reason=_MOVED)
                await self._reinsert(conn, table, before)
            else:
                if not _matches(
                    await self._by_keys(conn, table, after), after, table.primary_key
                ):
                    await tx.rollback()
                    return UndoResult(ok=False, reason=_MOVED)
                if tool == "db_create":
                    await self._delete_keys(conn, table, after)
                else:
                    await self._restore(conn, table, before)
            await tx.commit()
        except asyncpg.PostgresError as exc:
            await tx.rollback()
            detail = getattr(exc, "message", None) or str(exc)
            return UndoResult(ok=False, reason=f"Putting it back failed. {detail}")
        except BaseException:
            await tx.rollback()
            raise
        finally:
            await conn.close()
        return UndoResult(ok=True)

    # -- validation ----------------------------------------------------------

    def _checked(self, tool: str, args: Any) -> Table:
        """Every name in `args`, resolved against the allowlist.

        Called before a connection is opened, so an unknown table is a refusal
        rather than a query. Nothing here touches the database.
        """
        if tool not in self.TOOLS:
            raise Denied("unknown_tool", f"`{tool}` is not a tool this app has.")
        if not isinstance(args, dict):
            raise Denied("bad_arguments", "Arguments must be a set of named values.")

        table = self.catalog.table(args.get("table"))
        throwaway = _Params()
        self._where(table, args.get("filters"), throwaway)
        if tool in ("db_create", "db_update"):
            self._assignments(table, args.get("values"), throwaway)
        if tool == "db_query":
            self._selected(table, args.get("columns"))
            self._order(table, args.get("order_by"))
            self._read_limit(args.get("limit"))
        elif not table.primary_key:
            # Without a key there is no way to say which rows changed, so no
            # way to verify or undo. Refuse rather than write blind.
            raise Denied(
                "no_primary_key",
                f"`{table.name}` has no primary key, so changes to it cannot be"
                " checked afterwards or undone.",
            )
        return table

    def _where(self, table: Table, filters: Any, params: _Params) -> str:
        if filters is None:
            return ""
        if not isinstance(filters, (list, tuple)):
            raise Denied("bad_filter", "`filters` must be a list of conditions.")

        clauses = []
        for entry in filters:
            if not isinstance(entry, dict):
                raise Denied(
                    "bad_filter",
                    "Each filter is `{column, op, value}`.",
                )
            column = self.catalog.column(table, entry.get("column"))
            op = entry.get("op", "=")
            value = entry.get("value")
            name = quote_ident(column.name)
            cast = self.catalog.cast(column)

            if op == "is_null":
                clauses.append(f"{name} IS {'NOT NULL' if value is False else 'NULL'}")
            elif op == "in":
                if not isinstance(value, (list, tuple)) or not value:
                    raise Denied(
                        "bad_filter",
                        f"`in` on `{column.name}` needs a non-empty list of values.",
                    )
                slot = params.add([_as_text(item) for item in value])
                clauses.append(f"{name} = ANY({slot}::text[]::{cast}[])")
            elif op in COMPARISONS:
                slot = params.add(_as_text(value))
                clauses.append(f"{name} {COMPARISONS[op]} {slot}::text::{cast}")
            else:
                raise Denied(
                    "unknown_operator",
                    f"`{op}` is not a comparison you can use. Use one of:"
                    f" {', '.join(OPS)}.",
                )
        return (" WHERE " + " AND ".join(clauses)) if clauses else ""

    def _assignments(
        self, table: Table, values: Any, params: _Params
    ) -> list[tuple[str, str]]:
        """`(quoted column, bound expression)` for every value being written."""
        if not isinstance(values, dict) or not values:
            raise Denied(
                "bad_values", "`values` must name at least one column to write."
            )
        pairs = []
        for name, value in values.items():
            column = self.catalog.column(table, name)
            slot = params.add(_as_text(value))
            pairs.append(
                (quote_ident(column.name), f"{slot}::text::{self.catalog.cast(column)}")
            )
        return pairs

    def _selected(self, table: Table, columns: Any) -> str:
        """An explicit column list, never `*`: the shape of a result should not
        change because somebody ran a migration."""
        if columns is None:
            chosen = list(table.columns)
        elif isinstance(columns, (list, tuple)) and columns:
            chosen = [self.catalog.column(table, name).name for name in columns]
        else:
            raise Denied("bad_columns", "`columns` must be a list of column names.")
        return ", ".join(quote_ident(name) for name in chosen)

    def _order(self, table: Table, order_by: Any) -> str:
        if order_by is None:
            return ""
        if not isinstance(order_by, (list, tuple)):
            raise Denied("bad_order_by", "`order_by` must be a list.")
        parts = []
        for entry in order_by:
            if isinstance(entry, str):
                column, direction = self.catalog.column(table, entry), "ASC"
            elif isinstance(entry, dict):
                column = self.catalog.column(table, entry.get("column"))
                raw = str(entry.get("direction", "asc")).lower()
                if raw not in ("asc", "desc"):
                    raise Denied(
                        "unknown_direction",
                        f"`{raw}` is not a direction. Use `asc` or `desc`.",
                    )
                direction = raw.upper()
            else:
                raise Denied("bad_order_by", "Each entry is a column name.")
            parts.append(f"{quote_ident(column.name)} {direction}")
        return (" ORDER BY " + ", ".join(parts)) if parts else ""

    def _read_limit(self, limit: Any) -> int:
        if limit is None:
            return self.limits.max_rows_read
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise Denied("bad_limit", "`limit` must be a whole number above zero.")
        return min(limit, self.limits.max_rows_read)

    # -- statements ----------------------------------------------------------

    async def _connect(self, creds: Creds) -> asyncpg.Connection:
        try:
            return await asyncpg.connect(creds.dsn)
        except (OSError, asyncpg.PostgresError) as exc:
            raise Denied(
                "database_unreachable",
                "The app's database did not answer, so nothing was done.",
            ) from exc

    async def _identify(self, conn: asyncpg.Connection, creds: Creds) -> None:
        """Section 14 step 2. Bind parameters, transaction-local, both settings.

        With no identity the helper functions return NULL and every policy
        matches nothing, which is the fail-closed behaviour the whole product
        rests on. So this is never skipped and never string-formatted.
        """
        await conn.execute(
            "SELECT set_config('app.user_id', $1, true),"
            "       set_config('app.role', $2, true)",
            creds.user_id,
            creds.role,
        )

    async def _read(self, creds: Creds, args: dict[str, Any], table: Table) -> Result:
        params = _Params()
        columns = self._selected(table, args.get("columns"))
        where = self._where(table, args.get("filters"), params)
        order = self._order(table, args.get("order_by"))
        limit = params.add(self._read_limit(args.get("limit")))
        sql = (
            f"SELECT {columns} FROM {self.catalog.qualified(table)}"
            f"{where}{order} LIMIT {limit}"
        )

        conn = await self._connect(creds)
        try:
            async with conn.transaction():
                await self._identify(conn, creds)
                rows = await conn.fetch(sql, *params.values)
        except asyncpg.PostgresError as exc:
            raise _refused(exc) from exc
        finally:
            await conn.close()
        return Result(
            ok=True,
            affected=len(rows),
            rows=[_jsonable(dict(row)) for row in rows],
        )

    async def _change(
        self, conn: asyncpg.Connection, tool: str, args: dict[str, Any], table: Table
    ) -> DryRun:
        """The transaction body both `dry_run` and `execute` run.

        The caller decides whether it ends in COMMIT or ROLLBACK, and that is
        the *only* difference between checking and doing. Anything else would
        let the two drift apart without anyone noticing.
        """
        columns = ", ".join(quote_ident(name) for name in table.columns)
        qualified = self.catalog.qualified(table)

        if tool == "db_create":
            params = _Params()
            pairs = self._assignments(table, args.get("values"), params)
            sql = (
                f"INSERT INTO {qualified} ({', '.join(name for name, _ in pairs)})"
                f" VALUES ({', '.join(slot for _, slot in pairs)})"
                f" RETURNING {columns}"
            )
            after = [_jsonable(dict(row)) for row in await conn.fetch(sql, *params.values)]
            # A new row's key does not exist until the row does, and a dry run
            # throws its row away. Hashing the returned key would void every
            # approval the moment it was granted on any table with a generated
            # id, so what is approved here is what was asked for.
            return _result_of(
                tool,
                table,
                [],
                after,
                hashed=[{"values": _jsonable(args.get("values"))}],
            )

        before = await self._lock(conn, table, args, columns)
        if len(before) > self.limits.max_rows_changed:
            raise Denied(
                "limit_exceeded",
                f"This would change more than {self.limits.max_rows_changed} row(s),"
                " which is as many as this agent may change at once.",
            )
        if not before:
            # Rows the person cannot see are not "hidden", they are absent, so
            # aiming at one of them is not an error. It is nothing happening.
            return DryRun(
                affected=0,
                diff_hash=_hash(tool, table.name, []),
                note=NOTHING_MATCHED,
            )

        params = _Params()
        if tool == "db_update":
            pairs = self._assignments(table, args.get("values"), params)
            sets = ", ".join(f"{name} = {slot}" for name, slot in pairs)
            keys = self._key_clause(table, before, params)
            sql = f"UPDATE {qualified} SET {sets} WHERE {keys} RETURNING {columns}"
        else:
            keys = self._key_clause(table, before, params)
            sql = f"DELETE FROM {qualified} WHERE {keys} RETURNING {columns}"

        returned = [_jsonable(dict(row)) for row in await conn.fetch(sql, *params.values)]
        if tool == "db_update":
            return _result_of(tool, table, before, returned)
        # A delete has no after-images. What came back is what is gone.
        return _result_of(tool, table, returned, [])

    async def _lock(
        self, conn: asyncpg.Connection, table: Table, args: dict[str, Any], columns: str
    ) -> list[dict[str, Any]]:
        """The before-images, held for the rest of the transaction.

        One row over the limit is fetched on purpose: enough to know the limit
        was broken, not enough to lock a table because somebody asked to.
        """
        params = _Params()
        where = self._where(table, args.get("filters"), params)
        order = ", ".join(quote_ident(name) for name in table.primary_key)
        limit = params.add(self.limits.max_rows_changed + 1)
        sql = (
            f"SELECT {columns} FROM {self.catalog.qualified(table)}{where}"
            f" ORDER BY {order} LIMIT {limit} FOR UPDATE"
        )
        return [_jsonable(dict(row)) for row in await conn.fetch(sql, *params.values)]

    def _key_clause(
        self, table: Table, rows: Sequence[Mapping[str, Any]], params: _Params
    ) -> str:
        """Match exactly the rows we locked, by primary key.

        The filter found them; the key is what changes them. Re-using the
        filter would let a row that stopped matching mid-transaction slip out
        of the set the dry run showed.
        """
        if len(table.primary_key) == 1:
            key = table.primary_key[0]
            column = self.catalog.column(table, key)
            slot = params.add([_as_text(row[key]) for row in rows])
            return (
                f"{quote_ident(key)} = ANY({slot}::text[]::{self.catalog.cast(column)}[])"
            )
        groups = []
        for row in rows:
            parts = []
            for key in table.primary_key:
                column = self.catalog.column(table, key)
                slot = params.add(_as_text(row[key]))
                parts.append(
                    f"{quote_ident(key)} = {slot}::text::{self.catalog.cast(column)}"
                )
            groups.append("(" + " AND ".join(parts) + ")")
        return "(" + " OR ".join(groups) + ")"

    async def _by_keys(
        self, conn: asyncpg.Connection, table: Table, rows: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        if not rows:
            return []
        params = _Params()
        columns = ", ".join(quote_ident(name) for name in table.columns)
        keys = self._key_clause(table, rows, params)
        sql = f"SELECT {columns} FROM {self.catalog.qualified(table)} WHERE {keys}"
        return [_jsonable(dict(row)) for row in await conn.fetch(sql, *params.values)]

    async def _delete_keys(
        self, conn: asyncpg.Connection, table: Table, rows: Sequence[Mapping[str, Any]]
    ) -> None:
        params = _Params()
        keys = self._key_clause(table, rows, params)
        await conn.execute(
            f"DELETE FROM {self.catalog.qualified(table)} WHERE {keys}", *params.values
        )

    async def _restore(
        self, conn: asyncpg.Connection, table: Table, rows: Sequence[Mapping[str, Any]]
    ) -> None:
        for row in rows:
            params = _Params()
            pairs = self._assignments(
                table,
                {k: v for k, v in row.items() if k not in table.primary_key},
                params,
            )
            sets = ", ".join(f"{name} = {slot}" for name, slot in pairs)
            keys = self._key_clause(table, [row], params)
            await conn.execute(
                f"UPDATE {self.catalog.qualified(table)} SET {sets} WHERE {keys}",
                *params.values,
            )

    async def _reinsert(
        self, conn: asyncpg.Connection, table: Table, rows: Sequence[Mapping[str, Any]]
    ) -> None:
        for row in rows:
            params = _Params()
            pairs = self._assignments(table, dict(row), params)
            await conn.execute(
                f"INSERT INTO {self.catalog.qualified(table)}"
                f" ({', '.join(name for name, _ in pairs)})"
                f" VALUES ({', '.join(slot for _, slot in pairs)})",
                *params.values,
            )


_MOVED = (
    "Can't undo safely: the data has changed since, so putting this back would"
    " overwrite somebody else's work."
)


# ---------------------------------------------------------------------------
# Diffs
# ---------------------------------------------------------------------------


def _key_of(table: Table, row: Mapping[str, Any]) -> dict[str, Any]:
    return {name: row.get(name) for name in table.primary_key}


def _result_of(
    tool: str,
    table: Table,
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    *,
    hashed: Sequence[Mapping[str, Any]] | None = None,
) -> DryRun:
    if tool == "db_create":
        entries = [{"key": _key_of(table, row), "before": None, "after": dict(row)}
                   for row in after]
    elif tool == "db_delete":
        entries = [{"key": _key_of(table, row), "before": dict(row), "after": None}
                   for row in before]
    else:
        by_key = {canonical(_key_of(table, row)): dict(row) for row in after}
        entries = [
            {
                "key": _key_of(table, row),
                "before": dict(row),
                "after": by_key.get(canonical(_key_of(table, row))),
            }
            for row in before
        ]
    entries.sort(key=lambda entry: canonical(entry["key"]))
    affected = len(before) if tool == "db_delete" else len(after)
    return DryRun(
        affected=affected,
        diff=entries,
        diff_hash=_hash(tool, table.name, entries if hashed is None else hashed),
        before=[dict(row) for row in before],
        after=[dict(row) for row in after],
    )


def _hash(tool: str, table: str, entries: Sequence[Mapping[str, Any]]) -> str:
    """What an approval is bound to: the tool, the table and the exact rows.

    Sorted by key, so two runs that touch the same rows agree however the
    database happened to return them.
    """
    body = canonical({"tool": tool, "table": table, "diff": list(entries)})
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _matches(
    current: Iterable[Mapping[str, Any]],
    expected: Iterable[Mapping[str, Any]],
    primary_key: Sequence[str],
) -> bool:
    def index(rows):
        return {canonical([row.get(name) for name in primary_key]): row for row in rows}

    have, want = index(current), index(expected)
    if set(have) != set(want):
        return False
    return all(
        all(have[key].get(name) == value for name, value in row.items())
        for key, row in want.items()
    )


# ---------------------------------------------------------------------------
# What the agent is told a tool is
# ---------------------------------------------------------------------------

_SEES_ONLY = (
    " You only ever see and change rows belonging to the person you work for;"
    " everything else is not there as far as this tool is concerned."
)

_DESCRIPTIONS = {
    "db_query": "Read rows from one table." + _SEES_ONLY,
    "db_create": "Add one row to a table." + _SEES_ONLY,
    "db_update": "Change the rows of a table that match the filters." + _SEES_ONLY,
    "db_delete": "Remove the rows of a table that match the filters." + _SEES_ONLY,
}

_FILTERS = {
    "type": "array",
    "description": "Conditions every returned row must meet.",
    "items": {
        "type": "object",
        "properties": {
            "column": {"type": "string"},
            "op": {"type": "string", "enum": list(OPS)},
            "value": {"description": "Ignored by `is_null`; a list for `in`."},
        },
        "required": ["column", "op"],
        "additionalProperties": False,
    },
}


def _input_schema(tool: str, tables: Sequence[str]) -> dict[str, Any]:
    table = {"type": "string", "enum": list(tables)}
    if tool == "db_query":
        return {
            "type": "object",
            "properties": {
                "table": table,
                "filters": _FILTERS,
                "columns": {"type": "array", "items": {"type": "string"}},
                "order_by": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "column": {"type": "string"},
                            "direction": {"type": "string", "enum": ["asc", "desc"]},
                        },
                        "required": ["column"],
                        "additionalProperties": False,
                    },
                },
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["table"],
            "additionalProperties": False,
        }
    if tool == "db_create":
        return {
            "type": "object",
            "properties": {"table": table, "values": {"type": "object"}},
            "required": ["table", "values"],
            "additionalProperties": False,
        }
    if tool == "db_update":
        return {
            "type": "object",
            "properties": {
                "table": table,
                "filters": _FILTERS,
                "values": {"type": "object"},
            },
            "required": ["table", "values"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {"table": table, "filters": _FILTERS},
        "required": ["table", "filters"],
        "additionalProperties": False,
    }

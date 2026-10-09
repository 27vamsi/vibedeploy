"""Reads an app's schema out of pg_catalog. Readme.md section 8 M3.

Runs in the build job against the scratch database, right after the app's own
migrations have been applied as the migrator and before the kernel touches
anything. The result is the only description of the app's shape that derivation
(section 11), seeding (section 12) and the attack (section 13) are allowed to
use, so it has to be complete and it has to be stable: the same SQL must produce
byte-identical output every run, or `schema_hash` is worthless and "the schema
changed, re-verify" can never be trusted.

Two things make it stable:

  - Ordering is explicit everywhere. Columns keep their ordinal position because
    the seeder cares; everything else is sorted by name.
  - Introspection runs with `search_path` set to the app schema, so Postgres
    renders defaults and types unqualified. Otherwise a scratch schema named
    `app_ab12` and the same schema named `app_cd34` would hash differently over
    nothing but `nextval('app_ab12.audit_log_id_seq'::regclass)`.

No catalog oids ever reach the output for the same reason.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import asyncpg

from kernel.render import quote_ident

GRAPH_VERSION = 1

# pg_policy.polcmd, which comes back from asyncpg as a one-byte "char".
_POLICY_COMMANDS = {
    "*": "ALL",
    "r": "SELECT",
    "a": "INSERT",
    "w": "UPDATE",
    "d": "DELETE",
}

_TABLES = """
SELECT c.oid, c.relname, c.relrowsecurity, c.relforcerowsecurity
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = $1 AND c.relkind IN ('r', 'p')
ORDER BY c.relname
"""

# Every relation's columns, not just the app schema's: a foreign key may point
# at a table we are not describing, and we still have to name its columns.
_COLUMNS = """
SELECT a.attrelid, a.attnum, a.attname,
       format_type(a.atttypid, a.atttypmod) AS type,
       NOT a.attnotnull AS nullable,
       pg_get_expr(d.adbin, d.adrelid) AS default_expr
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attnum > 0 AND NOT a.attisdropped
  AND a.attrelid = ANY($1::oid[])
ORDER BY a.attrelid, a.attnum
"""

_CONSTRAINTS = """
SELECT con.conrelid, con.conname, con.contype, con.conkey, con.confkey,
       con.confrelid, fc.relname AS referenced_table,
       fn.nspname AS referenced_schema,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_class fc ON fc.oid = con.confrelid
LEFT JOIN pg_namespace fn ON fn.oid = fc.relnamespace
WHERE n.nspname = $1 AND con.contype IN ('p', 'u', 'c', 'f')
ORDER BY c.relname, con.conname
"""

_ENUMS = """
SELECT t.typname, e.enumlabel
FROM pg_type t
JOIN pg_enum e ON e.enumtypid = t.oid
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = $1
ORDER BY t.typname, e.enumsortorder
"""

# A view without security_invoker runs as its owner and skips the caller's
# policies entirely, so the flag is the whole reason views are in the graph.
# Readme.md section 25.
_VIEWS = """
SELECT c.relname, c.relkind, c.reloptions
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = $1 AND c.relkind IN ('v', 'm')
ORDER BY c.relname
"""

_FUNCTIONS = """
SELECT p.proname,
       pg_get_function_identity_arguments(p.oid) AS arguments,
       p.prosecdef
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = $1 AND p.prokind IN ('f', 'p')
ORDER BY p.proname, 2
"""

_POLICIES = """
SELECT c.relname AS table_name, pol.polname, pol.polcmd, pol.polpermissive,
       ARRAY(
           SELECT r.rolname FROM pg_roles r
           WHERE r.oid = ANY (pol.polroles) ORDER BY r.rolname
       ) AS roles,
       pg_get_expr(pol.polqual, pol.polrelid) AS using_expr,
       pg_get_expr(pol.polwithcheck, pol.polrelid) AS check_expr
FROM pg_policy pol
JOIN pg_class c ON c.oid = pol.polrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = $1
ORDER BY c.relname, pol.polname
"""


def _char(value: Any) -> str:
    """Postgres' internal "char" type arrives as bytes."""
    return value.decode() if isinstance(value, (bytes, bytearray)) else value


async def introspect(conn: asyncpg.Connection, schema: str) -> dict[str, Any]:
    """Describe `schema` as a plain, JSON-serialisable, deterministic dict."""
    async with conn.transaction():
        await conn.execute(f"SET LOCAL search_path TO {quote_ident(schema)}")
        return await _introspect(conn, schema)


async def _introspect(conn: asyncpg.Connection, schema: str) -> dict[str, Any]:
    table_rows = await conn.fetch(_TABLES, schema)
    constraint_rows = await conn.fetch(_CONSTRAINTS, schema)

    # Resolve every relation a constraint can name, including the far side of a
    # foreign key that points outside the app schema.
    wanted = {row["oid"] for row in table_rows}
    wanted.update(
        row["confrelid"] for row in constraint_rows if row["confrelid"]
    )
    column_rows = await conn.fetch(_COLUMNS, list(wanted))

    names: dict[tuple[int, int], str] = {}
    columns: dict[int, list[dict[str, Any]]] = {}
    for row in column_rows:
        names[(row["attrelid"], row["attnum"])] = row["attname"]
        columns.setdefault(row["attrelid"], []).append(
            {
                "name": row["attname"],
                "type": row["type"],
                "nullable": row["nullable"],
                "default": row["default_expr"],
            }
        )

    tables: dict[str, Any] = {
        row["relname"]: {
            "columns": columns.get(row["oid"], []),
            "primary_key": [],
            "unique": [],
            "checks": [],
            "foreign_keys": [],
            "rls_enabled": row["relrowsecurity"],
            "rls_forced": row["relforcerowsecurity"],
        }
        for row in table_rows
    }
    by_oid = {row["oid"]: row["relname"] for row in table_rows}

    for row in constraint_rows:
        table = tables[by_oid[row["conrelid"]]]
        contype = _char(row["contype"])
        cols = [names[(row["conrelid"], n)] for n in (row["conkey"] or ())]

        if contype == "p":
            table["primary_key"] = cols
        elif contype == "u":
            table["unique"].append({"name": row["conname"], "columns": cols})
        elif contype == "c":
            table["checks"].append(
                {"name": row["conname"], "expression": row["definition"]}
            )
        elif contype == "f":
            referenced = row["referenced_table"]
            if row["referenced_schema"] != schema:
                referenced = f"{row['referenced_schema']}.{referenced}"
            table["foreign_keys"].append(
                {
                    "name": row["conname"],
                    "columns": cols,
                    "references": referenced,
                    "referenced_columns": [
                        names[(row["confrelid"], n)] for n in row["confkey"]
                    ],
                }
            )

    enums: dict[str, list[str]] = {}
    for row in await conn.fetch(_ENUMS, schema):
        enums.setdefault(row["typname"], []).append(row["enumlabel"])

    views = {}
    for row in await conn.fetch(_VIEWS, schema):
        options = row["reloptions"] or []
        views[row["relname"]] = {
            "security_invoker": any(
                option.lower() in ("security_invoker=true", "security_invoker=on")
                for option in options
            )
        }

    functions = [
        {
            "name": row["proname"],
            "arguments": row["arguments"],
            "security_definer": row["prosecdef"],
        }
        for row in await conn.fetch(_FUNCTIONS, schema)
    ]

    policies: dict[str, list[dict[str, Any]]] = {}
    for row in await conn.fetch(_POLICIES, schema):
        policies.setdefault(row["table_name"], []).append(
            {
                "name": row["polname"],
                "command": _POLICY_COMMANDS[_char(row["polcmd"])],
                "permissive": row["polpermissive"],
                "roles": list(row["roles"]),
                "using": row["using_expr"],
                "check": row["check_expr"],
            }
        )

    return {
        "version": GRAPH_VERSION,
        "tables": tables,
        "enums": enums,
        "views": views,
        "functions": functions,
        "policies": policies,
    }


def app_schema(graph: dict[str, Any]) -> dict[str, Any]:
    """The part of the graph the app's own migrations decide.

    Policies, the two RLS flags and the `vd_` helper functions are ours, not the
    app's. They have to be introspected, because the attack's structural checks
    read them back out of the live catalog, but they must not be fingerprinted:
    a schema hashes differently before and after we protect it, so a redeploy of
    unchanged code would look like changed code and block forever.

    What is left is exactly what contract 7.2's `schema_hash` is for: "this is
    the shape the rules were derived against".
    """
    return {
        "version": graph["version"],
        "tables": {
            name: {
                key: value
                for key, value in entry.items()
                if key not in ("rls_enabled", "rls_forced")
            }
            for name, entry in graph["tables"].items()
        },
        "enums": graph["enums"],
        "views": graph["views"],
        "functions": [
            function
            for function in graph["functions"]
            if not function["name"].startswith("vd_")
        ],
    }


def schema_hash(graph: dict[str, Any]) -> str:
    """Stable fingerprint of a graph, for contract 7.2's `schema_hash`."""
    canonical = json.dumps(
        app_schema(graph), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

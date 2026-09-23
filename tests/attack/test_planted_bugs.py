"""Eleven deliberate holes, each of which must be caught. Readme.md M5.

This is the file that gives `test_clean.py` its meaning. A suite that passes a
clean app is worthless unless it also refuses a broken one, so every bug the
Readme names gets its own test, plants exactly that one thing, and asserts both
that the deploy would be blocked *and* which check noticed.

Asserting the check name matters as much as the failure: a bug caught by the
wrong check is a coincidence, and the next version of it will slip through.

**These tests are permanently protected.** CLAUDE.md: never delete or skip a
planted-bug test to make CI green.
"""

from __future__ import annotations

import asyncpg

from tests.attack.conftest import build

FIXTURE = "crm_chain"


async def _broken(admin, plant):
    """Build a clean app, break one thing, attack it."""
    async with build(admin, FIXTURE) as app:
        await plant(app)
        report = await app.attack()
        return report, app


def _names(report) -> set[str]:
    return {c.name for c in report.failures}


async def _assert_caught(admin, plant, expected_check):
    report, _ = await _broken(admin, plant)
    assert report.failures, "the attack suite did not notice the planted bug"
    assert expected_check in _names(report), (
        f"expected {expected_check!r} to fire, got {sorted(_names(report))}:\n"
        + "\n".join(c.message for c in report.failures[:5])
    )


# --------------------------------------------------------------------------
# 1-4: the policy itself is wrong
# --------------------------------------------------------------------------


async def test_using_true(attack_admin):
    """The classic. `USING (true)` on SELECT hands out every row."""

    async def plant(app):
        await app.admin.execute(
            f"DROP POLICY vd_sel ON {app.qualified('invoices')};"
            f"CREATE POLICY vd_sel ON {app.qualified('invoices')} FOR SELECT"
            f" TO {app.roles.runtime}, {app.roles.agent} USING (true)"
        )

    report, _ = await _broken(attack_admin, plant)
    assert "literal_true" in _names(report)
    # And it has to be caught behaviourally too, or the structural check is
    # the only thing standing between a builder and a leak.
    leaks = [c for c in report.failures if c.name == "read_all"]
    assert leaks, "nobody actually noticed the extra rows"


async def test_missing_with_check_on_insert(attack_admin):
    """No WITH CHECK on INSERT: a row can be created in someone else's name."""

    async def plant(app):
        await app.admin.execute(
            f"DROP POLICY vd_ins ON {app.qualified('orders')};"
            f"CREATE POLICY vd_ins ON {app.qualified('orders')} FOR INSERT"
            f" TO {app.roles.runtime}, {app.roles.agent} WITH CHECK (true)"
        )

    report, _ = await _broken(attack_admin, plant)
    assert "insert_as_other" in _names(report)
    assert "literal_true" in _names(report)


async def test_missing_with_check_on_update(attack_admin):
    """UPDATE with USING and no WITH CHECK.

    This one is caught structurally and only structurally, and that is worth
    being explicit about. Postgres quietly reuses the USING expression as the
    WITH CHECK for UPDATE policies, so no probe can see a difference today. The
    rule survives on that fallback rather than on anything written down, and
    the moment a restrictive policy or a later Postgres changes the fallback,
    rows start being writable into other people's names with nothing in the
    diff to explain why. Section 3: every policy carries both.
    """

    async def plant(app):
        table = app.qualified("customers")
        await app.admin.execute(
            f"DROP POLICY vd_upd ON {table};"
            f"CREATE POLICY vd_upd ON {table} FOR UPDATE"
            f" TO {app.roles.runtime}, {app.roles.agent}"
            f' USING ("rep_id" = "{app.roles.schema}".vd_user_id())'
        )

    await _assert_caught(attack_admin, plant, "missing_check")


async def test_select_only_policy(attack_admin):
    """Three of the four commands left uncovered."""

    async def plant(app):
        table = app.qualified("invoices")
        await app.admin.execute(
            f"DROP POLICY vd_ins ON {table};"
            f"DROP POLICY vd_upd ON {table};"
            f"DROP POLICY vd_del ON {table};"
        )

    await _assert_caught(attack_admin, plant, "missing_policy")


# --------------------------------------------------------------------------
# 5-7: the table or the role is wrong
# --------------------------------------------------------------------------


async def test_force_off(attack_admin):
    """FORCE off. Harmless until the app is also the owner, and then fatal."""

    async def plant(app):
        await app.admin.execute(
            f"ALTER TABLE {app.qualified('invoices')} NO FORCE ROW LEVEL SECURITY"
        )

    await _assert_caught(attack_admin, plant, "force_disabled")


async def test_runtime_owns_a_table(attack_admin):
    """An owner is exempt from its own rules unless FORCE says otherwise."""

    async def plant(app):
        await app.admin.execute(
            f"ALTER TABLE {app.qualified('invoices')} OWNER TO {app.roles.runtime}"
        )

    await _assert_caught(attack_admin, plant, "role_is_owner")


async def test_runtime_has_bypassrls(attack_admin):
    """One role attribute switches off every policy in the database."""

    async def plant(app):
        await app.admin.execute(f"ALTER ROLE {app.roles.runtime} BYPASSRLS")

    report, _ = await _broken(attack_admin, plant)
    assert "role_bypassrls" in _names(report)
    # It is also the one bug where runtime and agent genuinely disagree, which
    # is exactly what the agent parity check exists to catch.
    assert "agent_differs" in _names(report)


# --------------------------------------------------------------------------
# 8-11: something outside the policies undoes them
# --------------------------------------------------------------------------


async def test_view_without_security_invoker(attack_admin):
    """A plain view runs as its author, so it leaks everything behind it."""

    async def plant(app):
        await app.as_migrator(
            "CREATE VIEW all_invoices AS SELECT * FROM invoices"
        )

    await _assert_caught(attack_admin, plant, "view_not_security_invoker")


async def test_security_definer_function(attack_admin):
    """A SECURITY DEFINER function is a hole with a friendly name."""

    async def plant(app):
        await app.as_migrator(
            "CREATE FUNCTION peek() RETURNS SETOF invoices"
            " LANGUAGE sql SECURITY DEFINER AS $$ SELECT * FROM invoices $$"
        )

    await _assert_caught(attack_admin, plant, "security_definer_function")


async def test_new_table_with_rls_off(attack_admin):
    """A table added after the model was written is nobody's decision."""

    async def plant(app):
        await app.as_migrator(
            "CREATE TABLE leaks (id bigserial PRIMARY KEY, secret text)"
        )

    await _assert_caught(attack_admin, plant, "unclassified_table")


async def test_truncate_granted(attack_admin):
    """TRUNCATE ignores row level security completely."""

    async def plant(app):
        await app.admin.execute(
            f"GRANT TRUNCATE ON {app.qualified('invoices')}"
            f" TO {app.roles.runtime}"
        )

    await _assert_caught(attack_admin, plant, "dangerous_grant")


# --------------------------------------------------------------------------
# The behavioural probes must be able to fail, one by one
# --------------------------------------------------------------------------


async def test_writes_to_other_peoples_rows_are_noticed(attack_admin):
    """Permissive SELECT, UPDATE and DELETE on a chained table.

    All three have to be loosened together to get a write through, because
    Postgres applies the SELECT policy to any UPDATE or DELETE whose WHERE
    clause reads a column. That is a happy accident, not a guarantee, which is
    why the write probes exist separately from `read_all`.
    """

    async def plant(app):
        table = app.qualified("invoices")
        both = f"{app.roles.runtime}, {app.roles.agent}"
        await app.admin.execute(
            f"DROP POLICY vd_sel ON {table};"
            f"DROP POLICY vd_upd ON {table};"
            f"DROP POLICY vd_del ON {table};"
            f"CREATE POLICY vd_sel ON {table} FOR SELECT TO {both}"
            f' USING ("amount" >= 0);'
            f"CREATE POLICY vd_upd ON {table} FOR UPDATE TO {both}"
            f' USING ("amount" >= 0) WITH CHECK ("amount" >= 0);'
            f"CREATE POLICY vd_del ON {table} FOR DELETE TO {both}"
            f' USING ("amount" >= 0);'
        )

    report, _ = await _broken(attack_admin, plant)
    assert "update_other" in _names(report)
    assert "delete_other" in _names(report)


async def test_the_empty_string_identity_gotcha_is_noticed(attack_admin):
    """Readme.md section 9.2: `current_setting` returns `''`, not NULL.

    A policy that forgets the `NULLIF` looks fine to every logged-in persona
    and hands the whole table to a request with nobody behind it. Only the
    no-identity probe sees it, so this proves that probe is not decorative.
    """

    async def plant(app):
        table = app.qualified("plans")
        await app.admin.execute(
            f"DROP POLICY vd_sel ON {table};"
            f"CREATE POLICY vd_sel ON {table} FOR SELECT"
            f" TO {app.roles.runtime}, {app.roles.agent}"
            " USING (current_setting('app.user_id', true) IS NOT NULL)"
        )

    report, _ = await _broken(attack_admin, plant)
    assert "no_identity" in _names(report)
    leaked = [c for c in report.failures if c.name == "no_identity"]
    assert any(c.table == "plans" for c in leaked)


# --------------------------------------------------------------------------
# The suite must not pass an app whose policies were never applied
# --------------------------------------------------------------------------


async def test_no_policies_at_all(attack_admin):
    """The worst case: the kernel never ran.

    There are no policies, no RLS and no grants. The one outcome that must be
    impossible is a clean report; a raised error is contract 7.4's `error`
    status and blocks the deploy just as firmly as `blocked` does.
    """
    async with build(attack_admin, FIXTURE, apply=False) as app:
        try:
            report = await app.attack()
        except asyncpg.PostgresError:
            return
        assert report.failures, "an unprotected app passed the attack"
        assert "rls_disabled" in _names(report)

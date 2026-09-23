"""A correctly built app survives the attack. Readme.md section 8 M5.

If this file is green but `test_planted_bugs.py` is too, the suite proves
nothing: a check that never fires is indistinguishable from a check that always
passes. The two files only mean something together.
"""

from __future__ import annotations

from buildjob.attack import expected_visible


async def test_a_clean_app_has_no_failures(clean):
    report = await clean.attack()
    assert report.failures == [], "\n".join(c.message for c in report.failures)
    assert report.passed == report.total


async def test_the_suite_actually_did_something(clean):
    report = await clean.attack()
    # A handful of checks would be suspicious; the suite covers every table,
    # every persona and every command.
    assert report.total > 50
    names = {c.name for c in report.checks}
    for required in (
        "read_all",
        "read_other",
        "update_other",
        "delete_other",
        "insert_as_other",
        "no_identity",
        "rls_disabled",
        "force_disabled",
        "literal_true",
        "agent_differs",
    ):
        assert required in names, required


async def test_the_contract_block_matches_section_7_4(clean):
    report = await clean.attack()
    block = report.to_contract()
    assert set(block) == {"total", "passed", "failures"}
    assert block["total"] == block["passed"]
    assert block["failures"] == []


async def test_every_member_actually_owns_rows(clean):
    """The answer key must not be empty, or the reads prove nothing.

    Section 12 step 3 asks for two rows per member per owned table. The
    principal table is the exception: a person is one row, by definition.
    """
    owned = {
        table: len(clean.plan.owned_by(table, "A"))
        for table, entry in clean.model["tables"].items()
        if entry["template"] in ("owner_column", "fk_chain")
        and table != clean.model["principal"]["table"]
    }
    assert owned, clean.model["tables"]
    assert all(count >= 2 for count in owned.values()), owned
    assert len(clean.plan.owned_by(clean.model["principal"]["table"], "A")) == 1


async def test_expectations_come_from_the_plan_not_the_policies(clean):
    """Section 3 rule 10, stated as a test.

    The expected row set for every table is computable with the database
    switched off. If this ever needs a connection, the suite has started
    deriving its answers from the thing it is testing.
    """
    for table in clean.model["tables"]:
        for persona in ("A", "B", "ADMIN", "NONE"):
            assert isinstance(
                expected_visible(clean.model, clean.plan, table, persona), set
            )


async def test_nobody_sees_anything_without_an_identity(clean):
    report = await clean.attack()
    none_reads = [
        c for c in report.checks if c.name == "read_all" and c.persona == "NONE"
    ]
    assert none_reads
    assert all(c.ok for c in none_reads)

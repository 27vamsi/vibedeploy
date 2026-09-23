"""The fixed words a failure is reported in. Readme.md section 13.

Every check that can fail names one key here, and the key is the only thing the
attack suite is allowed to invent. The sentence itself lives here so that:

  - a builder reads the same wording every time, in their own vocabulary
    ("Anyone logged in can read every row in `invoices`"), not a stack trace;
  - the checks stay honest. A check that has to name its failure up front
    cannot quietly downgrade itself into a warning.

No message says "maybe", "possibly" or "consider". If one of these is emitted,
the deploy is blocked.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # -- behavioural: what a persona could actually do -----------------------
    "read_all": (
        "`{table}`: {persona} can see rows that are not theirs. Expected"
        " {expected} row(s), got {got}."
    ),
    "read_other": (
        "`{table}`: {persona} can read another person's rows by asking for"
        " them directly."
    ),
    "update_other": "`{table}`: {persona} can change another person's rows.",
    "delete_other": "`{table}`: {persona} can delete another person's rows.",
    "insert_as_other": (
        "`{table}`: {persona} can create a row that belongs to someone else."
    ),
    "reassign": (
        "`{table}`: {persona} can hand one of their own rows to someone else."
    ),
    "no_identity": (
        "`{table}`: a request with no logged-in person can still {action} rows."
        " With no identity the answer must be nothing at all."
    ),
    "agent_differs": (
        "`{table}`: the agent role and the app role do not get the same answer"
        " for {check} as {persona}. An agent must have exactly the access of"
        " the person it works for."
    ),
    # -- structural: what the database is configured to allow ----------------
    "rls_disabled": (
        "`{table}` has row level security switched off, so every row in it is"
        " readable by the app."
    ),
    "force_disabled": (
        "`{table}` does not force row level security, so whoever owns the"
        " table is not filtered by its own rules."
    ),
    "missing_policy": (
        "`{table}` has no rule for {command}, so {command} on it is refused"
        " for everyone or allowed for everyone depending on the grant. Every"
        " table needs all four."
    ),
    "missing_check": (
        "`{table}`'s {command} rule says which rows may be touched but not what"
        " they may become, so a row can be written into someone else's name."
    ),
    "literal_true": (
        "`{table}`'s {command} rule is the literal `true`, which lets anyone"
        " logged in read or change every row in it."
    ),
    "policy_role_missing": (
        "`{table}`'s {command} rule does not apply to `{role}`, so that role"
        " is not filtered by it."
    ),
    "role_is_owner": (
        "`{role}` owns `{table}`. A table's owner can step around its own"
        " rules, so the app must never own its tables."
    ),
    "role_in_owner_group": (
        "`{role}` is a member of `{owner}`, so it can take on the owner's"
        " privileges and step around the rules."
    ),
    "role_bypassrls": (
        "`{role}` has BYPASSRLS, which switches off every rule on every table"
        " for it."
    ),
    "role_superuser": (
        "`{role}` is a superuser, so nothing on this database applies to it."
    ),
    "dangerous_grant": (
        "`{role}` has {privilege} on `{table}`. {privilege} is not filtered by"
        " row level security, so it must never be granted to the app."
    ),
    "view_not_security_invoker": (
        "The view `{view}` runs as the person who wrote it, not the person"
        " reading it, so it hands out every row behind it."
    ),
    "security_definer_function": (
        "`{function}` runs with its author's privileges, so calling it steps"
        " around the rules on the tables it touches."
    ),
    "unclassified_table": (
        "`{table}` is not in the access model, so nobody has decided who may"
        " see it."
    ),
    # -- the suite could not be trusted to have run at all -------------------
    "no_protected_tables": (
        "The attack suite found nothing to attack, so it proves nothing."
    ),
    "seed_missing_rows": (
        "`{table}` has no rows owned by {persona}, so nothing about it was"
        " proven."
    ),
}


class UnknownMessage(KeyError):
    """A check tried to report a failure in words nobody agreed on."""


def message(key: str, **fields: object) -> str:
    """Render one fixed message. An unknown key is a bug, not a fallback."""
    if key not in MESSAGES:
        raise UnknownMessage(
            f"{key!r} is not a known failure; add it to kernel/messages.py"
            " rather than writing a sentence at the call site"
        )
    return MESSAGES[key].format(**fields)

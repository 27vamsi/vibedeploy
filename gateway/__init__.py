"""The agent gateway. Readme.md sections 19 to 21.

An AI agent gets exactly the access of the person it works for, never more.
That is not enforced here: it is enforced by the same Postgres RLS policies
that hold the person in, because every transaction this package opens sets the
acts-for person's identity and connects as the app's unprivileged `agent` role.

What lives here is everything *around* that: which tools an agent is even shown,
whether a call needs a person to approve it, what it would do before it does it,
and a record of the whole thing that cannot be edited afterwards.
"""

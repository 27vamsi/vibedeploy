# The fixture repository

`base/` is an app nobody has protected yet. Each directory under `branches/` is
copied over it, so a branch differs from `clean` by exactly the files it lists
and nothing else. When a deploy is blocked there is then no argument about what
blocked it.

| Branch | What is planted | What must catch it |
|---|---|---|
| `clean` | nothing | must go live |
| `permissive_policy` | `USING (true)` on `todos` | `literal_true`, and the read probes |
| `definer_function` | a `SECURITY DEFINER` function | `security_definer_function` |
| `leaky_view` | a view without `security_invoker` | `view_not_security_invoker` |
| `truncate_grant` | `GRANT TRUNCATE ... TO PUBLIC` | `dangerous_grant` |
| `membership` | access decided by a junction table | derivation refuses |
| `unlinked` | a table with no path to `users` | a question, not a block |

`unlinked` is the odd one out and is the point of M7: it is not a bug. It is a
schema we cannot read the intent of, so the pipeline stops and asks instead of
guessing, and moves again only when somebody answers.

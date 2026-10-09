"""The worker. Readme.md sections 5 and 18.2.

One process, one job table, no broker. It is the only thing that holds the
customer database's admin credentials, and it is deliberately the only thing
that never runs a line of an app's own code: everything untrusted is a
subprocess (`buildjob.run`, `buildjob.migrate_task`) that it starts and waits
for.
"""

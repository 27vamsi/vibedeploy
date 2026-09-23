"""Hand-written expected access models. Readme.md section 8 M4, contract 7.2.

`CRM_CHAIN` is the worked example printed in the Readme's contract 7.2, with
four fields the contract shows but the example elides: `version`, `explanation`
(one line per table, section 11 step 5) and the two `confirmed_*` fields, which
derivation leaves `None` because confirmation has not happened yet.
"""

from __future__ import annotations

APP_ID = "app_fixture"
SCHEMA_HASH = "sha256:0000000000000000000000000000000000000000000000000000000000000000"


FLAT_TODO = {
    "version": 1,
    "app_id": APP_ID,
    "principal": {"table": "users", "key": "id", "key_type": "uuid"},
    "answers": {
        "audience": "me",
        "size": "small",
        "visibility": "own_data",
        "sensitive": False,
    },
    "tables": {
        "users": {"template": "owner_column", "column": "id"},
        "todos": {"template": "owner_column", "column": "owner_id"},
    },
    # Nothing is admin_only, nothing is read_only_shared and no row can end up
    # ownerless, so there is no reason for an admin bypass to exist at all.
    "admin_enabled": False,
    "explanation": [
        "Someone can see and change only their own row in `users`.",
        "Someone can see and change a row in `todos` when `todos.owner_id` is them.",
    ],
    "confirmed_by": None,
    "confirmed_at": None,
    "schema_hash": SCHEMA_HASH,
}


CRM_CHAIN = {
    "version": 1,
    "app_id": APP_ID,
    "principal": {"table": "users", "key": "id", "key_type": "uuid"},
    "answers": {
        "audience": "team",
        "size": "small",
        "visibility": "own_data",
        "sensitive": True,
        "unlinked.audit_log": "admin_only",
        "unlinked.plans": "read_only_shared",
    },
    "tables": {
        "users": {"template": "owner_column", "column": "id"},
        "customers": {"template": "owner_column", "column": "rep_id"},
        "orders": {
            "template": "fk_chain",
            "path": [
                {
                    "from": "orders",
                    "to": "customers",
                    "column": "customer_id",
                    "to_column": "id",
                },
                {
                    "from": "customers",
                    "to": "users",
                    "column": "rep_id",
                    "to_column": "id",
                },
            ],
        },
        "invoices": {
            "template": "fk_chain",
            "path": [
                {
                    "from": "invoices",
                    "to": "orders",
                    "column": "order_id",
                    "to_column": "id",
                },
                {
                    "from": "orders",
                    "to": "customers",
                    "column": "customer_id",
                    "to_column": "id",
                },
                {
                    "from": "customers",
                    "to": "users",
                    "column": "rep_id",
                    "to_column": "id",
                },
            ],
        },
        "plans": {"template": "read_only_shared"},
        "audit_log": {"template": "admin_only"},
    },
    "admin_enabled": True,
    "explanation": [
        "Someone can see and change only their own row in `users`.",
        "Only admins can see or change `audit_log`.",
        "Someone can see and change a row in `customers` when"
        " `customers.rep_id` is them.",
        "Someone can see and change a row in `invoices` when"
        " `invoices.order_id` leads to `orders.id`,"
        " `orders.customer_id` leads to `customers.id`,"
        " and `customers.rep_id` is them.",
        "Someone can see and change a row in `orders` when"
        " `orders.customer_id` leads to `customers.id`,"
        " and `customers.rep_id` is them.",
        "Everyone logged in can read `plans`. Only admins can change it.",
    ],
    "confirmed_by": None,
    "confirmed_at": None,
    "schema_hash": SCHEMA_HASH,
}

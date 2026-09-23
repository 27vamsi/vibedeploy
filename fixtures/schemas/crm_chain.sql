-- The demo shape. invoices -> orders -> customers -> users is a 3 hop chain,
-- which is what fk_chain exists for. Readme.md sections 9.3 and 26.
--
-- Also carries the two table shapes that are not owned by anyone: a reference
-- table everybody reads, and an audit table nobody should read.

CREATE TYPE invoice_status AS ENUM ('draft', 'sent', 'paid', 'void');

CREATE TABLE users (
    id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email text NOT NULL UNIQUE,
    name  text
);

CREATE TABLE customers (
    id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rep_id  uuid NOT NULL REFERENCES users(id),
    name    text NOT NULL
);

CREATE TABLE orders (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id uuid NOT NULL REFERENCES customers(id),
    placed_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE invoices (
    id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id uuid NOT NULL REFERENCES orders(id),
    amount   numeric(12,2) NOT NULL CHECK (amount >= 0),
    status   invoice_status NOT NULL DEFAULT 'draft'
);

-- Reference data: read by everyone, changed by admins. read_only_shared.
CREATE TABLE plans (
    code       text PRIMARY KEY,
    monthly    numeric(12,2) NOT NULL
);

-- admin_only.
CREATE TABLE audit_log (
    id      bigserial PRIMARY KEY,
    at      timestamptz NOT NULL DEFAULT now(),
    actor   text NOT NULL,
    detail  text
);

CREATE INDEX ON customers (rep_id);
CREATE INDEX ON orders (customer_id);
CREATE INDEX ON invoices (order_id);

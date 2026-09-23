-- The simplest shape: every table points straight at the principal table.
-- Readme.md section 11 step 4: one path of length 1 means owner_column.

CREATE TABLE users (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email      text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE todos (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id  uuid NOT NULL REFERENCES users(id),
    title     text NOT NULL,
    done      boolean NOT NULL DEFAULT false,
    priority  integer NOT NULL DEFAULT 0 CHECK (priority >= 0 AND priority <= 5)
);

CREATE INDEX ON todos (owner_id);

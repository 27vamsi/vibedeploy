-- The app's own migration, written by whoever vibe-coded it. Nothing in here
-- knows about vibedeploy: no policies, no RLS, no roles, no grants. That is the
-- starting point the whole pipeline is given.

CREATE TABLE users (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email      text NOT NULL UNIQUE,
    name       text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE todos (
    id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES users(id),
    title    text NOT NULL,
    done     boolean NOT NULL DEFAULT false
);

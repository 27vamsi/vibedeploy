-- Not a bug either: a table with no way back to `users`.
--
-- A price list everybody may read and a table of secrets only an admin may see
-- look identical from the schema. Nothing in the database says which this is,
-- so the pipeline stops and asks, and stays stopped until somebody answers.

CREATE TABLE plans (
    id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name   text NOT NULL,
    price  integer NOT NULL DEFAULT 0
);

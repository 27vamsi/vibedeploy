-- Not a bug in the code: a shape we refuse to guess at.
--
-- `team_members` is a junction whose primary key is exactly its foreign keys,
-- so who may see a team is decided by rows in it rather than by a column on the
-- team. Section 11 does not support that yet, and the honest answer is to say
-- so and stop, not to invent a rule that looks close enough.

CREATE TABLE teams (
    id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL
);

CREATE TABLE team_members (
    team_id uuid NOT NULL REFERENCES teams(id),
    user_id uuid NOT NULL REFERENCES users(id),
    PRIMARY KEY (team_id, user_id)
);

-- Planted bug: the app writes its own policy and gets it wrong.
--
-- RLS policies are OR-ed together, so one permissive `USING (true)` beside the
-- kernel's own policies hands every row to everybody. This is the bug the whole
-- product exists to catch, which is why it is the first branch.

ALTER TABLE todos ENABLE ROW LEVEL SECURITY;

CREATE POLICY everyone_can_see_everything ON todos
    FOR SELECT USING (true);

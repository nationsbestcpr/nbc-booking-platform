-- 0005: "View as" (impersonation) for admins.
-- Frozen copy: later edits to db/schema.sql / db/seed.sql never change this file.

-- Open the app as another account, to see exactly what they see. Read-only:
-- nothing can be changed while viewing as someone else. Admins hold it through
-- their all-permissions role; no other role has it unless an admin ticks it.
INSERT INTO permissions (code, area, description, own_records_only) VALUES
  ('user.impersonate', 'Admin', 'View the app as another account (read-only), to see what they see', false);

-- 0006: the Integrations page is for admins only.
-- Frozen copy: later edits to db/schema.sql / db/seed.sql never change this file.

-- Seeing sync status and data problems (system.health_view) and fixing them
-- (system.issue_resolve) were also given to customer service, managers,
-- operations and viewers. Admins keep both through their all-permissions role;
-- an admin can still tick them for a role on Roles & permissions.
DELETE FROM role_permissions
 WHERE permission_code IN ('system.health_view', 'system.issue_resolve')
   AND role_code IN ('customer_service', 'manager', 'operations', 'viewer');

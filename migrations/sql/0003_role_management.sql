-- 0003: role management for the admin dashboard.
-- Frozen copy: later edits to db/schema.sql / db/seed.sql never change this file.

-- Who may change which permissions each role grants (the Roles & permissions tab).
INSERT INTO permissions (code, area, description, own_records_only) VALUES
  ('role.manage', 'Admin', 'Change which permissions each role grants', false);

-- Role grants made by a person (granted_by / revoked_by set) are checked here:
--   * the person needs 'user.manage' in all city areas;
--   * only someone holding an all-permissions role (admin) can grant or revoke one;
--   * 'user' and 'student' are granted automatically, never by a person.
-- Grants with no person (sign-up, a paid seat, the first-admin set-up command)
-- are system grants and pass.
CREATE FUNCTION trg_user_roles_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_actor bigint;
  v_role  roles%ROWTYPE;
BEGIN
  IF TG_OP = 'INSERT' THEN
    v_actor := NEW.granted_by_user_id;
  ELSIF OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL THEN
    v_actor := NEW.revoked_by_user_id;
  ELSE
    RETURN NEW;
  END IF;
  IF v_actor IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT * INTO v_role FROM roles WHERE code = NEW.role_code;
  IF v_role.is_self_service THEN
    RAISE EXCEPTION 'role % is granted automatically, not by a person', NEW.role_code
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NOT user_has_permission(v_actor, 'user.manage') THEN
    RAISE EXCEPTION 'user % lacks permission user.manage', v_actor
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF v_role.grants_all AND NOT EXISTS (
       SELECT 1 FROM user_roles ur
         JOIN roles r ON r.code = ur.role_code
         JOIN user_accounts a ON a.id = ur.user_account_id
        WHERE ur.user_account_id = v_actor AND ur.revoked_at IS NULL
          AND r.grants_all AND a.status = 'active') THEN
    RAISE EXCEPTION 'only an admin can grant or revoke role %', NEW.role_code
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER user_roles_guard BEFORE INSERT OR UPDATE OF revoked_at ON user_roles
  FOR EACH ROW EXECUTE FUNCTION trg_user_roles_guard();

-- Someone must always be able to manage the system: revoking an admin role or
-- switching off an admin's account is refused if no other active admin is left.
CREATE FUNCTION trg_keep_an_admin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME = 'user_roles' THEN
    IF NOT (SELECT grants_all FROM roles WHERE code = OLD.role_code) THEN
      RETURN NULL;
    END IF;
  ELSIF NOT EXISTS (SELECT 1 FROM user_roles ur JOIN roles r ON r.code = ur.role_code
                     WHERE ur.user_account_id = OLD.id AND ur.revoked_at IS NULL AND r.grants_all) THEN
    RETURN NULL;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM user_roles ur
                   JOIN roles r ON r.code = ur.role_code
                   JOIN user_accounts a ON a.id = ur.user_account_id
                  WHERE ur.revoked_at IS NULL AND r.grants_all AND a.status = 'active') THEN
    RAISE EXCEPTION 'at least one active admin must remain'
      USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER user_roles_keep_an_admin AFTER UPDATE OF revoked_at ON user_roles
  FOR EACH ROW WHEN (OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL)
  EXECUTE FUNCTION trg_keep_an_admin();
CREATE TRIGGER user_accounts_keep_an_admin AFTER UPDATE OF status ON user_accounts
  FOR EACH ROW WHEN (OLD.status = 'active' AND NEW.status <> 'active')
  EXECUTE FUNCTION trg_keep_an_admin();

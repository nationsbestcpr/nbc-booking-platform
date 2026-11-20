-- 0010: the booking engine's API (/api/v1) for the NBC Ops Hub and other tools,
-- and the Superadmin name for the role that holds every permission.
-- Frozen: later changes to db/schema.sql never change this file.

-- Who may create and switch off API keys. Only the Superadmin role (it holds
-- every permission) has it at first.
INSERT INTO permissions (code, area, description, own_records_only) VALUES
  ('api_key.manage', 'Admin', 'Create and switch off the keys other tools (the NBC Ops Hub) use to call the booking engine', false)
ON CONFLICT (code) DO NOTHING;

-- The role that holds every permission is the Superadmin. Its code stays 'admin'.
UPDATE roles SET name = 'Superadmin',
                 description = 'Everything: people and roles, API keys, and every desk action (through the API or the fallback pages).'
 WHERE code = 'admin';

-- One key per calling tool. Only a hash of the secret part is stored; the key
-- is shown once when it is made. A call also names the staff member it is for
-- (X-Acting-User): it runs with that person's roles, limited to the key's
-- permissions, so every rule about cities, refunds and extra seats still holds.
CREATE TABLE IF NOT EXISTS api_keys (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name                text NOT NULL CHECK (btrim(name) <> ''),
  prefix              text NOT NULL UNIQUE CHECK (prefix ~ '^[a-z0-9]{8}$'),
  secret_hash         bytea NOT NULL,
  created_by_user_id  bigint NOT NULL REFERENCES user_accounts (id),
  created_at          timestamptz NOT NULL DEFAULT now(),
  expires_at          timestamptz,
  last_used_at        timestamptz,
  revoked_at          timestamptz,
  revoked_by_user_id  bigint REFERENCES user_accounts (id),
  CHECK (revoked_at IS NULL OR revoked_by_user_id IS NOT NULL)
);
COMMENT ON TABLE api_keys IS '[OWNED] Keys other tools (the NBC Ops Hub) use to call /api/v1. Only a hash of the secret is kept.';

-- What each key may do: a subset of the permissions (and never more than the
-- staff member named on the call holds).
CREATE TABLE IF NOT EXISTS api_key_permissions (
  api_key_id       bigint NOT NULL REFERENCES api_keys (id) ON DELETE CASCADE,
  permission_code  text NOT NULL REFERENCES permissions (code),
  PRIMARY KEY (api_key_id, permission_code)
);
COMMENT ON TABLE api_key_permissions IS '[OWNED] The permissions an API key may use.';

-- Only someone with api_key.manage makes or switches off a key.
CREATE OR REPLACE FUNCTION trg_api_keys_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_actor bigint;
BEGIN
  IF TG_OP = 'INSERT' THEN
    v_actor := NEW.created_by_user_id;
  ELSIF OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL THEN
    v_actor := NEW.revoked_by_user_id;
  ELSE
    RETURN NEW;
  END IF;
  IF NOT user_has_permission(v_actor, 'api_key.manage') THEN
    RAISE EXCEPTION 'user % lacks permission api_key.manage', v_actor USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS api_keys_guard ON api_keys;
CREATE TRIGGER api_keys_guard BEFORE INSERT OR UPDATE OF revoked_at ON api_keys
  FOR EACH ROW EXECUTE FUNCTION trg_api_keys_guard();

-- ============================================================================
-- Match Systems / Match Education — admin panel schema
-- Target: PostgreSQL 13+ (works as-is on Supabase, Neon, RDS, local Postgres)
-- Idempotent: safe to run repeatedly. Creates structure only, inserts no rows.
-- ============================================================================

BEGIN;

-- --------------------------------------------------------------------------
-- Shared enums
-- --------------------------------------------------------------------------
DO $$ BEGIN
  CREATE TYPE sector_kind AS ENUM ('education', 'distribution', 'general');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE record_status AS ENUM ('active', 'suspended', 'draft');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE plan_kind AS ENUM ('basic', 'standard', 'premium');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;


DO $$ BEGIN
  CREATE TYPE scope_kind AS ENUM ('global', 'per_client');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- --------------------------------------------------------------------------
-- Auto-maintained updated_at
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- --------------------------------------------------------------------------
-- clients
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clients (
  id          bigserial PRIMARY KEY,
  name_ar     text        NOT NULL CHECK (length(btrim(name_ar)) BETWEEN 2 AND 80),
  name_en     text        NOT NULL CHECK (length(btrim(name_en)) BETWEEN 2 AND 80),
  sector      sector_kind NOT NULL,
  site        text        NOT NULL UNIQUE CHECK (site ~ '^[a-z0-9.-]+[.][a-z]{2,}$'),
  status      record_status NOT NULL DEFAULT 'draft',
  created_by  text        NOT NULL DEFAULT 'system@matchsystems.com',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS clients_sector_status_idx ON clients (sector, status);
CREATE INDEX IF NOT EXISTS clients_created_at_idx    ON clients (created_at DESC);

DROP TRIGGER IF EXISTS clients_touch ON clients;
CREATE TRIGGER clients_touch BEFORE UPDATE ON clients
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- --------------------------------------------------------------------------
-- feedback  (each testimonial is bound to a client and carries its sector)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feedback (
  id          bigserial PRIMARY KEY,
  client_id   bigint      NOT NULL REFERENCES clients (id) ON DELETE CASCADE,
  site        text        NOT NULL REFERENCES clients (site) ON UPDATE CASCADE ON DELETE CASCADE,
  name_ar     text        NOT NULL CHECK (length(btrim(name_ar)) BETWEEN 2 AND 60),
  name_en     text        NOT NULL CHECK (length(btrim(name_en)) BETWEEN 2 AND 60),
  sector      sector_kind NOT NULL,
  role_ar     text        NOT NULL CHECK (length(btrim(role_ar)) BETWEEN 2 AND 90),
  role_en     text        NOT NULL CHECK (length(btrim(role_en)) BETWEEN 2 AND 90),
  body_ar     text        NOT NULL CHECK (length(btrim(body_ar)) BETWEEN 10 AND 400),
  body_en     text        NOT NULL CHECK (length(btrim(body_en)) BETWEEN 10 AND 400),
  rating      smallint    NOT NULL CHECK (rating BETWEEN 1 AND 5),
  status      record_status NOT NULL DEFAULT 'draft',
  created_by  text        NOT NULL DEFAULT 'system@matchsystems.com',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS feedback_client_idx        ON feedback (client_id);
CREATE INDEX IF NOT EXISTS feedback_sector_status_idx ON feedback (sector, status);
CREATE INDEX IF NOT EXISTS feedback_rating_idx        ON feedback (rating DESC);

DROP TRIGGER IF EXISTS feedback_touch ON feedback;
CREATE TRIGGER feedback_touch BEFORE UPDATE ON feedback
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Keep feedback.sector honest: it must equal the parent client's sector.
CREATE OR REPLACE FUNCTION feedback_inherit_sector() RETURNS trigger AS $$
DECLARE parent clients%ROWTYPE;
BEGIN
  SELECT * INTO parent FROM clients WHERE id = NEW.client_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'feedback.client_id % does not exist', NEW.client_id;
  END IF;
  NEW.sector := parent.sector;
  NEW.site   := parent.site;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS feedback_sector_sync ON feedback;
CREATE TRIGGER feedback_sector_sync BEFORE INSERT OR UPDATE ON feedback
  FOR EACH ROW EXECUTE FUNCTION feedback_inherit_sector();
-- --------------------------------------------------------------------------
-- portals
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portals (
  id          bigserial PRIMARY KEY,
  code        text        NOT NULL UNIQUE CHECK (code ~ '^[a-z][a-z0-9_]{2,19}$'),
  name_ar     text        NOT NULL,
  name_en     text        NOT NULL,
  roles       smallint    NOT NULL DEFAULT 1 CHECK (roles BETWEEN 1 AND 40),
  user_count  integer     NOT NULL DEFAULT 0 CHECK (user_count >= 0),
  status      record_status NOT NULL DEFAULT 'active',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS portals_status_idx ON portals (status);

DROP TRIGGER IF EXISTS portals_touch ON portals;
CREATE TRIGGER portals_touch BEFORE UPDATE ON portals
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- --------------------------------------------------------------------------
-- users  (accounts allowed to sign in to the admin panel)
-- --------------------------------------------------------------------------
-- Core fields only. `password` stores a bcrypt hash, never plaintext, and is
-- excluded from every API response.
-- Any earlier shape of this table (per-portal accounts, role/is_active) is
-- dropped so the simplified one is created in its place.
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'users'
      AND column_name IN ('portal', 'name_en', 'name_ar', 'hashed_password', 'role', 'is_active')
  ) THEN
    DROP TABLE users CASCADE;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS users (
  id         bigserial   PRIMARY KEY,
  name       text        NOT NULL CHECK (length(btrim(name)) BETWEEN 2 AND 80),
  email      text        NOT NULL CHECK (email ~ '^[^ @]+@[^ @]+[.][^ @]{2,}$'),
  password   text        NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS users_email_key ON users (lower(email));

-- --------------------------------------------------------------------------
-- subscriptions
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscriptions (
  id            bigserial PRIMARY KEY,
  client_id     bigint      NOT NULL REFERENCES clients (id) ON DELETE CASCADE,
  plan          plan_kind   NOT NULL DEFAULT 'basic',
  academic_year text        NOT NULL CHECK (academic_year ~ '^[0-9]{4}/[0-9]{4}$'),
  seats         integer     NOT NULL CHECK (seats BETWEEN 1 AND 20000),
  ends_at       date        NOT NULL,
  status        record_status NOT NULL DEFAULT 'draft',
  created_by    text        NOT NULL DEFAULT 'system@matchsystems.com',
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (client_id, academic_year)
);

CREATE INDEX IF NOT EXISTS subscriptions_client_idx  ON subscriptions (client_id);
CREATE INDEX IF NOT EXISTS subscriptions_status_idx  ON subscriptions (status);
CREATE INDEX IF NOT EXISTS subscriptions_ends_at_idx ON subscriptions (ends_at);

DROP TRIGGER IF EXISTS subscriptions_touch ON subscriptions;
CREATE TRIGGER subscriptions_touch BEFORE UPDATE ON subscriptions
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- --------------------------------------------------------------------------
-- modules  (feature flags)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS modules (
  id            bigserial PRIMARY KEY,
  code          text        NOT NULL UNIQUE CHECK (code ~ '^[a-z][a-z0-9_.]{2,29}$'),
  name_ar       text        NOT NULL,
  name_en       text        NOT NULL,
  scope         scope_kind  NOT NULL DEFAULT 'per_client',
  client_count  integer     NOT NULL DEFAULT 0 CHECK (client_count >= 0),
  status        record_status NOT NULL DEFAULT 'draft',
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS modules_scope_status_idx ON modules (scope, status);

DROP TRIGGER IF EXISTS modules_touch ON modules;
CREATE TRIGGER modules_touch BEFORE UPDATE ON modules
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- --------------------------------------------------------------------------
-- logs  (append-only audit trail)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS logs (
  id       bigserial PRIMARY KEY,
  actor    text        NOT NULL,
  action   text        NOT NULL CHECK (action IN ('create', 'update', 'delete', 'seed', 'login')),
  entity   text        NOT NULL,
  target   text,
  level    record_status NOT NULL DEFAULT 'active',
  meta     jsonb       NOT NULL DEFAULT '{}'::jsonb,
  at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS logs_at_idx     ON logs (at DESC);
CREATE INDEX IF NOT EXISTS logs_entity_idx ON logs (entity, at DESC);
CREATE INDEX IF NOT EXISTS logs_actor_idx  ON logs (actor);

-- --------------------------------------------------------------------------
-- --------------------------------------------------------------------------
-- stats  (homepage counters, editable from the admin panel)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stats (
  id          bigserial PRIMARY KEY,
  key         text        NOT NULL UNIQUE CHECK (key ~ '^[a-z][a-z0-9_.]{2,39}$'),
  label_ar    text        NOT NULL CHECK (length(btrim(label_ar)) BETWEEN 2 AND 60),
  label_en    text,
  value       numeric     NOT NULL DEFAULT 0 CHECK (value >= 0),
  suffix      text        NOT NULL DEFAULT '',
  icon        text        NOT NULL DEFAULT 'ph-duotone ph-chart-line-up',
  sort_order  smallint    NOT NULL DEFAULT 0,
  status      record_status NOT NULL DEFAULT 'active',
  created_by  text        NOT NULL DEFAULT 'system@matchsystems.com',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS stats_status_order_idx ON stats (status, sort_order);

DROP TRIGGER IF EXISTS stats_touch ON stats;
CREATE TRIGGER stats_touch BEFORE UPDATE ON stats
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Forward migrations
-- Applied to databases created before the portfolio/testimonial forms were
-- simplified. Every statement is idempotent, so this runs on every boot.
-- --------------------------------------------------------------------------

-- clients: logo replaces the website field; only the Arabic name is required.
ALTER TABLE clients ADD COLUMN IF NOT EXISTS logo_url text;
ALTER TABLE clients ALTER COLUMN name_en DROP NOT NULL;
ALTER TABLE clients ALTER COLUMN site    DROP NOT NULL;

-- The portfolio form now identifies a company by its Arabic name.
CREATE UNIQUE INDEX IF NOT EXISTS clients_name_ar_key ON clients (name_ar);

-- feedback: client_id is the only link to the company; the duplicated site
-- column and the English variants are no longer collected by the form.
ALTER TABLE feedback DROP COLUMN IF EXISTS site;
ALTER TABLE feedback ALTER COLUMN name_en DROP NOT NULL;
ALTER TABLE feedback ALTER COLUMN role_en DROP NOT NULL;
ALTER TABLE feedback ALTER COLUMN body_en DROP NOT NULL;
ALTER TABLE feedback ALTER COLUMN rating  SET DEFAULT 5;

-- The trigger no longer mirrors clients.site, only the sector.
CREATE OR REPLACE FUNCTION feedback_inherit_sector() RETURNS trigger AS $$
DECLARE parent clients%ROWTYPE;
BEGIN
  SELECT * INTO parent FROM clients WHERE id = NEW.client_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'feedback.client_id % does not exist', NEW.client_id;
  END IF;
  NEW.sector := parent.sector;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- stats: the admin form now collects only label, value and visibility.
-- value holds a display string ("150+", "1,200", "2K+"), so it is text.
ALTER TABLE stats ADD COLUMN IF NOT EXISTS is_visible boolean NOT NULL DEFAULT true;
ALTER TABLE stats DROP CONSTRAINT IF EXISTS stats_value_check;
ALTER TABLE stats ALTER COLUMN value TYPE text USING value::text;
ALTER TABLE stats ALTER COLUMN value SET DEFAULT '0';

-- key is no longer asked for, so it generates itself.
CREATE SEQUENCE IF NOT EXISTS stats_key_seq;
ALTER TABLE stats ALTER COLUMN key SET DEFAULT ('metric_' || nextval('stats_key_seq'));

-- Existing rows: carry the old status across to the new visibility flag.
UPDATE stats SET is_visible = (status = 'active') WHERE is_visible IS DISTINCT FROM (status = 'active');

CREATE INDEX IF NOT EXISTS stats_visible_order_idx ON stats (is_visible, sort_order);

-- Schools are gone: a school is just a client, so users and subscriptions now
-- hang off clients.id. Safe to re-run; no-ops once applied.
ALTER TABLE users         ADD COLUMN IF NOT EXISTS client_id bigint REFERENCES clients (id) ON DELETE CASCADE;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS client_id bigint REFERENCES clients (id) ON DELETE CASCADE;
ALTER TABLE users         DROP COLUMN IF EXISTS school_id;
ALTER TABLE subscriptions DROP COLUMN IF EXISTS school_id;

-- Tighten to NOT NULL only when no orphan rows would block it.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM users WHERE client_id IS NULL) THEN
    ALTER TABLE users ALTER COLUMN client_id SET NOT NULL;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM subscriptions WHERE client_id IS NULL) THEN
    ALTER TABLE subscriptions ALTER COLUMN client_id SET NOT NULL;
  END IF;
EXCEPTION WHEN others THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE modules RENAME COLUMN schools_count TO client_count;
EXCEPTION WHEN others THEN NULL; END $$;
ALTER TABLE modules ADD COLUMN IF NOT EXISTS client_count integer NOT NULL DEFAULT 0;

DO $$ BEGIN
  ALTER TYPE scope_kind RENAME VALUE 'per_school' TO 'per_client';
EXCEPTION WHEN others THEN NULL; END $$;

DROP TABLE IF EXISTS school_modules CASCADE;
DROP TABLE IF EXISTS schools CASCADE;

CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_client_year_key ON subscriptions (client_id, academic_year);

-- portal_kind was only used by the old users table.
DO $$ BEGIN
  DROP TYPE IF EXISTS portal_kind;
EXCEPTION WHEN others THEN NULL; END $$;

COMMIT;

-- Merge support.  The runtime schema is created by SQLAlchemy metadata and
-- startup backfills origin_uid; this file is the optional manual upgrade for
-- an existing PostgreSQL database.
ALTER TABLE archive_studies ADD COLUMN IF NOT EXISTS merge_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE archive_studies ADD COLUMN IF NOT EXISTS baseline_hash VARCHAR NOT NULL DEFAULT '';
ALTER TABLE climate_windows ADD COLUMN IF NOT EXISTS origin_uid VARCHAR;
ALTER TABLE climate_windows ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE climate_windows ADD COLUMN IF NOT EXISTS last_branch VARCHAR NOT NULL DEFAULT 'main';
ALTER TABLE climate_windows ADD COLUMN IF NOT EXISTS last_package_uid VARCHAR NOT NULL DEFAULT '';

-- origin_uid is uuid5(study code, window code) and is filled in by the
-- API/worker startup backfill (a plain SQL expression cannot compute it).
-- The unique index is created only after that backfill has finished:
--   CREATE UNIQUE INDEX climate_windows_study_uid_idx
--     ON climate_windows (study_id, origin_uid);

CREATE INDEX IF NOT EXISTS window_study_split_idx ON climate_windows (study_id, split);
CREATE INDEX IF NOT EXISTS study_baselines_study_revision_idx ON study_baselines (study_id, revision);
CREATE INDEX IF NOT EXISTS merge_packages_study_status_idx ON source_merge_packages (study_id, status);

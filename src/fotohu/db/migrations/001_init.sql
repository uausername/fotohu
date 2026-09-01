-- FotoHu initial schema.
-- A "person" is a family member; they may have several messenger accounts.

CREATE TABLE people (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL,
    role                TEXT    NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
    status              TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'blocked')),
    personal_folder     TEXT,            -- overrides the slug derived from name
    group_id            INTEGER REFERENCES groups(id) ON DELETE SET NULL,
    folder_mode_override TEXT   CHECK (folder_mode_override IN ('per_person','shared','per_group')),
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    folder      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE accounts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id         INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    platform          TEXT    NOT NULL CHECK (platform IN ('telegram', 'viber')),
    platform_user_id  TEXT    NOT NULL,
    username          TEXT,
    chat_id           TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (platform, platform_user_id)
);

CREATE TABLE invites (
    code        TEXT    PRIMARY KEY,
    created_by  INTEGER REFERENCES people(id) ON DELETE SET NULL,
    role        TEXT    NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
    group_id    INTEGER REFERENCES groups(id) ON DELETE SET NULL,
    expires_at  TEXT,
    max_uses    INTEGER NOT NULL DEFAULT 1,
    uses        INTEGER NOT NULL DEFAULT 0,
    revoked_at  TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE storage_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    backend         TEXT    NOT NULL,
    label           TEXT    NOT NULL,
    credentials_enc TEXT,
    root_folder     TEXT    NOT NULL DEFAULT 'FotoHu',
    extra_json      TEXT    NOT NULL DEFAULT '{}',
    is_default      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Google Drive addresses folders by id, not by path, so we memoise the mapping.
CREATE TABLE folder_cache (
    storage_account_id INTEGER NOT NULL REFERENCES storage_accounts(id) ON DELETE CASCADE,
    path               TEXT    NOT NULL,
    remote_id          TEXT    NOT NULL,
    PRIMARY KEY (storage_account_id, path)
);

CREATE TABLE uploads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id      INTEGER REFERENCES people(id) ON DELETE SET NULL,
    platform       TEXT    NOT NULL,
    chat_id        TEXT    NOT NULL,
    message_id     TEXT    NOT NULL,
    media_group_id TEXT,
    source_kind    TEXT    NOT NULL,   -- document | photo | video | file | picture
    lossless       INTEGER NOT NULL DEFAULT 1,
    remote_file_id TEXT,               -- messenger-side handle (file_id / media URL)
    file_name      TEXT    NOT NULL,
    size           INTEGER,
    sha256         TEXT,
    md5            TEXT,
    caption        TEXT,
    taken_at       TEXT,
    date_source    TEXT,               -- exif | message | now
    storage_account_id INTEGER REFERENCES storage_accounts(id) ON DELETE SET NULL,
    backend        TEXT,
    remote_path    TEXT,
    remote_id      TEXT,
    verified       INTEGER NOT NULL DEFAULT 0,
    state          TEXT    NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending','uploading','done','failed','skipped_dup','rejected')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    received_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    uploaded_at    TEXT,
    next_attempt_at TEXT,
    purge_after    TEXT,
    purged_at      TEXT,
    purge_error    TEXT,
    bot_message_id TEXT,               -- our own reply, purged alongside
    UNIQUE (platform, chat_id, message_id)
);

CREATE INDEX idx_uploads_state       ON uploads (state, next_attempt_at);
CREATE INDEX idx_uploads_purge       ON uploads (state, purged_at, purge_after);
CREATE INDEX idx_uploads_sha         ON uploads (sha256);
CREATE INDEX idx_uploads_person      ON uploads (person_id, received_at);

CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE oauth_states (
    state       TEXT PRIMARY KEY,
    backend     TEXT NOT NULL,
    person_id   INTEGER REFERENCES people(id) ON DELETE CASCADE,
    verifier    TEXT,
    payload     TEXT NOT NULL DEFAULT '{}',
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

-- The shared feed: a photo shown to the rest of the family while it is still news.
--
-- A mirror is a copy of the *view*, never of the archive. It holds no messenger
-- file handle and no remote path, nothing ever claims it from the upload queue,
-- and it is keyed so one upload can reach one chat exactly once — that is what
-- keeps showing a photo to five people from turning into five files in the cloud.
--
-- Each row is one bot message in one chat, remembered only so the purge worker
-- can take it away again on the same schedule as the original.

CREATE TABLE mirrors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id   INTEGER NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    person_id   INTEGER REFERENCES people(id) ON DELETE SET NULL,
    platform    TEXT    NOT NULL,
    chat_id     TEXT    NOT NULL,
    message_id  TEXT    NOT NULL,
    sent_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    purge_after TEXT,
    purged_at   TEXT,
    purge_error TEXT,
    UNIQUE (upload_id, platform, chat_id)
);

CREATE INDEX idx_mirrors_purge  ON mirrors (purged_at, purge_after);
CREATE INDEX idx_mirrors_upload ON mirrors (upload_id);

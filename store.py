"""The bot's database: one SQLite file (DATABASE_FILE, default DATA_DIR/smartbot.db) for everything it
remembers. Settings changed from Discord, strikes, uploaded images, and what it last sent to Discord for
each server's profile.

The file is created owner-only (SQLite gives its journal the same mode). It uses the default rollback
journal rather than WAL, because WAL needs shared memory that network filesystems like NFS don't provide.
Only one bot process should use a database at a time.
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
from typing import Any

log = logging.getLogger("smartbot.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    guild_id   INTEGER NOT NULL,
    key        TEXT    NOT NULL,
    value      TEXT    NOT NULL,          -- JSON
    updated_by INTEGER,
    updated_at REAL    NOT NULL,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS strikes (
    id         INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    points     INTEGER NOT NULL,
    reason     TEXT    NOT NULL,
    given_by   INTEGER,                   -- NULL: automod or anti-spam
    created_at REAL    NOT NULL,
    cleared_at REAL                       -- set by /strikes clear; kept for history
);
CREATE INDEX IF NOT EXISTS strikes_member ON strikes (guild_id, user_id, created_at);
CREATE TABLE IF NOT EXISTS images (
    sha256     TEXT PRIMARY KEY,
    mime       TEXT NOT NULL,
    data       BLOB NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_state (
    guild_id   INTEGER NOT NULL,
    field      TEXT    NOT NULL,
    value      TEXT,                      -- what was last sent (an image hash, or the bio)
    PRIMARY KEY (guild_id, field)
);
"""

IMAGE_PREFIX = "stored:"  # an image setting's value when the image lives in the database


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
            os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
            os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit; `with self.db` for transactions
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---------- settings changed from Discord ----------

    def settings(self) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for gid, key, value in self.db.execute("SELECT guild_id, key, value FROM settings"):
            out.setdefault(gid, {})[key] = json.loads(value)
        return out

    def set_setting(self, guild_id: int, key: str, value: Any, by: int | None = None) -> None:
        self.db.execute("INSERT INTO settings VALUES (?, ?, ?, ?, ?) ON CONFLICT (guild_id, key) DO UPDATE SET "
                        "value = excluded.value, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                        (guild_id, key, json.dumps(value), by, time.time()))

    def delete_setting(self, guild_id: int, key: str) -> bool:
        return self.db.execute("DELETE FROM settings WHERE guild_id = ? AND key = ?", (guild_id, key)).rowcount > 0

    # ---------- strikes ----------

    def active_strikes(self, guild_id: int, user_id: int, expiry_days: int) -> list[tuple[float, int, str]]:
        """(time, points, reason) for each strike that hasn't expired or been cleared, oldest first."""
        return self.db.execute(
            "SELECT created_at, points, reason FROM strikes WHERE guild_id = ? AND user_id = ? "
            "AND cleared_at IS NULL AND created_at >= ? ORDER BY created_at",
            (guild_id, user_id, time.time() - expiry_days * 86400)).fetchall()

    def add_strike(self, guild_id: int, user_id: int, points: int, reason: str, by: int | None = None,
                   at: float | None = None) -> None:
        self.db.execute("INSERT INTO strikes (guild_id, user_id, points, reason, given_by, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (guild_id, user_id, points, reason, by, at or time.time()))

    def clear_strikes(self, guild_id: int, user_id: int) -> int:
        return self.db.execute("UPDATE strikes SET cleared_at = ? WHERE guild_id = ? AND user_id = ? "
                               "AND cleared_at IS NULL", (time.time(), guild_id, user_id)).rowcount

    # ---------- images ----------

    def put_image(self, raw: bytes, mime: str) -> str:
        """Store an image; returns the value to save in an image setting."""
        digest = hashlib.sha256(raw).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO images VALUES (?, ?, ?, ?)", (digest, mime, raw, time.time()))
        return IMAGE_PREFIX + digest

    def get_image(self, ref: str) -> tuple[bytes, str] | None:
        row = self.db.execute("SELECT data, mime FROM images WHERE sha256 = ?",
                              (ref.removeprefix(IMAGE_PREFIX),)).fetchone()
        return (row[0], row[1]) if row else None

    def prune_images(self) -> int:
        """Drop images no setting uses any more."""
        used = {json.loads(v) for (v,) in self.db.execute("SELECT value FROM settings WHERE key IN ('avatar', 'banner')")}
        stored = [s for (s,) in self.db.execute("SELECT sha256 FROM images")]
        unused = [s for s in stored if IMAGE_PREFIX + s not in used]
        self.db.executemany("DELETE FROM images WHERE sha256 = ?", [(s,) for s in unused])
        return len(unused)

    # ---------- what was last sent to Discord ----------

    def profile_state(self, guild_id: int) -> dict[str, str | None]:
        return dict(self.db.execute("SELECT field, value FROM profile_state WHERE guild_id = ?", (guild_id,)))

    def save_profile_state(self, guild_id: int, state: dict[str, str | None]) -> None:
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM profile_state WHERE guild_id = ?", (guild_id,))
            self.db.executemany("INSERT INTO profile_state VALUES (?, ?, ?)",
                                [(guild_id, f, v) for f, v in state.items()])

    def import_profile_state(self, path: str) -> None:
        """Take over a .profile-state.json from older versions, so profiles aren't all resent once."""
        if not os.path.exists(path) or self.db.execute("SELECT 1 FROM profile_state LIMIT 1").fetchone():
            return
        try:
            with open(path) as f:
                data = json.load(f)
            for gid, state in data.items():
                self.save_profile_state(int(gid), state)
            log.info("Imported profile state from %s", path)
        except (OSError, ValueError, AttributeError) as e:
            log.warning("Couldn't import profile state from %s: %s", path, e)

#!/usr/bin/env python3
"""Persistent community data for the Jellyfin progress-sync image."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_COMMENT_LENGTH = 2000
MAX_CHAT_LENGTH = 500


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class CommunityStore:
    """SQLite store kept separate from Jellyfin's database."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS media_items (
                    item_id TEXT PRIMARY KEY,
                    item_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    year INTEGER,
                    series_name TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ratings (
                    item_id TEXT NOT NULL REFERENCES media_items(item_id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 10),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (item_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS comments (
                    id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL REFERENCES media_items(item_id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    deleted_at TEXT
                );

                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY,
                    reporter_id TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (reporter_id, target_type, target_id)
                );

                CREATE INDEX IF NOT EXISTS idx_comments_item_created
                    ON comments(item_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_chat_created
                    ON chat_messages(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ratings_item
                    ON ratings(item_id);
                """
            )

    def save_media_item(self, item: dict[str, Any]) -> None:
        item_id = str(item.get("Id", ""))
        if not item_id:
            raise ValueError("item id is required")
        name = str(item.get("Name") or item_id)
        year = item.get("ProductionYear")
        series_name = item.get("SeriesName")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO media_items(item_id, item_type, name, year, series_name, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    item_type=excluded.item_type,
                    name=excluded.name,
                    year=excluded.year,
                    series_name=excluded.series_name,
                    updated_at=excluded.updated_at
                """,
                (item_id, str(item.get("Type", "")), name, year, series_name, utc_now()),
            )

    @staticmethod
    def _media(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["item_id"],
            "type": row["item_type"],
            "name": row["name"],
            "year": row["year"],
            "seriesName": row["series_name"],
        }

    @staticmethod
    def _comment(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "itemId": row["item_id"],
            "userId": row["user_id"],
            "userName": row["user_name"],
            "body": row["body"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def item(self, item_id: str, user_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            media = conn.execute("SELECT * FROM media_items WHERE item_id = ?", (item_id,)).fetchone()
            if not media:
                return None
            stats = conn.execute(
                "SELECT AVG(rating) AS average, COUNT(*) AS count FROM ratings WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            mine = conn.execute(
                "SELECT rating FROM ratings WHERE item_id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
            comments = conn.execute(
                """
                SELECT * FROM comments
                WHERE item_id = ? AND deleted_at IS NULL
                ORDER BY created_at DESC LIMIT 100
                """,
                (item_id,),
            ).fetchall()
        return {
            "item": self._media(media),
            "rating": {
                "average": round(float(stats["average"] or 0), 2),
                "count": int(stats["count"]),
                "mine": int(mine["rating"]) if mine else None,
            },
            "comments": [self._comment(row) for row in comments],
        }

    def upsert_rating(self, item_id: str, user_id: str, user_name: str, rating: int | None) -> None:
        with self.connect() as conn:
            if rating is None:
                conn.execute("DELETE FROM ratings WHERE item_id = ? AND user_id = ?", (item_id, user_id))
            else:
                now = utc_now()
                conn.execute(
                    """
                    INSERT INTO ratings(item_id, user_id, user_name, rating, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(item_id, user_id) DO UPDATE SET
                        user_name=excluded.user_name,
                        rating=excluded.rating,
                        updated_at=excluded.updated_at
                    """,
                    (item_id, user_id, user_name, rating, now, now),
                )

    def add_comment(self, item_id: str, user_id: str, user_name: str, body: str) -> dict[str, Any]:
        body = body.strip()
        if not body:
            raise ValueError("comment cannot be empty")
        if len(body) > MAX_COMMENT_LENGTH:
            raise ValueError(f"comment cannot exceed {MAX_COMMENT_LENGTH} characters")
        now = utc_now()
        comment_id = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO comments(id, item_id, user_id, user_name, body, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (comment_id, item_id, user_id, user_name, body, now, now),
            )
            row = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
        return self._comment(row)

    def delete_comment(self, comment_id: str, user_id: str, is_admin: bool) -> bool:
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE comments SET deleted_at = ?
                WHERE id = ? AND deleted_at IS NULL AND (user_id = ? OR ? = 1)
                """,
                (utc_now(), comment_id, user_id, int(is_admin)),
            )
        return result.rowcount > 0

    def feed(self, sort: str, item_type: str | None, user_id: str, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 50))
        type_filter = "AND m.item_type = ?" if item_type in {"Movie", "Series", "Episode"} else ""
        params: list[Any] = [item_type] if type_filter else []
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.*,
                       AVG(r.rating) AS average_rating,
                       COUNT(r.rating) AS rating_count,
                       COUNT(DISTINCT CASE WHEN c.deleted_at IS NULL THEN c.id END) AS comment_count,
                       MAX(c.created_at) AS latest_comment
                FROM media_items m
                LEFT JOIN ratings r ON r.item_id = m.item_id
                LEFT JOIN comments c ON c.item_id = m.item_id
                WHERE 1=1 {type_filter}
                GROUP BY m.item_id
                """,
                params,
            ).fetchall()
            mine = {
                row["item_id"]: row["rating"]
                for row in conn.execute(
                    "SELECT item_id, rating FROM ratings WHERE user_id = ?", (user_id,)
                ).fetchall()
            }

        def score(row: sqlite3.Row) -> tuple[Any, ...]:
            average = float(row["average_rating"] or 0)
            count = int(row["rating_count"] or 0)
            if sort == "recent":
                return (row["latest_comment"] or row["updated_at"],)
            if sort == "discussed":
                return (int(row["comment_count"] or 0), row["latest_comment"] or "")
            # Bayesian smoothing keeps a single 10/10 from beating well-rated items.
            weighted = ((average * count) + (7.0 * 3.0)) / (count + 3.0)
            return (weighted, count, row["name"].lower())

        rows = sorted(rows, key=score, reverse=True)[:limit]
        return [
            {
                "item": self._media(row),
                "rating": {
                    "average": round(float(row["average_rating"] or 0), 2),
                    "count": int(row["rating_count"] or 0),
                    "mine": int(mine[row["item_id"]]) if row["item_id"] in mine else None,
                },
                "commentCount": int(row["comment_count"] or 0),
            }
            for row in rows
        ]

    @staticmethod
    def _chat(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "userId": row["user_id"],
            "userName": row["user_name"],
            "body": row["body"],
            "createdAt": row["created_at"],
        }

    def chat(self, since: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        condition = "AND created_at > ?" if since else ""
        params: list[Any] = [since] if since else []
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM chat_messages
                WHERE deleted_at IS NULL {condition}
                ORDER BY created_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._chat(row) for row in reversed(rows)]

    def add_chat(self, user_id: str, user_name: str, body: str) -> dict[str, Any]:
        body = body.strip()
        if not body:
            raise ValueError("message cannot be empty")
        if len(body) > MAX_CHAT_LENGTH:
            raise ValueError(f"message cannot exceed {MAX_CHAT_LENGTH} characters")
        message_id = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO chat_messages(id, user_id, user_name, body, created_at) VALUES (?, ?, ?, ?, ?)",
                (message_id, user_id, user_name, body, utc_now()),
            )
            row = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
        return self._chat(row)

    def delete_chat(self, message_id: str, user_id: str, is_admin: bool) -> bool:
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE chat_messages SET deleted_at = ?
                WHERE id = ? AND deleted_at IS NULL AND (user_id = ? OR ? = 1)
                """,
                (utc_now(), message_id, user_id, int(is_admin)),
            )
        return result.rowcount > 0

    def report(self, reporter_id: str, target_type: str, target_id: str, reason: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO reports(id, reporter_id, target_type, target_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (uuid.uuid4().hex, reporter_id, target_type, target_id, reason[:500], utc_now()),
            )

#!/usr/bin/env python3
import argparse
import base64
import json
import logging
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
LOG = logging.getLogger("jellyfin-progress-sync")
TICKS_PER_SECOND = 10_000_000


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class ConfigStore:
    path: Path
    data: dict[str, Any]
    lock: threading.RLock

    @classmethod
    def load(cls, path: Path) -> "ConfigStore":
        if not path.exists():
            sample = ROOT / "config.example.json"
            if sample.exists():
                shutil.copyfile(sample, path)
            else:
                path.write_text("{}", encoding="utf-8")
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("listen_host", "0.0.0.0")
        data.setdefault("listen_port", 8097)
        data.setdefault("ui_username", "admin")
        data.setdefault("ui_password", "change-me")
        data.setdefault("sync_interval_seconds", 60)
        data.setdefault("make_backups", True)
        data.setdefault("groups", [])
        return cls(path=path, data=data, lock=threading.RLock())

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def save(self, data: dict[str, Any]) -> None:
        with self.lock:
            self.data = data
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)


class JellyfinClient:
    def __init__(self, cfg: dict[str, Any]):
        self.base = str(cfg.get("jellyfin_url", "")).rstrip("/")
        self.api_key = str(cfg.get("api_key", ""))
        if not self.base or not self.api_key:
            raise ValueError("jellyfin_url and api_key must be set")

    def get(self, path: str, **params: Any) -> Any:
        res = requests.get(
            f"{self.base}{path}",
            params={k: v for k, v in params.items() if v is not None},
            headers={"X-Emby-Token": self.api_key},
            timeout=30,
        )
        res.raise_for_status()
        return res.json()

    def users(self) -> list[dict[str, Any]]:
        return self.get("/Users", isDisabled="false", isHidden="false")

    def series(self, user_id: str | None = None, search: str | None = None) -> list[dict[str, Any]]:
        data = self.get(
            "/Items",
            userId=user_id,
            includeItemTypes="Series",
            recursive="true",
            searchTerm=search,
            sortBy="SortName",
            enableImages="false",
            fields="ProviderIds",
            limit=200,
        )
        return data.get("Items", [])

    def episodes(self, series_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
        data = self.get(
            f"/Shows/{series_id}/Episodes",
            userId=user_id,
            fields="SortName",
            enableUserData="false",
            enableImages="false",
            sortBy="ParentIndexNumber,IndexNumber",
        )
        return data.get("Items", [])


def normalize_columns(cols: list[tuple[Any, ...]]) -> dict[str, str]:
    wanted = {
        "key": "key",
        "userid": "userId",
        "rating": "rating",
        "played": "played",
        "playcount": "playCount",
        "isfavorite": "isFavorite",
        "playbackpositionticks": "playbackPositionTicks",
        "lastplayeddate": "lastPlayedDate",
    }
    found: dict[str, str] = {}
    for col in cols:
        name = str(col[1])
        lower = name.lower()
        if lower in wanted:
            found[wanted[lower]] = name
    return found


class UserDataDb:
    def __init__(self, db_path: str, make_backups: bool):
        self.path = Path(db_path)
        self.make_backups = make_backups
        if not self.path.exists():
            raise FileNotFoundError(f"database not found: {self.path}")

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def validate(self) -> dict[str, str]:
        with self.connect() as conn:
            cols = normalize_columns(conn.execute("PRAGMA table_info(UserDatas)").fetchall())
        missing = {"key", "userId", "played", "playCount", "playbackPositionTicks", "lastPlayedDate"} - set(cols)
        if missing:
            raise RuntimeError(f"UserDatas table missing required columns: {', '.join(sorted(missing))}")
        return cols

    def backup(self) -> str | None:
        if not self.make_backups:
            return None
        backup_path = self.path.with_name(f"{self.path.name}.progress-sync.{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak")
        shutil.copy2(self.path, backup_path)
        return str(backup_path)

    def get_many(self, cols: dict[str, str], users: list[str], keys: list[str]) -> dict[tuple[str, str], sqlite3.Row]:
        if not users or not keys:
            return {}
        q_users = ",".join("?" for _ in users)
        q_keys = ",".join("?" for _ in keys)
        sql = f"""
            SELECT * FROM UserDatas
            WHERE {cols['userId']} IN ({q_users})
              AND {cols['key']} IN ({q_keys})
        """
        with self.connect() as conn:
            rows = conn.execute(sql, [*users, *keys]).fetchall()
        return {(str(row[cols["userId"]]), str(row[cols["key"]])): row for row in rows}

    def upsert_progress(self, cols: dict[str, str], user_id: str, item_key: str, source: dict[str, Any]) -> None:
        now = utc_now()
        played = int(bool(source["played"]))
        play_count = max(1, int(source.get("playCount") or 0)) if played else int(source.get("playCount") or 0)
        ticks = 0 if played else int(source.get("playbackPositionTicks") or 0)
        last_played = source.get("lastPlayedDate") or now
        row_id = uuid.uuid4().hex

        with self.connect() as conn:
            existing = conn.execute(
                f"SELECT rowid FROM UserDatas WHERE {cols['userId']} = ? AND {cols['key']} = ?",
                (user_id, item_key),
            ).fetchone()
            if existing:
                conn.execute(
                    f"""
                    UPDATE UserDatas
                    SET {cols['played']} = ?,
                        {cols['playCount']} = ?,
                        {cols['playbackPositionTicks']} = ?,
                        {cols['lastPlayedDate']} = ?
                    WHERE rowid = ?
                    """,
                    (played, play_count, ticks, last_played, existing["rowid"]),
                )
            else:
                names = [cols["key"], cols["userId"], cols["played"], cols["playCount"], cols["playbackPositionTicks"], cols["lastPlayedDate"]]
                values = [item_key, user_id, played, play_count, ticks, last_played]
                if "rating" in cols:
                    names.append(cols["rating"])
                    values.append(None)
                if "isFavorite" in cols:
                    names.append(cols["isFavorite"])
                    values.append(0)
                if "id" in [n.lower() for n in cols.values()]:
                    pass
                try:
                    conn.execute(
                        f"INSERT INTO UserDatas ({', '.join(names)}) VALUES ({', '.join('?' for _ in values)})",
                        values,
                    )
                except sqlite3.IntegrityError:
                    raise
            conn.commit()


def item_key(item: dict[str, Any]) -> str:
    return str(item.get("UserData", {}).get("Key") or item.get("Id"))


def progress_from_row(row: sqlite3.Row | None, cols: dict[str, str], episode_index: int) -> dict[str, Any]:
    if row is None:
        return {
            "episodeIndex": episode_index,
            "played": False,
            "playCount": 0,
            "playbackPositionTicks": 0,
            "lastPlayedDate": None,
            "score": (episode_index, 0, 0),
        }
    played = bool(row[cols["played"]])
    ticks = int(row[cols["playbackPositionTicks"]] or 0)
    score_ticks = 9_000_000_000_000_000 if played else ticks
    return {
        "episodeIndex": episode_index,
        "played": played,
        "playCount": int(row[cols["playCount"]] or 0),
        "playbackPositionTicks": ticks,
        "lastPlayedDate": row[cols["lastPlayedDate"]],
        "score": (episode_index, int(played), score_ticks),
    }


class SyncEngine:
    def __init__(self, store: ConfigStore):
        self.store = store
        self.last_result: dict[str, Any] = {"status": "never_run"}
        self.lock = threading.Lock()

    def run_once(self) -> dict[str, Any]:
        with self.lock:
            cfg = self.store.snapshot()
            client = JellyfinClient(cfg)
            db = UserDataDb(cfg["database_path"], bool(cfg.get("make_backups", True)))
            cols = db.validate()
            changed = []
            checked = 0
            backup_done = False
            backup_path = None

            for group in cfg.get("groups", []):
                if not group.get("enabled", True):
                    continue
                user_ids = list(dict.fromkeys(group.get("user_ids", [])))
                series_ids = list(dict.fromkeys(group.get("series_ids", [])))
                if len(user_ids) < 2 or not series_ids:
                    continue

                for series_id in series_ids:
                    episodes = client.episodes(series_id, user_ids[0])
                    keys = [item_key(ep) for ep in episodes]
                    rows = db.get_many(cols, user_ids, keys)

                    best = None
                    best_key = None
                    best_ep = None
                    for idx, key in enumerate(keys):
                        for user_id in user_ids:
                            progress = progress_from_row(rows.get((user_id, key)), cols, idx)
                            checked += 1
                            if progress["played"] or progress["playbackPositionTicks"] > 0:
                                if best is None or progress["score"] > best["score"]:
                                    best = progress
                                    best_key = key
                                    best_ep = episodes[idx]

                    if not best or best_key is None:
                        continue

                    for user_id in user_ids:
                        target = progress_from_row(rows.get((user_id, best_key)), cols, best["episodeIndex"])
                        if target["score"] >= best["score"]:
                            continue
                        if not backup_done:
                            backup_path = db.backup()
                            backup_done = True
                        db.upsert_progress(cols, user_id, best_key, best)
                        changed.append({
                            "group": group.get("name", "Unnamed group"),
                            "seriesId": series_id,
                            "episode": best_ep.get("Name") if best_ep else best_key,
                            "userId": user_id,
                            "played": best["played"],
                            "positionSeconds": round(best["playbackPositionTicks"] / TICKS_PER_SECOND, 1),
                        })

            self.last_result = {
                "status": "ok",
                "checked": checked,
                "changed": changed,
                "backup": backup_path,
                "finishedAt": utc_now(),
            }
            return self.last_result

    def loop(self) -> None:
        while True:
            cfg = self.store.snapshot()
            try:
                if not cfg.get("jellyfin_url") or not cfg.get("api_key") or not cfg.get("database_path"):
                    self.last_result = {"status": "waiting_for_config", "finishedAt": utc_now()}
                    time.sleep(max(15, int(cfg.get("sync_interval_seconds", 60))))
                    continue
                if not Path(str(cfg["database_path"])).exists():
                    self.last_result = {
                        "status": "waiting_for_database",
                        "database": cfg["database_path"],
                        "finishedAt": utc_now(),
                    }
                    time.sleep(max(15, int(cfg.get("sync_interval_seconds", 60))))
                    continue
                self.run_once()
            except Exception as exc:
                LOG.exception("sync failed")
                self.last_result = {"status": "error", "error": str(exc), "finishedAt": utc_now()}
            time.sleep(max(15, int(cfg.get("sync_interval_seconds", 60))))


def json_response(handler: SimpleHTTPRequestHandler, data: Any, status: int = 200) -> None:
    body = json.dumps(data, indent=2).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(SimpleHTTPRequestHandler):
    store: ConfigStore
    engine: SyncEngine

    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        req = parsed.path
        if req == "/":
            req = "/index.html"
        return str(STATIC / req.lstrip("/"))

    def authenticated(self) -> bool:
        cfg = self.store.snapshot()
        password = str(cfg.get("ui_password", ""))
        if not password:
            return True
        username = str(cfg.get("ui_username", "admin"))
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header.removeprefix("Basic ").strip()).decode()
        except Exception:
            return False
        return decoded == f"{username}:{password}"

    def require_auth(self) -> bool:
        if self.authenticated():
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Jellyfin Progress Sync"')
        self.end_headers()
        return False

    def do_GET(self) -> None:
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/config":
                cfg = self.store.snapshot()
                safe = dict(cfg)
                if safe.get("api_key"):
                    safe["api_key"] = "********"
                if safe.get("ui_password"):
                    safe["ui_password"] = "********"
                json_response(self, safe)
            elif parsed.path == "/api/status":
                json_response(self, self.engine.last_result)
            elif parsed.path == "/api/users":
                json_response(self, JellyfinClient(self.store.snapshot()).users())
            elif parsed.path == "/api/series":
                user_id = qs.get("userId", [None])[0]
                search = qs.get("search", [None])[0]
                json_response(self, JellyfinClient(self.store.snapshot()).series(user_id, search))
            elif parsed.path == "/api/schema":
                cfg = self.store.snapshot()
                cols = UserDataDb(cfg["database_path"], bool(cfg.get("make_backups", True))).validate()
                json_response(self, {"ok": True, "columns": cols})
            else:
                super().do_GET()
        except Exception as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode() or "{}")
        try:
            if parsed.path == "/api/config":
                current = self.store.snapshot()
                if payload.get("api_key") == "********":
                    payload["api_key"] = current.get("api_key", "")
                if payload.get("ui_password") == "********":
                    payload["ui_password"] = current.get("ui_password", "")
                self.store.save(payload)
                json_response(self, {"ok": True})
            elif parsed.path == "/api/sync":
                json_response(self, self.engine.run_once())
            else:
                json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = ConfigStore.load(Path(args.config))
    engine = SyncEngine(store)
    Handler.store = store
    Handler.engine = engine

    thread = threading.Thread(target=engine.loop, daemon=True)
    thread.start()

    cfg = store.snapshot()
    server = ThreadingHTTPServer((cfg["listen_host"], int(cfg["listen_port"])), Handler)
    LOG.info("listening on http://%s:%s", cfg["listen_host"], cfg["listen_port"])
    server.serve_forever()


if __name__ == "__main__":
    main()

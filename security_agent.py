#!/usr/bin/env python3
import argparse
import ipaddress
import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


LOG = logging.getLogger("jellyfin-security-agent")
IP_RE = re.compile(
    r"(?P<ip>(?:\d{1,3}\.){3}\d{1,3}|(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4})"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "database_path": "/config/data/jellyfin.db",
    "state_path": "/config/security-agent-state.json",
    "poll_interval_seconds": 15,
    "startup_lookback_minutes": 5,
    "alert_types": ["AuthenticationFailed", "UserLockedOut"],
    "discord": {
        "webhook_url": "",
        "username": "Jellyfin Security",
        "avatar_url": "",
    },
    "thresholds": {
        "failures": 5,
        "window_seconds": 600,
    },
    "ban": {
        "enabled": False,
        "action": "none",
        "duration_seconds": 86400,
        "allowlist": ["127.0.0.1", "::1"],
        "command": "",
        "cloudflare": {
            "api_token": "",
            "account_id": "",
            "list_id": "",
        },
    },
}


@dataclass
class Activity:
    id: int
    date: datetime
    type: str
    name: str
    short_overview: str
    overview: str
    user_id: str
    item_id: str
    severity: int | None

    @property
    def username(self) -> str:
        if self.type == "UserLockedOut":
            return extract_locked_user(self.name)
        return extract_failed_user(self.name)

    @property
    def ip(self) -> str:
        return extract_ip(" ".join([self.short_overview, self.overview, self.name]))


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(path: Path) -> dict[str, Any]:
    cfg = deep_merge(DEFAULT_CONFIG, load_json(path))
    env_webhook = os.getenv("SECURITY_DISCORD_WEBHOOK_URL")
    if env_webhook:
        cfg["discord"]["webhook_url"] = env_webhook
    for env_name, cfg_path in {
        "SECURITY_CLOUDFLARE_API_TOKEN": ("ban", "cloudflare", "api_token"),
        "SECURITY_CLOUDFLARE_ACCOUNT_ID": ("ban", "cloudflare", "account_id"),
        "SECURITY_CLOUDFLARE_LIST_ID": ("ban", "cloudflare", "list_id"),
    }.items():
        value = os.getenv(env_name)
        if value:
            target = cfg
            for key in cfg_path[:-1]:
                target = target[key]
            target[cfg_path[-1]] = value
    return cfg


def load_state(path: Path) -> dict[str, Any]:
    state = load_json(path)
    state.setdefault("last_id", 0)
    state.setdefault("bans", {})
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def extract_ip(text: str) -> str:
    for match in IP_RE.finditer(text):
        value = match.group("ip").strip("[]().,;")
        if valid_ip(value):
            return value
    return ""


def extract_failed_user(name: str) -> str:
    patterns = [
        r"failed login attempt (?:from|by|of user)\s+(?P<user>.+)$",
        r"(?P<user>.+?)\s+failed to log in$",
    ]
    for pattern in patterns:
        match = re.search(pattern, name, re.IGNORECASE)
        if match:
            return match.group("user").strip(" .")
    return name.strip()


def extract_locked_user(name: str) -> str:
    patterns = [
        r"user\s+(?P<user>.+?)\s+(?:has been locked|locked out|is locked)",
        r"(?P<user>.+?)\s+locked out$",
    ]
    for pattern in patterns:
        match = re.search(pattern, name, re.IGNORECASE)
        if match:
            return match.group("user").strip(" .")
    return name.strip()


def is_allowed(ip: str, allowlist: list[str]) -> bool:
    if not ip:
        return True
    address = ipaddress.ip_address(ip)
    for entry in allowlist:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if address in network:
            return True
    return False


def activity_rows(db_path: Path, after_id: int) -> list[Activity]:
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT Id, DateCreated, Type, Name, ShortOverview, Overview, UserId, ItemId, LogSeverity
            FROM ActivityLogs
            WHERE Id > ?
            ORDER BY Id ASC
            """,
            (after_id,),
        ).fetchall()
    return [
        Activity(
            id=int(row["Id"]),
            date=parse_dt(row["DateCreated"]),
            type=str(row["Type"] or ""),
            name=str(row["Name"] or ""),
            short_overview=str(row["ShortOverview"] or ""),
            overview=str(row["Overview"] or ""),
            user_id=str(row["UserId"] or ""),
            item_id=str(row["ItemId"] or ""),
            severity=int(row["LogSeverity"]) if row["LogSeverity"] is not None else None,
        )
        for row in rows
    ]


def initialize_last_id(db_path: Path, lookback_minutes: int) -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - (lookback_minutes * 60)
    last_id = 0
    for activity in activity_rows(db_path, 0):
        if activity.date.timestamp() >= cutoff:
            break
        last_id = activity.id
    return last_id


def discord_embed(activity: Activity, title: str, color: int, extra: dict[str, str]) -> dict[str, Any]:
    fields = [
        {"name": "Type", "value": activity.type or "unknown", "inline": True},
        {"name": "Username", "value": activity.username or "unknown", "inline": True},
        {"name": "IP", "value": activity.ip or "unknown", "inline": True},
        {"name": "Activity ID", "value": str(activity.id), "inline": True},
        {"name": "Time", "value": activity.date.isoformat().replace("+00:00", "Z"), "inline": True},
    ]
    for key, value in extra.items():
        fields.append({"name": key, "value": value or "unknown", "inline": True})
    fields.extend(
        [
            {"name": "Name", "value": activity.name[:1024] or "unknown", "inline": False},
            {"name": "Short Overview", "value": activity.short_overview[:1024] or "none", "inline": False},
        ]
    )
    return {
        "title": title,
        "color": color,
        "timestamp": activity.date.isoformat().replace("+00:00", "Z"),
        "fields": fields,
    }


def send_discord(cfg: dict[str, Any], activity: Activity, title: str, color: int, extra: dict[str, str]) -> None:
    webhook_url = str(cfg["discord"].get("webhook_url") or "")
    if not webhook_url:
        return
    payload: dict[str, Any] = {
        "username": cfg["discord"].get("username") or "Jellyfin Security",
        "embeds": [discord_embed(activity, title, color, extra)],
    }
    avatar_url = cfg["discord"].get("avatar_url")
    if avatar_url:
        payload["avatar_url"] = avatar_url
    res = requests.post(webhook_url, json=payload, timeout=20)
    res.raise_for_status()


def ban_with_command(command: str, ip: str, reason: str, activity: Activity) -> None:
    rendered = command.format(
        ip=shlex.quote(ip),
        reason=shlex.quote(reason),
        activity_id=activity.id,
        username=shlex.quote(activity.username or ""),
    )
    subprocess.run(rendered, shell=True, check=True, timeout=30)


def ban_with_cloudflare(cfg: dict[str, Any], ip: str, reason: str) -> None:
    cf = cfg["ban"]["cloudflare"]
    token = str(cf.get("api_token") or "")
    account_id = str(cf.get("account_id") or "")
    list_id = str(cf.get("list_id") or "")
    if not token or not account_id or not list_id:
        raise ValueError("Cloudflare ban action requires api_token, account_id, and list_id")

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/rules/lists/{list_id}/items"
    payload = [{"ip": ip, "comment": reason[:500]}]
    res = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if res.status_code == 409:
        return
    res.raise_for_status()


class SecurityAgent:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.failures: dict[str, deque[float]] = defaultdict(deque)

    def run(self) -> None:
        while True:
            try:
                cfg = load_config(self.config_path)
                if not cfg.get("enabled", True):
                    time.sleep(60)
                    continue
                self.tick(cfg)
                time.sleep(max(5, int(cfg.get("poll_interval_seconds", 15))))
            except Exception:
                LOG.exception("security agent tick failed")
                time.sleep(30)

    def tick(self, cfg: dict[str, Any]) -> None:
        state_path = Path(str(cfg.get("state_path") or DEFAULT_CONFIG["state_path"]))
        state = load_state(state_path)
        db_path = Path(str(cfg.get("database_path") or DEFAULT_CONFIG["database_path"]))
        if not state.get("last_id"):
            state["last_id"] = initialize_last_id(db_path, int(cfg.get("startup_lookback_minutes", 5)))
            save_state(state_path, state)

        alert_types = set(cfg.get("alert_types") or [])
        for activity in activity_rows(db_path, int(state.get("last_id") or 0)):
            state["last_id"] = max(int(state.get("last_id") or 0), activity.id)
            if activity.type not in alert_types:
                continue
            self.handle_activity(cfg, state, activity)
        save_state(state_path, state)

    def handle_activity(self, cfg: dict[str, Any], state: dict[str, Any], activity: Activity) -> None:
        if activity.type == "AuthenticationFailed":
            ip = activity.ip
            count = self.record_failure(cfg, ip)
            send_discord(
                cfg,
                activity,
                "Jellyfin Failed Login",
                0xD83A34,
                {"Failures In Window": str(count)},
            )
            if self.should_ban(cfg, state, ip, count):
                self.ban(cfg, state, activity, ip, f"{count} failed Jellyfin logins")
            return

        if activity.type == "UserLockedOut":
            send_discord(cfg, activity, "Jellyfin User Locked Out", 0xA855F7, {})

    def record_failure(self, cfg: dict[str, Any], ip: str) -> int:
        if not ip:
            return 0
        window = int(cfg["thresholds"].get("window_seconds") or 600)
        now = time.time()
        attempts = self.failures[ip]
        attempts.append(now)
        while attempts and attempts[0] < now - window:
            attempts.popleft()
        return len(attempts)

    def should_ban(self, cfg: dict[str, Any], state: dict[str, Any], ip: str, count: int) -> bool:
        if not cfg["ban"].get("enabled") or not ip:
            return False
        if is_allowed(ip, list(cfg["ban"].get("allowlist") or [])):
            return False
        threshold = int(cfg["thresholds"].get("failures") or 5)
        if count < threshold:
            return False
        ban = state.get("bans", {}).get(ip)
        if ban and float(ban.get("until", 0)) > time.time():
            return False
        return True

    def ban(self, cfg: dict[str, Any], state: dict[str, Any], activity: Activity, ip: str, reason: str) -> None:
        action = str(cfg["ban"].get("action") or "none").lower()
        if action == "cloudflare":
            ban_with_cloudflare(cfg, ip, reason)
        elif action == "command":
            ban_with_command(str(cfg["ban"].get("command") or ""), ip, reason, activity)
        elif action != "none":
            raise ValueError(f"unknown ban action: {action}")

        until = time.time() + int(cfg["ban"].get("duration_seconds") or 86400)
        state.setdefault("bans", {})[ip] = {"reason": reason, "until": until, "at": utc_now()}
        send_discord(cfg, activity, "Jellyfin IP Ban Triggered", 0x111827, {"Reason": reason, "Action": action})
        LOG.warning("ban triggered for %s via %s: %s", ip, action, reason)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/config/security-alerts.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    SecurityAgent(Path(args.config)).run()


if __name__ == "__main__":
    main()

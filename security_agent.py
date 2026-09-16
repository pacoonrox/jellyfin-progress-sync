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
from zoneinfo import ZoneInfo

import requests


LOG = logging.getLogger("jellyfin-security-agent")
IP_RE = re.compile(
    r"(?P<ip>(?:\d{1,3}\.){3}\d{1,3}|(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4})"
)
DENIED_LOG_RE = re.compile(
    r"Authentication request for (?P<username>.+?) has been denied \((?P<source>.*)\)\.",
    re.IGNORECASE,
)
LOG_TS_RE = re.compile(r"^\[(?P<time>\d{2}:\d{2}:\d{2})\]")


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
    "log_paths": ["/config/log"],
    "state_path": "/config/security-agent-state.json",
    "poll_interval_seconds": 15,
    "startup_lookback_minutes": 5,
    "alert_types": [
        "AuthenticationFailed",
        "UserLockedOut",
        "TwoFactorAuthenticationFailed",
        "QuickConnectPortalEntered",
    ],
    "discord": {
        "webhook_url": "",
        "username": "Jellyfin Security",
        "avatar_url": "",
        "mention": "@dakxp",
    },
    "thresholds": {
        "failures": 5,
        "window_seconds": 600,
    },
    "proxy": {
        "trust_headers": False,
        "trusted_proxies": [],
        "header_names": [
            "CF-Connecting-IP",
            "CF-Connecting-IPv6",
            "True-Client-IP",
            "X-Forwarded-For",
            "X-Real-IP",
            "Forwarded",
        ],
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
    source: str = "activity-log"
    account_name: str = ""

    @property
    def username(self) -> str:
        if self.account_name:
            return self.account_name
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
    override = load_json(path)
    cfg = deep_merge(DEFAULT_CONFIG, override)
    if set(override.get("alert_types") or []) == {"AuthenticationFailed", "UserLockedOut"}:
        # Upgrade installations still using the previous default alert set.
        cfg["alert_types"] = list(DEFAULT_CONFIG["alert_types"])
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
    state.setdefault("log_offsets", {})
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


def extract_forwarded_ips(text: str, header_names: list[str] | None = None) -> list[str]:
    ips: list[str] = []
    allowed_headers = {header.lower() for header in header_names or []}
    patterns = [
        ("CF-Connecting-IP", r"\bCF-Connecting-IP\s*[:=]\s*(?P<value>[^\s,;]+)"),
        ("CF-Connecting-IPv6", r"\bCF-Connecting-IPv6\s*[:=]\s*(?P<value>[^\s,;]+)"),
        ("True-Client-IP", r"\bTrue-Client-IP\s*[:=]\s*(?P<value>[^\s,;]+)"),
        ("X-Forwarded-For", r"\bX-Forwarded-For\s*[:=]\s*(?P<value>[^\n\r;]+)"),
        ("X-Real-IP", r"\bX-Real-IP\s*[:=]\s*(?P<value>[^\s,;]+)"),
        ("Forwarded", r"\bForwarded\s*[:=]\s*(?P<value>[^\n\r]+)"),
    ]
    for header_name, pattern in patterns:
        if allowed_headers and header_name.lower() not in allowed_headers:
            continue
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = match.group("value")
            for ip_match in IP_RE.finditer(value):
                ip = ip_match.group("ip").strip("[]\"().,;")
                if valid_ip(ip):
                    ips.append(ip)
    return list(dict.fromkeys(ips))


def extract_request_host(text: str) -> str:
    patterns = [
        r"\bX-Forwarded-Host\s*[:=]\s*(?P<value>[^\s,;]+)",
        r"\bX-Original-Host\s*[:=]\s*(?P<value>[^\s,;]+)",
        r"\bHost\s*[:=]\s*(?P<value>[^\s,;]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group("value").strip("[]\"().,;")
    return ""


def extract_source_value(text: str, name: str) -> str:
    pattern = rf"(?:^|;\s*){re.escape(name)}\s*:\s*(?P<value>.*?)(?=;\s*[\w-]+\s*:|$)"
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return ""
    return match.group("value").strip("[]\"().,; ")


def clean_discord_value(value: str | None, fallback: str = "unknown") -> str:
    value = (value or "").strip()
    if not value:
        return fallback
    return value[:1024]


def bool_icon(value: bool) -> str:
    return "Yes" if value else "No"


def display_time(value: datetime) -> str:
    eastern = value.astimezone(ZoneInfo("America/New_York"))
    return eastern.strftime("%B %-d, %Y at %-I:%M:%S %p %Z")


def client_ip_for_activity(cfg: dict[str, Any], activity: Activity) -> str:
    direct_ip = activity.ip
    proxy_cfg = cfg.get("proxy") or {}
    if not proxy_cfg.get("trust_headers"):
        return direct_ip

    forwarded_ips = extract_forwarded_ips(
        " ".join([activity.short_overview, activity.overview, activity.name]),
        list(proxy_cfg.get("header_names") or []),
    )
    if not forwarded_ips:
        return direct_ip

    trusted_proxies = list(proxy_cfg.get("trusted_proxies") or [])
    if direct_ip and trusted_proxies and not is_allowed(direct_ip, trusted_proxies):
        return direct_ip

    return forwarded_ips[0]


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


def security_audit_rows(db_path: Path, after_id: int) -> list[Activity]:
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT s.Id, s.TimestampUtc, s.Event, s.Source, s.Result, s.Detail,
                       COALESCE(target.Username, actor.Username, 'Unknown') AS AccountName
                FROM SecurityAuditRecords AS s
                LEFT JOIN Users AS target ON target.Id = s.TargetUserId
                LEFT JOIN Users AS actor ON actor.Id = s.ActingUserId
                WHERE s.Id > ?
                  AND s.Event IN ('TwoFactorAuthenticationFailed', 'QuickConnectPortalEntered')
                ORDER BY s.Id ASC
                """,
                (after_id,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise

    activities: list[Activity] = []
    for row in rows:
        event_type = str(row["Event"] or "")
        account_name = str(row["AccountName"] or "Unknown")
        name = (
            f"Wrong two-factor code entered for {account_name}"
            if event_type == "TwoFactorAuthenticationFailed"
            else f"{account_name} entered the Quick Connect portal"
        )
        activities.append(Activity(
            id=int(row["Id"]),
            date=parse_dt(str(row["TimestampUtc"] or "")),
            type=event_type,
            name=name,
            short_overview=str(row["Detail"] or ""),
            overview=f"Result: {row['Result']}; Source: {row['Source']}; {row['Detail'] or ''}",
            user_id="",
            item_id="",
            severity=4 if event_type == "TwoFactorAuthenticationFailed" else 2,
            source="security-audit",
            account_name=account_name,
        ))
    return activities


def iter_log_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(path.glob("*.log")))
        elif path.exists():
            files.append(path)
    return sorted(set(files), key=lambda p: str(p))


def log_activity(path: Path, line_number: int, line: str) -> Activity | None:
    match = DENIED_LOG_RE.search(line)
    if not match:
        return None
    source = match.group("source").strip()
    ip = extract_ip(source)
    username = match.group("username").strip()
    timestamp = parse_dt(None)
    ts_match = LOG_TS_RE.search(line)
    if ts_match:
        now = datetime.now(timezone.utc)
        hour, minute, second = [int(part) for part in ts_match.group("time").split(":")]
        timestamp = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    return Activity(
        id=line_number,
        date=timestamp,
        type="AuthenticationFailed",
        name=f"Failed login attempt from {username}",
        short_overview=f"IP address: {ip}",
        overview=line.strip(),
        user_id="",
        item_id=str(path),
        severity=4,
        source="jellyfin-log",
    )


def log_rows(paths: list[str], state: dict[str, Any]) -> list[Activity]:
    offsets: dict[str, int] = state.setdefault("log_offsets", {})
    rows: list[Activity] = []
    for path in iter_log_files(paths):
        key = str(path)
        size = path.stat().st_size
        if key not in offsets:
            offsets[key] = size
            continue
        offset = int(offsets.get(key) or 0)
        if size < offset:
            offset = 0
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            for line_number, line in enumerate(handle, start=1):
                activity = log_activity(path, line_number, line)
                if activity is not None:
                    rows.append(activity)
            offsets[key] = handle.tell()
    return rows


def initialize_last_id(db_path: Path, lookback_minutes: int) -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - (lookback_minutes * 60)
    last_id = 0
    for activity in activity_rows(db_path, 0):
        if activity.date.timestamp() >= cutoff:
            break
        last_id = activity.id
    return last_id


def initialize_security_audit_last_id(db_path: Path, lookback_minutes: int) -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - (lookback_minutes * 60)
    last_id = 0
    for activity in security_audit_rows(db_path, 0):
        if activity.date.timestamp() >= cutoff:
            break
        last_id = activity.id
    return last_id


def discord_embed(
    activity: Activity,
    title: str,
    color: int,
    extra: dict[str, str],
    display_ip: str | None = None,
) -> dict[str, Any]:
    username = clean_discord_value(activity.username)
    domain = clean_discord_value(extra.get("Domain"), "Unavailable from log fallback")
    client_ip = clean_discord_value(display_ip or extra.get("Client IP") or activity.ip)
    failures = clean_discord_value(extra.get("Failures In Window"), "0")
    source = clean_discord_value(extra.get("Source"), activity.source or "unknown")
    source_text = " ".join([activity.short_overview, activity.overview, activity.name])
    device = clean_discord_value(extra.get("Device") or extract_source_value(source_text, "Device"), "unknown")
    user_agent = clean_discord_value(extra.get("User-Agent") or extract_source_value(source_text, "User-Agent"), "unknown")

    fields = [
        {"name": "Account", "value": username, "inline": True},
        {"name": "Client IP", "value": client_ip, "inline": True},
        {"name": "Domain", "value": domain, "inline": True},
        {"name": "Failures", "value": failures, "inline": True},
        {"name": "Time", "value": display_time(activity.date), "inline": True},
        {"name": "Device", "value": device, "inline": True},
        {"name": "User-Agent", "value": user_agent, "inline": False},
    ]

    return {
        "title": title,
        "description": f"Failed Jellyfin login for `{username}`." if activity.type == "AuthenticationFailed" else activity.name[:2048],
        "color": color,
        "timestamp": activity.date.isoformat().replace("+00:00", "Z"),
        "fields": fields,
        "footer": {
            "text": f"{source} | Activity {activity.id}"
        },
    }


def send_discord(
    cfg: dict[str, Any],
    activity: Activity,
    title: str,
    color: int,
    extra: dict[str, str],
    display_ip: str | None = None,
) -> None:
    webhook_url = str(cfg["discord"].get("webhook_url") or "")
    if not webhook_url:
        return
    payload: dict[str, Any] = {
        "username": cfg["discord"].get("username") or "Jellyfin Security",
        "embeds": [discord_embed(activity, title, color, extra, display_ip)],
    }
    mention = str(cfg["discord"].get("mention") or "").strip()
    if mention:
        payload["content"] = mention
        payload["allowed_mentions"] = {"parse": ["users", "roles"]}
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
        if not state.get("last_security_audit_id"):
            state["last_security_audit_id"] = initialize_security_audit_last_id(
                db_path,
                int(cfg.get("startup_lookback_minutes", 5)),
            )
            save_state(state_path, state)

        alert_types = set(cfg.get("alert_types") or [])
        processed_db_auth_failures = 0
        for activity in activity_rows(db_path, int(state.get("last_id") or 0)):
            state["last_id"] = max(int(state.get("last_id") or 0), activity.id)
            if activity.type not in alert_types:
                continue
            if activity.type == "AuthenticationFailed":
                processed_db_auth_failures += 1
            self.handle_activity(cfg, state, activity)
        for activity in security_audit_rows(db_path, int(state.get("last_security_audit_id") or 0)):
            state["last_security_audit_id"] = max(
                int(state.get("last_security_audit_id") or 0),
                activity.id,
            )
            if activity.type not in alert_types:
                continue
            self.handle_activity(cfg, state, activity)
        for activity in log_rows(list(cfg.get("log_paths") or []), state):
            if activity.type not in alert_types:
                continue
            if activity.type == "AuthenticationFailed" and processed_db_auth_failures:
                continue
            self.handle_activity(cfg, state, activity)
        save_state(state_path, state)

    def handle_activity(self, cfg: dict[str, Any], state: dict[str, Any], activity: Activity) -> None:
        if activity.type == "AuthenticationFailed":
            ip = client_ip_for_activity(cfg, activity)
            count = self.record_failure(cfg, ip)
            extra = {"Failures In Window": str(count), "Source": activity.source}
            request_host = extract_request_host(" ".join([activity.short_overview, activity.overview, activity.name]))
            if request_host:
                extra["Domain"] = request_host
            device = extract_source_value(" ".join([activity.short_overview, activity.overview, activity.name]), "Device")
            if device:
                extra["Device"] = device
            user_agent = extract_source_value(" ".join([activity.short_overview, activity.overview, activity.name]), "User-Agent")
            if user_agent:
                extra["User-Agent"] = user_agent
            if ip and ip != activity.ip:
                extra["Client IP"] = ip
                extra["Proxy IP"] = activity.ip
            send_discord(
                cfg,
                activity,
                "Jellyfin Failed Login",
                0xD83A34,
                extra,
                display_ip=ip,
            )
            if self.should_ban(cfg, state, ip, count):
                self.ban(cfg, state, activity, ip, f"{count} failed Jellyfin logins")
            return

        if activity.type == "UserLockedOut":
            send_discord(cfg, activity, "Jellyfin User Locked Out", 0xA855F7, {})
            return

        if activity.type == "TwoFactorAuthenticationFailed":
            ip = client_ip_for_activity(cfg, activity)
            count = self.record_failure(cfg, ip)
            extra = {"Failures In Window": str(count), "Source": activity.source}
            request_host = extract_request_host(" ".join([activity.short_overview, activity.overview]))
            if request_host:
                extra["Domain"] = request_host
            send_discord(cfg, activity, "Jellyfin Wrong 2FA Code", 0xF97316, extra, display_ip=ip)
            if self.should_ban(cfg, state, ip, count):
                self.ban(cfg, state, activity, ip, f"{count} failed Jellyfin 2FA attempts")
            return

        if activity.type == "QuickConnectPortalEntered":
            ip = client_ip_for_activity(cfg, activity)
            extra = {"Source": activity.source}
            request_host = extract_request_host(" ".join([activity.short_overview, activity.overview]))
            if request_host:
                extra["Domain"] = request_host
            send_discord(cfg, activity, "Jellyfin Quick Connect Portal Entered", 0x2563EB, extra, display_ip=ip)

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

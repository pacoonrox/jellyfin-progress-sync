#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import discord
import requests
from discord import app_commands
from discord.ext import tasks

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("jellyfin-discord-bot")


@dataclass
class BotConfigStore:
    path: Path
    data: dict[str, Any]
    lock: threading.RLock

    @classmethod
    def load(cls, path: Path) -> "BotConfigStore":
        if not path.exists():
            sample = ROOT / "discord-bot.example.json"
            if sample.exists():
                shutil.copyfile(sample, path)
            else:
                path.write_text("{}", encoding="utf-8")
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("discord", {})
        data["discord"].setdefault("bot_token", "")
        data["discord"].setdefault("guild_id", None)
        data["discord"].setdefault("admin_channel_id", None)
        data["discord"].setdefault("admin_control_message_id", None)
        data["discord"].setdefault("log_channel_id", None)
        data["discord"].setdefault("category_id", None)
        data.setdefault("jellyfin", {})
        data["jellyfin"].setdefault("base_url", "http://127.0.0.1:8096")
        data["jellyfin"].setdefault("api_key", "")
        data.setdefault("users", {})
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


class JellyfinAdminClient:
    """Talks to Jellyfin as a single admin API key.

    An API key authenticates as Administrator with no user identity of its
    own, so every call below explicitly names the target userId — there is
    no notion of "acting as yourself" from the bot's side.
    """

    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"X-Emby-Token": self.api_key}

    def _get(self, path: str, **params: Any) -> Any:
        res = requests.get(
            f"{self.base}{path}",
            params={k: v for k, v in params.items() if v is not None},
            headers=self._headers(),
            timeout=15,
        )
        res.raise_for_status()
        return res.json() if res.content else None

    def _post(self, path: str, json_body: Any = None, **params: Any) -> Any:
        res = requests.post(
            f"{self.base}{path}",
            params={k: v for k, v in params.items() if v is not None},
            json=json_body,
            headers=self._headers(),
            timeout=15,
        )
        res.raise_for_status()
        return res.json() if res.content else None

    def users(self) -> list[dict[str, Any]]:
        return self._get("/Users")

    def find_user(self, username: str) -> dict[str, Any] | None:
        target = username.strip().lower()
        for user in self.users():
            if str(user.get("Name", "")).lower() == target:
                return user
        return None

    def quick_connect_enabled(self) -> bool:
        return bool(self._get("/QuickConnect/Enabled"))

    def quick_connect_authorize(self, code: str, user_id: str) -> bool:
        return bool(self._post("/QuickConnect/Authorize", code=code, userId=user_id))

    def device_approval_enabled(self) -> bool:
        return bool(self._get("/DeviceApproval/Enabled"))

    def queue(self) -> list[dict[str, Any]]:
        return self._get("/DeviceApproval/Queue") or []

    def select(self, request_id: str, user_id: str) -> dict[str, Any]:
        return self._post(f"/DeviceApproval/Queue/{request_id}/Select", userId=user_id)

    def confirm(self, request_id: str, user_id: str, matches: bool, trust_device: bool) -> None:
        self._post(
            f"/DeviceApproval/Queue/{request_id}/Confirm",
            json_body={"Matches": matches, "TrustDevice": trust_device},
            userId=user_id,
        )

    def deny(self, request_id: str, user_id: str) -> None:
        self._post(f"/DeviceApproval/Queue/{request_id}/Deny", userId=user_id)


_CHANNEL_NAME_RE = re.compile(r"[^a-z0-9-]+")


def sanitize_channel_name(name: str) -> str:
    slug = _CHANNEL_NAME_RE.sub("-", name.lower()).strip("-")
    return f"jellyfin-{slug or 'user'}"


def find_mapping_by_channel(store: BotConfigStore, channel_id: int | None) -> dict[str, Any] | None:
    if channel_id is None:
        return None
    cfg = store.snapshot()
    for discord_id, entry in cfg.get("users", {}).items():
        if str(entry.get("channel_id")) == str(channel_id):
            return {
                "discord_id": int(discord_id),
                "jellyfin_user_id": entry["jellyfin_user_id"],
                "jellyfin_username": entry["jellyfin_username"],
            }
    return None


def to_discord_timestamp(iso_value: str | None, style: str = "R") -> str:
    if not iso_value:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(iso_value)
    except ValueError:
        return iso_value
    return f"<t:{int(dt.timestamp())}:{style}>"


def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.guild.owner_id == interaction.user.id


def format_portal_telemetry(entry: dict[str, Any], *, include_ip: bool = False) -> str:
    """Mirrors the fields and layout of the deviceApprovalCard on the actual
    Quick Sign-On web page (quickConnect/index.tsx): device name, client,
    platform, connection domain, entered/expires timestamps, and — only for
    the admin channel, same restriction as the website's isAdministrator
    check — the requesting IP.
    """
    lines = [
        f"**{entry.get('DeviceName') or 'Unknown device'}**",
        f"Client: {entry.get('AppName', '?')} {entry.get('AppVersion', '')}".rstrip(),
        f"Platform: {entry.get('Platform') or 'Unknown'} {entry.get('OsVersion', '')}".rstrip(),
        f"Connection domain: {entry.get('ConnectionDomain') or 'Unknown'}",
        f"Entered queue: {to_discord_timestamp(entry.get('CreatedUtc'))}",
        f"Expires: {to_discord_timestamp(entry.get('ExpiresUtc'))}",
    ]
    if include_ip:
        lines.append(f"Requesting IP: {entry.get('RequestingIpAddress') or 'Unknown'}")
    return "\n".join(lines)


async def create_user_channel(
    guild: discord.Guild, member: discord.Member, category: discord.CategoryChannel | None
) -> discord.TextChannel:
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    return await guild.create_text_channel(
        name=sanitize_channel_name(member.display_name),
        overwrites=overwrites,
        category=category,
        topic=f"Jellyfin Quick Sign-On for Discord user {member.id}.",
        reason=f"Jellyfin sign-on channel linked by {member.display_name}",
    )


class QueuePickerView(discord.ui.View):
    def __init__(
        self,
        bot: "SignOnBot",
        queue: list[dict[str, Any]],
        target_user_id: str,
        target_username: str,
    ):
        super().__init__(timeout=120)
        self.bot = bot
        self.target_user_id = target_user_id
        self.target_username = target_username

        options = [
            discord.SelectOption(
                label=str(entry.get("DeviceName") or "Unknown device")[:100],
                description=f"{entry.get('AppName', '?')} · {entry.get('Platform', '?')}"[:100],
                value=entry["Id"],
            )
            for entry in queue[:25]
        ]
        select = discord.ui.Select(placeholder="Choose a device", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        request_id = interaction.data["values"][0]  # type: ignore[index]
        entry = self.bot.portal_entries.get(request_id) or {}
        device_name = entry.get("DeviceName") or "a device"
        device_ip = entry.get("RequestingIpAddress") or "unknown IP"
        device_domain = entry.get("ConnectionDomain") or "unknown domain"
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(self.bot.jellyfin.select, request_id, self.target_user_id)
            await asyncio.to_thread(self.bot.jellyfin.confirm, request_id, self.target_user_id, True, False)
        except Exception as exc:
            await interaction.followup.send(f"Couldn't approve that device: {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"Approved. Signed in as **{self.target_username}**.", ephemeral=True)
        await self.bot.post_log(f"{interaction.user.mention} approved **{device_name}** ({device_ip}, {device_domain}) to sign in as **{self.target_username}**.")
        await self.bot.cleanup_portal_messages(request_id)
        self.stop()


class _PortalRequestView(discord.ui.View):
    """Each instance is scoped to one specific device request and posted
    once. Resolution (approve, or a hard deny) is handled by the bot's
    cleanup_portal_messages, which removes every broadcast copy of this
    request across every channel — not just this one — so a plain
    (non-persistent) timeout matching the request's own 5-minute
    server-side expiry is enough; it doesn't need to survive a bot restart
    the way the standing Quick Connect button does.
    """

    def __init__(self, bot: "SignOnBot", request_id: str) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.request_id = request_id

    def _device_name(self) -> str:
        return (self.bot.portal_entries.get(self.request_id) or {}).get("DeviceName") or "a device"

    def _device_ip(self) -> str:
        return (self.bot.portal_entries.get(self.request_id) or {}).get("RequestingIpAddress") or "unknown IP"

    def _device_domain(self) -> str:
        return (self.bot.portal_entries.get(self.request_id) or {}).get("ConnectionDomain") or "unknown domain"


class UserPortalRequestView(_PortalRequestView):
    def __init__(self, bot: "SignOnBot", request_id: str, jellyfin_user_id: str, jellyfin_username: str):
        super().__init__(bot, request_id)
        self.jellyfin_user_id = jellyfin_user_id
        self.jellyfin_username = jellyfin_username

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        device_name = self._device_name()
        device_ip = self._device_ip()
        device_domain = self._device_domain()
        try:
            await asyncio.to_thread(self.bot.jellyfin.select, self.request_id, self.jellyfin_user_id)
            await asyncio.to_thread(self.bot.jellyfin.confirm, self.request_id, self.jellyfin_user_id, True, False)
        except Exception as exc:
            await interaction.followup.send(f"Couldn't approve that device: {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"Approved. Signed in as **{self.jellyfin_username}**.", ephemeral=True)
        await self.bot.post_log(f"{interaction.user.mention} approved **{device_name}** ({device_ip}, {device_domain}) to sign in as **{self.jellyfin_username}**.")
        await self.bot.cleanup_portal_messages(self.request_id)

    @discord.ui.button(label="Not this device", style=discord.ButtonStyle.secondary)
    async def not_mine(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        device_name = self._device_name()
        device_ip = self._device_ip()
        device_domain = self._device_domain()
        try:
            await asyncio.to_thread(self.bot.jellyfin.select, self.request_id, self.jellyfin_user_id)
            await asyncio.to_thread(self.bot.jellyfin.confirm, self.request_id, self.jellyfin_user_id, False, False)
        except Exception as exc:
            await interaction.followup.send(f"Couldn't release that device: {exc}", ephemeral=True)
            return
        await interaction.followup.send("Released back to the queue for someone else.", ephemeral=True)
        # Deliberately not cleaned up — Confirm(false) only releases the selection,
        # the request is still genuinely Pending and other channels should keep it.
        await self.bot.post_log(f"{interaction.user.mention} marked **{device_name}** ({device_ip}, {device_domain}) \"not this device\" — still pending for others.")


class AdminPortalRequestView(_PortalRequestView):
    """Admin has no fixed target account, so approval is a dropdown of
    current Jellyfin users (fetched once, at broadcast time) rather than a
    modal text field — Discord modals can't hold a select menu, only text
    inputs. "Not this device" is a hard removal (Deny) here, not a release
    back to Pending, since an admin's "no" is a moderation decision rather
    than one user passing on a request that might still be someone else's.
    """

    def __init__(self, bot: "SignOnBot", request_id: str, users: list[dict[str, Any]]):
        super().__init__(bot, request_id)
        self.user_lookup = {u["Id"]: u.get("Name", "?") for u in users}

        select = discord.ui.Select(
            placeholder="Approve for...",
            options=[discord.SelectOption(label=u.get("Name", "?")[:100], value=u["Id"]) for u in users[:25]],
            row=0,
        )
        select.callback = self._on_approve_select
        self.add_item(select)

    async def _on_approve_select(self, interaction: discord.Interaction) -> None:
        user_id = interaction.data["values"][0]  # type: ignore[index]
        username = self.user_lookup.get(user_id, "?")
        device_name = self._device_name()
        device_ip = self._device_ip()
        device_domain = self._device_domain()
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(self.bot.jellyfin.select, self.request_id, user_id)
            await asyncio.to_thread(self.bot.jellyfin.confirm, self.request_id, user_id, True, False)
        except Exception as exc:
            await interaction.followup.send(f"Couldn't approve that device: {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"Approved. Signed the device in as **{username}**.", ephemeral=True)
        await self.bot.post_log(f"{interaction.user.mention} approved **{device_name}** ({device_ip}, {device_domain}) to sign in as **{username}** (admin).")
        await self.bot.cleanup_portal_messages(self.request_id)

    @discord.ui.button(label="Not this device", style=discord.ButtonStyle.danger, row=1)
    async def not_mine(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        device_name = self._device_name()
        device_ip = self._device_ip()
        device_domain = self._device_domain()
        actor_id = str(uuid.uuid4())
        try:
            await asyncio.to_thread(self.bot.jellyfin.select, self.request_id, actor_id)
            await asyncio.to_thread(self.bot.jellyfin.deny, self.request_id, actor_id)
        except Exception as exc:
            await interaction.followup.send(f"Couldn't deny that request: {exc}", ephemeral=True)
            return
        await interaction.followup.send("Denied — removed from the queue.", ephemeral=True)
        await self.bot.post_log(f"{interaction.user.mention} denied **{device_name}** ({device_ip}, {device_domain}) (admin) — removed from the queue.")
        await self.bot.cleanup_portal_messages(self.request_id)


class UserQuickConnectModal(discord.ui.Modal, title="Enter Quick Connect Code"):
    code: discord.ui.TextInput = discord.ui.TextInput(label="6-digit code", placeholder="123456", min_length=6, max_length=6)

    def __init__(self, jellyfin: JellyfinAdminClient, jellyfin_user_id: str, jellyfin_username: str):
        super().__init__()
        self.jellyfin = jellyfin
        self.jellyfin_user_id = jellyfin_user_id
        self.jellyfin_username = jellyfin_username

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            ok = await asyncio.to_thread(self.jellyfin.quick_connect_authorize, self.code.value.strip(), self.jellyfin_user_id)
        except Exception as exc:
            await interaction.followup.send(f"Jellyfin rejected that: {exc}", ephemeral=True)
            return
        if ok:
            await interaction.followup.send(f"Signed in as **{self.jellyfin_username}**.", ephemeral=True)
        else:
            await interaction.followup.send("That code wasn't accepted. It may be wrong or expired.", ephemeral=True)


class AdminQuickConnectUserPickerView(discord.ui.View):
    """Second step of the admin Quick Connect flow: the code is already
    known, this just picks which account to authorize it for — a dropdown
    of current Jellyfin users, not a typed name, same reasoning as
    AdminPortalRequestView's Approve dropdown.
    """

    def __init__(self, bot: "SignOnBot", code: str, users: list[dict[str, Any]]):
        super().__init__(timeout=120)
        self.bot = bot
        self.code = code

        self.user_lookup = {u["Id"]: u.get("Name", "?") for u in users}
        select = discord.ui.Select(
            placeholder="Sign this code in as...",
            options=[discord.SelectOption(label=u.get("Name", "?")[:100], value=u["Id"]) for u in users[:25]],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        user_id = interaction.data["values"][0]  # type: ignore[index]
        username = self.user_lookup.get(user_id, "?")
        await interaction.response.defer(ephemeral=True)
        try:
            ok = await asyncio.to_thread(self.bot.jellyfin.quick_connect_authorize, self.code, user_id)
        except Exception as exc:
            await interaction.followup.send(f"Jellyfin rejected that: {exc}", ephemeral=True)
            return
        if ok:
            await interaction.followup.send(f"Signed the device in as **{username}**.", ephemeral=True)
        else:
            await interaction.followup.send("That code wasn't accepted.", ephemeral=True)
        self.stop()


class AdminQuickConnectCodeModal(discord.ui.Modal, title="Admin: Enter Quick Connect Code"):
    code: discord.ui.TextInput = discord.ui.TextInput(label="6-digit code", placeholder="123456", min_length=6, max_length=6)

    def __init__(self, bot: "SignOnBot"):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            users = await asyncio.to_thread(self.bot.jellyfin.users)
        except Exception as exc:
            await interaction.followup.send(f"Couldn't reach Jellyfin: {exc}", ephemeral=True)
            return
        if not users:
            await interaction.followup.send("No Jellyfin users found.", ephemeral=True)
            return
        view = AdminQuickConnectUserPickerView(self.bot, self.code.value.strip(), users)
        await interaction.followup.send("Sign this code in as:", view=view, ephemeral=True)


class QuickConnectButtonView(discord.ui.View):
    """A standing button, persistent across bot restarts (fixed custom_id + timeout=None).

    Which modal it opens depends on the channel it's clicked in. A linked
    user channel is locked to that channel's own account. The admin channel
    only asks for the code here — picking who it's for is a second-step
    dropdown (AdminQuickConnectUserPickerView), not a field in this same
    modal, since a modal can't hold a select menu and typing a name invites
    typos on an action that instantly signs someone in.
    """

    def __init__(self, bot: "SignOnBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Enter Quick Connect Code", style=discord.ButtonStyle.primary, custom_id="jellyfin:quickconnect-button")
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cfg = self.bot.store.snapshot()
        admin_channel_id = cfg["discord"].get("admin_channel_id")
        if admin_channel_id and interaction.channel_id == int(admin_channel_id):
            await interaction.response.send_modal(AdminQuickConnectCodeModal(self.bot))
            return
        mapping = find_mapping_by_channel(self.bot.store, interaction.channel_id)
        if mapping is None:
            await interaction.response.send_message("This channel isn't linked to a Jellyfin account.", ephemeral=True)
            return
        await interaction.response.send_modal(
            UserQuickConnectModal(self.bot.jellyfin, mapping["jellyfin_user_id"], mapping["jellyfin_username"])
        )


ADMIN_CONTROL_MESSAGE = (
    "This is the admin sign-on channel. Use the button below (or `/admin-signin`) for a device showing "
    "a Quick Connect code, or `/admin-queue` to approve a device waiting in the Quick Sign-On queue — "
    "both let you pick any Jellyfin account."
)


class SignOnBot(discord.Client):
    def __init__(self, store: BotConfigStore):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.store = store
        cfg = store.snapshot()
        self.jellyfin = JellyfinAdminClient(cfg["jellyfin"]["base_url"], cfg["jellyfin"]["api_key"])
        self.announced_requests: set[str] = set()
        # request_id -> [(channel_id, message_id), ...] for every broadcast copy of that
        # request, so a real resolution (approve, or a hard deny) can remove it everywhere
        # at once instead of leaving stale prompts in channels that didn't act on it.
        self.portal_messages: dict[str, list[tuple[int, int]]] = {}
        self.portal_entries: dict[str, dict[str, Any]] = {}

    async def post_log(self, message: str) -> None:
        cfg = self.store.snapshot()
        log_channel_id = cfg["discord"].get("log_channel_id")
        if not log_channel_id:
            return
        channel = self.get_channel(int(log_channel_id))
        if channel is None:
            return
        try:
            await channel.send(message)
        except discord.HTTPException:
            LOG.warning("Couldn't post to the log channel %s", log_channel_id)

    async def cleanup_portal_messages(self, request_id: str) -> None:
        tracked = self.portal_messages.pop(request_id, [])
        self.portal_entries.pop(request_id, None)
        for channel_id, message_id in tracked:
            channel = self.get_channel(channel_id)
            if channel is None:
                continue
            try:
                message = await channel.fetch_message(message_id)
                await message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                LOG.warning("Couldn't delete a portal-queue message in channel %s", channel_id)

    async def ensure_admin_control_message(self) -> None:
        cfg = self.store.snapshot()
        admin_channel_id = cfg["discord"].get("admin_channel_id")
        if not admin_channel_id:
            return
        channel = self.get_channel(int(admin_channel_id))
        if channel is None:
            return
        message_id = cfg["discord"].get("admin_control_message_id")
        if message_id:
            try:
                await channel.fetch_message(int(message_id))
                return
            except discord.NotFound:
                pass
            except discord.HTTPException:
                return
        try:
            message = await channel.send(ADMIN_CONTROL_MESSAGE, view=QuickConnectButtonView(self))
        except discord.Forbidden:
            LOG.warning("Missing permission to post the admin control message in channel %s", admin_channel_id)
            return
        cfg = self.store.snapshot()
        cfg["discord"]["admin_control_message_id"] = str(message.id)
        self.store.save(cfg)

    async def setup_hook(self) -> None:
        guild_id = self.store.snapshot()["discord"].get("guild_id")
        if guild_id:
            guild_obj = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
        else:
            await self.tree.sync()
        self.add_view(QuickConnectButtonView(self))
        self.watch_portal_queue.start()
        self.ensure_admin_control_message_loop.start()

    async def on_ready(self) -> None:
        LOG.info("Logged in as %s", self.user)

    @tasks.loop(minutes=10)
    async def ensure_admin_control_message_loop(self) -> None:
        await self.ensure_admin_control_message()

    @ensure_admin_control_message_loop.before_loop
    async def before_ensure_admin_control_message_loop(self) -> None:
        await self.wait_until_ready()
        # Check once immediately at startup instead of waiting for the first
        # 10-minute tick — this is what makes the button retroactively appear
        # in an admin channel that predates this feature, or reappear if the
        # message gets deleted, without needing /set-admin-channel re-run.
        await self.ensure_admin_control_message()

    @tasks.loop(seconds=5)
    async def watch_portal_queue(self) -> None:
        try:
            queue = await asyncio.to_thread(self.jellyfin.queue)
        except Exception:
            return
        current_ids = {entry["Id"] for entry in queue}
        previous_ids = self.announced_requests
        new_ids = current_ids - previous_ids
        resolved_ids = previous_ids - current_ids
        self.announced_requests = current_ids

        for request_id in resolved_ids:
            if request_id not in self.portal_messages:
                continue  # already cleaned up by whichever button actually resolved it
            entry = self.portal_entries.get(request_id) or {}
            device_name = entry.get("DeviceName") or "a device"
            device_ip = entry.get("RequestingIpAddress") or "unknown IP"
            device_domain = entry.get("ConnectionDomain") or "unknown domain"
            await self.post_log(f"Request for **{device_name}** ({device_ip}, {device_domain}) is no longer pending (expired, canceled, or connection lost).")
            await self.cleanup_portal_messages(request_id)

        if not new_ids:
            return

        cfg = self.store.snapshot()
        admin_channel_id = cfg["discord"].get("admin_channel_id")
        admin_users: list[dict[str, Any]] = []
        if admin_channel_id:
            try:
                admin_users = await asyncio.to_thread(self.jellyfin.users)
            except Exception:
                admin_users = []

        for entry in queue:
            if entry["Id"] not in new_ids:
                continue
            self.portal_entries[entry["Id"]] = entry
            tracked: list[tuple[int, int]] = []

            user_text = f"A device is waiting to sign in:\n\n{format_portal_telemetry(entry)}"
            for mapping in cfg["users"].values():
                channel_id = mapping.get("channel_id")
                if not channel_id:
                    continue
                channel = self.get_channel(int(channel_id))
                if channel is None:
                    continue
                view = UserPortalRequestView(self, entry["Id"], mapping["jellyfin_user_id"], mapping["jellyfin_username"])
                try:
                    message = await channel.send(
                        f"{user_text}\n\nApprove into **{mapping['jellyfin_username']}**?", view=view
                    )
                    tracked.append((message.channel.id, message.id))
                except discord.Forbidden:
                    LOG.warning("Missing permission to post the portal-queue notice in channel %s", channel_id)

            if admin_channel_id:
                channel = self.get_channel(int(admin_channel_id))
                if channel is not None:
                    admin_text = f"A device is waiting to sign in:\n\n{format_portal_telemetry(entry, include_ip=True)}"
                    admin_view = AdminPortalRequestView(self, entry["Id"], admin_users)
                    try:
                        message = await channel.send(admin_text, view=admin_view)
                        tracked.append((message.channel.id, message.id))
                    except discord.Forbidden:
                        LOG.warning("Missing permission to post the portal-queue notice in the admin channel %s", admin_channel_id)

            self.portal_messages[entry["Id"]] = tracked

    @watch_portal_queue.before_loop
    async def before_watch_portal_queue(self) -> None:
        await self.wait_until_ready()


def build_bot(store: BotConfigStore) -> SignOnBot:
    bot = SignOnBot(store)
    tree = bot.tree

    async def require_owner(interaction: discord.Interaction) -> bool:
        if not is_owner(interaction):
            await interaction.response.send_message("Only the server owner can do this.", ephemeral=True)
            return False
        return True

    async def require_admin_channel(interaction: discord.Interaction) -> bool:
        admin_channel_id = store.snapshot()["discord"].get("admin_channel_id")
        if not admin_channel_id or interaction.channel_id != int(admin_channel_id):
            await interaction.response.send_message("This command only works in the admin channel.", ephemeral=True)
            return False
        return True

    async def username_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice]:
        try:
            users = await asyncio.to_thread(bot.jellyfin.users)
        except Exception:
            return []
        needle = current.lower()
        return [
            app_commands.Choice(name=u.get("Name", "?"), value=u.get("Name", ""))
            for u in users
            if needle in u.get("Name", "").lower()
        ][:25]

    async def request_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice]:
        try:
            queue = await asyncio.to_thread(bot.jellyfin.queue)
        except Exception:
            return []
        return [
            app_commands.Choice(name=f"{q.get('DeviceName', '?')} · {q.get('Platform', '?')}"[:100], value=q["Id"])
            for q in queue
        ][:25]

    @tree.command(name="link-user", description="Link a Discord member to a Jellyfin account and create their private sign-on channel.")
    @app_commands.guild_only()
    @app_commands.describe(member="The Discord member to link", jellyfin_username="Their exact Jellyfin username")
    @app_commands.autocomplete(jellyfin_username=username_autocomplete)
    async def link_user(interaction: discord.Interaction, member: discord.Member, jellyfin_username: str) -> None:
        if not await require_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        assert guild is not None

        try:
            user = await asyncio.to_thread(bot.jellyfin.find_user, jellyfin_username)
        except Exception as exc:
            await interaction.followup.send(f"Couldn't reach Jellyfin: {exc}", ephemeral=True)
            return
        if user is None:
            await interaction.followup.send(f"No Jellyfin user named `{jellyfin_username}`.", ephemeral=True)
            return

        cfg = store.snapshot()
        category = None
        category_id = cfg["discord"].get("category_id")
        if category_id:
            found = guild.get_channel(int(category_id))
            if isinstance(found, discord.CategoryChannel):
                category = found

        existing = cfg["users"].get(str(member.id))
        channel = guild.get_channel(int(existing["channel_id"])) if existing and existing.get("channel_id") else None
        if not isinstance(channel, discord.TextChannel):
            try:
                channel = await create_user_channel(guild, member, category)
            except discord.Forbidden:
                await interaction.followup.send(
                    "I don't have permission to create a channel here — check that I have Manage Channels "
                    "and Manage Roles, and that the configured category (if any) doesn't have overwrites "
                    "excluding me.",
                    ephemeral=True,
                )
                return

        cfg["users"][str(member.id)] = {
            "jellyfin_user_id": user["Id"],
            "jellyfin_username": user["Name"],
            "channel_id": str(channel.id),
        }
        store.save(cfg)

        await channel.send(
            f"{member.mention} this channel is linked to the Jellyfin account **{user['Name']}**.\n"
            f"Use the button below (or `/signin code:XXXXXX`) for a device showing a Quick Connect code, "
            f"or `/signin-queue` to approve a device waiting in the Quick Sign-On queue.",
            view=QuickConnectButtonView(bot),
        )
        await interaction.followup.send(f"Linked {member.mention} to `{user['Name']}` in {channel.mention}.", ephemeral=True)

    @tree.command(name="unlink-user", description="Remove a Discord member's Jellyfin link.")
    @app_commands.guild_only()
    @app_commands.describe(member="The Discord member to unlink", delete_channel="Also delete their private channel (default: no)")
    async def unlink_user(interaction: discord.Interaction, member: discord.Member, delete_channel: bool = False) -> None:
        if not await require_owner(interaction):
            return
        cfg = store.snapshot()
        entry = cfg["users"].pop(str(member.id), None)
        store.save(cfg)
        if entry is None:
            await interaction.response.send_message(f"{member.mention} wasn't linked.", ephemeral=True)
            return

        note = ""
        channel = None
        if entry.get("channel_id") and interaction.guild is not None:
            channel = interaction.guild.get_channel(int(entry["channel_id"]))
        if isinstance(channel, discord.TextChannel):
            if delete_channel:
                await channel.delete(reason=f"Unlinked by {interaction.user}")
                note = " Channel deleted."
            else:
                await channel.set_permissions(member, overwrite=None)
                note = " Their access to the channel was revoked; the channel itself was kept."
        await interaction.response.send_message(f"Unlinked {member.mention}.{note}", ephemeral=True)

    @tree.command(name="set-admin-channel", description="Mark the current channel as the admin sign-on control room.")
    @app_commands.guild_only()
    async def set_admin_channel(interaction: discord.Interaction) -> None:
        if not await require_owner(interaction):
            return
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Run this inside a text channel.", ephemeral=True)
            return
        try:
            await channel.set_permissions(guild.default_role, view_channel=False)
            await channel.set_permissions(interaction.user, view_channel=True, send_messages=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I don't have permission to manage {channel.mention}'s access — it likely has permission "
                f"overwrites from before I was invited that don't grant me Manage Permissions here. "
                f"Try a brand-new channel instead, or add me to this channel's permissions with Manage "
                f"Roles/Manage Channels allowed.",
                ephemeral=True,
            )
            return
        cfg = store.snapshot()
        cfg["discord"]["admin_channel_id"] = str(channel.id)
        store.save(cfg)
        message = await channel.send(ADMIN_CONTROL_MESSAGE, view=QuickConnectButtonView(bot))
        cfg = store.snapshot()
        cfg["discord"]["admin_control_message_id"] = str(message.id)
        store.save(cfg)
        await interaction.response.send_message(f"{channel.mention} is now the admin channel.", ephemeral=True)

    @tree.command(name="set-log-channel", description="Mark the current channel as the admin-only sign-on activity log.")
    @app_commands.guild_only()
    async def set_log_channel(interaction: discord.Interaction) -> None:
        if not await require_owner(interaction):
            return
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Run this inside a text channel.", ephemeral=True)
            return
        try:
            await channel.set_permissions(guild.default_role, view_channel=False)
            await channel.set_permissions(interaction.user, view_channel=True, send_messages=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I don't have permission to manage {channel.mention}'s access — it likely has permission "
                f"overwrites from before I was invited that don't grant me Manage Permissions here. "
                f"Try a brand-new channel instead, or add me to this channel's permissions with Manage "
                f"Roles/Manage Channels allowed.",
                ephemeral=True,
            )
            return
        cfg = store.snapshot()
        cfg["discord"]["log_channel_id"] = str(channel.id)
        store.save(cfg)
        await channel.send(
            "This channel logs sign-on activity: who approved or denied a device, for which account, "
            "and requests that expired or were canceled with no response."
        )
        await interaction.response.send_message(f"{channel.mention} is now the activity log channel.", ephemeral=True)

    @tree.command(name="lockdown-server", description="Admin: hide every channel from @everyone by default until a member is linked.")
    @app_commands.guild_only()
    async def lockdown_server(interaction: discord.Interaction) -> None:
        if not await require_owner(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True)

        everyone = guild.default_role
        permissions = discord.Permissions(everyone.permissions.value)
        permissions.update(view_channel=False, create_instant_invite=False)
        await everyone.edit(permissions=permissions, reason=f"Server lockdown requested by {interaction.user}")

        updated = 0
        failed: list[str] = []
        for channel in guild.channels:
            try:
                await channel.set_permissions(everyone, view_channel=False, reason="Server lockdown")
                updated += 1
            except discord.Forbidden:
                failed.append(channel.name)

        note = f" Couldn't update: {', '.join(failed)}." if failed else ""
        await interaction.followup.send(
            f"Locked down {guild.name}: @everyone can no longer view channels or create invites by default, "
            f"and {updated} existing channel(s) now explicitly hide from @everyone.{note} "
            f"The server owner is unaffected (owners bypass all permission checks). "
            f"Run `/link-user` to give someone access to their own channel.",
            ephemeral=True,
        )

    @tree.command(name="signin", description="Sign in a device that's showing a Quick Connect code.")
    @app_commands.guild_only()
    @app_commands.describe(code="The 6-digit code shown on the device")
    async def signin(interaction: discord.Interaction, code: str) -> None:
        mapping = find_mapping_by_channel(store, interaction.channel_id)
        if mapping is None:
            await interaction.response.send_message("This channel isn't linked to a Jellyfin account.", ephemeral=True)
            return
        if interaction.user.id != mapping["discord_id"] and not is_owner(interaction):
            await interaction.response.send_message("This isn't your sign-on channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            ok = await asyncio.to_thread(bot.jellyfin.quick_connect_authorize, code.strip(), mapping["jellyfin_user_id"])
        except Exception as exc:
            await interaction.followup.send(f"Jellyfin rejected that: {exc}", ephemeral=True)
            return
        if ok:
            await interaction.followup.send(
                f"Signed in as **{mapping['jellyfin_username']}**. The device should connect within a few seconds.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("That code wasn't accepted. It may be wrong or expired — check the device screen.", ephemeral=True)

    @tree.command(name="signin-queue", description="Show devices waiting in the Quick Sign-On queue and approve one.")
    @app_commands.guild_only()
    async def signin_queue(interaction: discord.Interaction) -> None:
        mapping = find_mapping_by_channel(store, interaction.channel_id)
        if mapping is None:
            await interaction.response.send_message("This channel isn't linked to a Jellyfin account.", ephemeral=True)
            return
        if interaction.user.id != mapping["discord_id"] and not is_owner(interaction):
            await interaction.response.send_message("This isn't your sign-on channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            queue = await asyncio.to_thread(bot.jellyfin.queue)
        except Exception as exc:
            await interaction.followup.send(f"Couldn't reach Jellyfin: {exc}", ephemeral=True)
            return
        if not queue:
            await interaction.followup.send("No devices are waiting right now.", ephemeral=True)
            return
        view = QueuePickerView(bot, queue, mapping["jellyfin_user_id"], mapping["jellyfin_username"])
        await interaction.followup.send("Pick the device that's yours:", view=view, ephemeral=True)

    @tree.command(name="admin-signin", description="Admin: sign in a device to any Jellyfin user via a Quick Connect code.")
    @app_commands.guild_only()
    @app_commands.describe(jellyfin_username="Target Jellyfin username", code="The 6-digit code shown on the device")
    @app_commands.autocomplete(jellyfin_username=username_autocomplete)
    async def admin_signin(interaction: discord.Interaction, jellyfin_username: str, code: str) -> None:
        if not await require_owner(interaction) or not await require_admin_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            user = await asyncio.to_thread(bot.jellyfin.find_user, jellyfin_username)
            if user is None:
                await interaction.followup.send(f"No Jellyfin user named `{jellyfin_username}`.", ephemeral=True)
                return
            ok = await asyncio.to_thread(bot.jellyfin.quick_connect_authorize, code.strip(), user["Id"])
        except Exception as exc:
            await interaction.followup.send(f"Jellyfin rejected that: {exc}", ephemeral=True)
            return
        if ok:
            await interaction.followup.send(f"Signed the device in as **{user['Name']}**.", ephemeral=True)
        else:
            await interaction.followup.send("That code wasn't accepted.", ephemeral=True)

    @tree.command(name="admin-queue", description="Admin: list devices waiting in the Quick Sign-On queue and approve one for any user.")
    @app_commands.guild_only()
    @app_commands.describe(jellyfin_username="Jellyfin user to sign the picked device into")
    @app_commands.autocomplete(jellyfin_username=username_autocomplete)
    async def admin_queue(interaction: discord.Interaction, jellyfin_username: str) -> None:
        if not await require_owner(interaction) or not await require_admin_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            user = await asyncio.to_thread(bot.jellyfin.find_user, jellyfin_username)
            if user is None:
                await interaction.followup.send(f"No Jellyfin user named `{jellyfin_username}`.", ephemeral=True)
                return
            queue = await asyncio.to_thread(bot.jellyfin.queue)
        except Exception as exc:
            await interaction.followup.send(f"Couldn't reach Jellyfin: {exc}", ephemeral=True)
            return
        if not queue:
            await interaction.followup.send("No devices are waiting right now.", ephemeral=True)
            return
        view = QueuePickerView(bot, queue, user["Id"], user["Name"])
        await interaction.followup.send(f"Pick the device to sign in as **{user['Name']}**:", view=view, ephemeral=True)

    @tree.command(name="admin-deny", description="Admin: deny a device waiting in the Quick Sign-On queue.")
    @app_commands.guild_only()
    @app_commands.describe(request_id="The queue entry to deny")
    @app_commands.autocomplete(request_id=request_autocomplete)
    async def admin_deny(interaction: discord.Interaction, request_id: str) -> None:
        if not await require_owner(interaction) or not await require_admin_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        # Deny requires a prior Select under the same actor id; a throwaway id is
        # fine here since the request is removed from the queue immediately after
        # and no account ever gets signed in.
        actor_id = str(uuid.uuid4())
        entry = bot.portal_entries.get(request_id) or {}
        device_name = entry.get("DeviceName") or "a device"
        device_ip = entry.get("RequestingIpAddress") or "unknown IP"
        device_domain = entry.get("ConnectionDomain") or "unknown domain"
        try:
            await asyncio.to_thread(bot.jellyfin.select, request_id, actor_id)
            await asyncio.to_thread(bot.jellyfin.deny, request_id, actor_id)
        except Exception as exc:
            await interaction.followup.send(f"Couldn't deny that request: {exc}", ephemeral=True)
            return
        await interaction.followup.send("Denied.", ephemeral=True)
        await bot.post_log(f"{interaction.user.mention} denied **{device_name}** ({device_ip}, {device_domain}) (admin) — removed from the queue.")
        await bot.cleanup_portal_messages(request_id)

    @tree.error
    async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        LOG.exception("Command error", exc_info=error)
        message = f"Something went wrong: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    return bot


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "discord-bot.json"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = BotConfigStore.load(Path(args.config))
    token = store.snapshot()["discord"].get("bot_token")
    if not token:
        LOG.error("discord.bot_token is not set in %s; the bot will not start.", args.config)
        return

    bot = build_bot(store)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()

# Jellyfin Progress Sync Image

Custom Jellyfin container image with a built-in progress sync UI for selected shows and selected users.

This is not a separate sidecar container. The image extends the official `jellyfin/jellyfin` image, starts normal Jellyfin, and also starts the progress-sync UI inside the same container.

The UI uses the local Jellyfin API to list users and shows, then the sync loop writes selected episode progress into Jellyfin's SQLite `UserDatas` table.

## Safety Notes

- Stop Jellyfin or take a config backup before first use.
- The service makes timestamped copies of `jellyfin.db` before writes by default.
- It only advances users to the furthest configured progress. It does not roll users backward.
- It validates the `UserDatas` table before writing and refuses to run if required columns are missing.
- Do not expose port `8097` publicly unless you have changed the UI password and protected it behind your normal access controls.

## Local App Test

```bash
cd /home/dak/jellyfin-progress-sync
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
python3 app.py --config config.json
```

Open `http://localhost:8097`.

The default UI login from `config.example.json` is `admin` / `change-me`.

## Build The Custom Jellyfin Image

```bash
cd /home/dak/jellyfin-progress-sync
docker build -t jellyfin-progress-sync:local .
```

The base image is your own compiled fork, `ghcr.io/pacoonrox/jellyfin-server-progress-sync`
(built by `jellyfin-progress-sync-packaging`'s `publish-progress-sync-image.yml`
from the `jellyfin-server-progress-sync` / `jellyfin-web-progress-sync` commits
that repo's submodules are pinned to), not stock `jellyfin/jellyfin`. Push to
that packaging repo's `main`/`master` first so the base image exists before
building here.

To pin a specific server build instead of tracking `latest`, use one of the
`sha-XXXXXXX` tags that workflow also publishes:

```bash
docker build --build-arg JELLYFIN_BASE_TAG=sha-abc1234 -t jellyfin-progress-sync:abc1234 .
```

To build against stock upstream Jellyfin instead (e.g. for isolating whether
a bug comes from your server fork or from this layer), override the base
image entirely:

```bash
docker build \
  --build-arg JELLYFIN_BASE_IMAGE=jellyfin/jellyfin \
  --build-arg JELLYFIN_BASE_TAG=10.10.7 \
  -t jellyfin-progress-sync:stock-10.10.7 .
```

## Publish To GitHub Container Registry

Push this project to a GitHub repo. The workflow at `.github/workflows/publish-image.yml` publishes:

```text
ghcr.io/YOUR_GITHUB_USERNAME/YOUR_REPO:latest
ghcr.io/YOUR_GITHUB_USERNAME/YOUR_REPO:main
ghcr.io/YOUR_GITHUB_USERNAME/YOUR_REPO:sha-...
```

If your repo is named `jellyfin-progress-sync`, your compose image will usually be:

```text
ghcr.io/YOUR_GITHUB_USERNAME/jellyfin-progress-sync:latest
```

For a private package, log in on the Docker host before pulling:

```bash
echo YOUR_GITHUB_TOKEN | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

## Existing Compose Stack

On the remote server that currently runs Jellyfin, keep your current mounted paths:

```yaml
- /mnt/pool/nas/jellyfin/config:/config
- /mnt/pool/nas/jellyfin/cache:/cache
- /mnt/pool/nas/Plex:/Fake
- /mnt/pool/nas/Media:/Media
```

Those mounts are what make the custom image use your existing database, metadata, cache, media paths, users, and playback history. There is no database migration.

In your existing Jellyfin service, replace:

```yaml
image: jellyfin/jellyfin
```

with:

```yaml
image: ghcr.io/YOUR_GITHUB_USERNAME/jellyfin-progress-sync:latest
```

Then add the UI port and optional config path env var:

```yaml
ports:
  - "8096:8096"
  - "8097:8097"

environment:
  - JELLYFIN_PublishedServerUrl=https://drdave.uk
  - LIBVA_DRIVER_NAME=radeonsi
  - PROGRESS_SYNC_CONFIG=/config/progress-sync.json
```

Your existing `/mnt/pool/nas/jellyfin/config:/config` mount is reused. The progress sync config will live at:

```text
/mnt/pool/nas/jellyfin/config/progress-sync.json
```

Inside the container, `database_path` should be:

```json
"/config/data/jellyfin.db"
```

An example full compose file without your Cloudflare tunnel tokens is in `docker-compose.custom-jellyfin.yml`.

## Remote Server Deploy Flow

1. Push `/home/dak/jellyfin-progress-sync` to GitHub.
2. Let GitHub Actions publish the image to GHCR.
3. On the Jellyfin server, log in to GHCR if the package is private.
4. Change only the Jellyfin image line in your compose file, add port `8097`, and keep the existing volume/device/user settings.
5. Run:

```bash
docker compose pull jellyfin
docker compose up -d jellyfin
```

Then open the progress UI on the remote server at:

```text
http://SERVER_IP_OR_HOSTNAME:8097
```

## Jellyfin API Key

Create one in Jellyfin dashboard:

`Dashboard -> Advanced -> API Keys`

Use an admin API key so the UI can list users and shows.

## Discord Security Alerts And Cloudflare Bans

The image also starts a passive security monitor. It reads Jellyfin's
`ActivityLogs` table from `/config/data/jellyfin.db`, sends Discord webhook
alerts for failed logins and user lockouts, and can add abusive IPs to a
Cloudflare IP list after a threshold is reached.

This does not change Jellyfin's authentication flow, so normal web, mobile, and
TV clients continue to log in through Jellyfin.

Create the runtime config on the host:

```bash
cp security-alerts.example.json /mnt/pool/nas/jellyfin/config/security-alerts.json
```

Set at least:

```json
{
  "discord": {
    "webhook_url": "https://discord.com/api/webhooks/..."
  },
  "thresholds": {
    "failures": 5,
    "window_seconds": 600
  },
  "ban": {
    "enabled": false
  }
}
```

For Cloudflare Tunnel deployments, host firewall bans usually do not help
because traffic reaches Jellyfin through `cloudflared`. To automate bans at the
edge:

1. Create a Cloudflare custom IP list, for example `jellyfin_banned_ips`.
2. Create a WAF custom rule for your Jellyfin hostname:

```text
(http.host eq "jellyfin.example.com" and ip.src in $jellyfin_banned_ips)
```

3. Set the rule action to `block` or `managed_challenge`.
4. Create a Cloudflare API token with permission to edit account rules lists.
5. Put the account ID, list ID, and token in `security-alerts.json`, or provide
   them as environment variables.

```json
{
  "ban": {
    "enabled": true,
    "action": "cloudflare",
    "cloudflare": {
      "api_token": "cloudflare-token",
      "account_id": "cloudflare-account-id",
      "list_id": "cloudflare-ip-list-id"
    }
  }
}
```

Environment variable alternatives:

```yaml
environment:
  - SECURITY_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
  - SECURITY_CLOUDFLARE_API_TOKEN=...
  - SECURITY_CLOUDFLARE_ACCOUNT_ID=...
  - SECURITY_CLOUDFLARE_LIST_ID=...
```

Cloudflare list entries are persistent. The `duration_seconds` setting is used
by the monitor as a local cooldown to avoid repeated ban alerts for the same IP;
remove entries from the Cloudflare list when you want to unban them.

## Discord Sign-On Bot

A Discord bot (`discord_bot.py`) that lets people sign devices into Jellyfin
from Discord instead of the Jellyfin web UI. It drives both sign-on
mechanisms:

- **Quick Connect** — a device shows a 6-digit code; a Discord command
  authorizes it.
- **Quick Sign-On queue** (the shared portal) — the bot lists devices
  currently waiting and lets you pick one to approve, no code required.

The bot authenticates to Jellyfin with a single admin API key and never
stores anyone's Jellyfin password. Every sign-on call explicitly names the
target user, so a regular user's channel can only ever sign in their own
linked account, while the admin channel can target anyone.

By default, nobody but the server owner can see any channel, invite anyone,
or (as a consequence — Discord has no separate "hide member list" toggle;
the member list is just a byproduct of channel visibility) see who else is
in the server. Running `/link-user` grants that one person visibility into
exactly their own channel and nothing else. All of this is enforced with
real Discord permissions, not just bot-side checks, and the server owner is
always exempt (Discord guild owners bypass every permission check).

Every command is registered guild-only (`app_commands.guild_only()`), which
Discord enforces on its own servers, not just in the bot's code — the
commands simply do not exist in a DM with the bot. Combined with the bot
never reading message content, this means the bot cannot meaningfully be
DMed. The one thing code can't control: in the Developer Portal, under
**Installation**, make sure only **Guild Install** is enabled and **User
Install** is off — a user-installed app's commands follow the user into
DMs regardless of the guild-only flag.

Approving from the Quick Sign-On queue (unlike Quick Connect) requires the
DeviceApproval admin-override already applied in this repo's
`jellyfin-server-progress-sync` fork (an API key is allowed to approve, and
may target any user, not just itself — see
`docs/device-approval-and-trusted-devices.md`). Against a stock/unpatched
Jellyfin server, only the Quick Connect commands will work.

### Setup

1. In the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application, add a Bot user, and copy its token.
2. Under **OAuth2 → URL Generator**, select the `bot` and
   `applications.commands` scopes, and under bot permissions select
   **Manage Channels**, **Manage Roles**, **View Channels**, and
   **Send Messages**. Use the generated URL to invite the bot to your server.
3. Copy `discord-bot.example.json` to
   `/mnt/pool/nas/jellyfin/config/discord-bot.json` (same pattern as
   `progress-sync.json` and `security-alerts.json` above) and fill in:

```json
{
  "discord": {
    "bot_token": "...",
    "guild_id": "your-discord-server-id"
  },
  "jellyfin": {
    "base_url": "http://127.0.0.1:8096",
    "api_key": "a-jellyfin-admin-api-key"
  }
}
```

4. If you use a config path other than the default shown above, set
   `DISCORD_BOT_CONFIG=/config/discord-bot.json` as an environment variable.
   The container starts the bot automatically alongside Jellyfin, the
   progress sync UI, and the security monitor.
5. In Discord, as the **server owner**, run `/lockdown-server` once to hide
   every channel from `@everyone` by default, run `/set-admin-channel`
   inside whichever channel should be the admin control room, then run
   `/link-user` once per person to create their private sign-on channel.

### Commands

Owner-only, any channel:

- `/link-user member:@someone jellyfin_username:name` — creates or reuses a
  private channel visible only to that member and links it to a Jellyfin
  account.
- `/unlink-user member:@someone [delete_channel]` — removes the link;
  optionally deletes the channel.
- `/set-admin-channel` — marks the current channel as the admin channel.
- `/lockdown-server` — denies `@everyone` the ability to view channels or
  create invites, server-wide, and forces the same on every existing
  channel. Run this once, before linking anyone.

Inside a linked user's own private channel:

- `/signin code:XXXXXX` — Quick Connect.
- `/signin-queue` — pick a waiting device from the Quick Sign-On queue.

Admin channel only, owner-only:

- `/admin-signin jellyfin_username:name code:XXXXXX` — Quick Connect into
  any account.
- `/admin-queue jellyfin_username:name` — pick a waiting device to sign into
  any account.
- `/admin-deny request_id:...` — remove a device from the queue.

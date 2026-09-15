# Jellyfin Progress Sync Image

Custom Jellyfin container image with a built-in progress sync UI for selected shows and selected users.

This is not a separate sidecar container. The image extends the official `jellyfin/jellyfin` image, starts normal Jellyfin, and also starts the progress-sync UI inside the same container.

The UI uses the local Jellyfin API to list users and shows, then the sync loop writes selected episode progress into Jellyfin's SQLite `UserDatas` table.

## Community Ratings, Comments, And Chat

The image includes an optional Community feature. It adds a Community button after
the visible library buttons in Jellyfin Web, a right-click `Rate & discuss` action
on media cards, per-user 1–10 ratings, comments on movies/shows/episodes, server
rankings, and a small server chat room.

Community data is stored in a separate SQLite database and never modifies
Jellyfin's `jellyfin.db` or `UserDatas` table:

```text
/config/data/community.db
```

The Community page receives the current Jellyfin session token in the URL
fragment, validates it against Jellyfin, and then removes it from the address bar.
No Jellyfin password or admin API key is stored in community records. The
`community_enabled` setting can be set to `false` in `progress-sync.json` to
disable the feature.

The image exposes the Community service on port `8097`, which must be reachable
from users' browsers. If Jellyfin is behind a reverse proxy, proxy this port as
well (for example, `community.example.com` -> container port `8097`) and adjust
the generated web plugin URL if your deployment does not expose port 8097 on the
same hostname.

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

To pin the Jellyfin base version instead of tracking `latest`:

```bash
docker build --build-arg JELLYFIN_BASE_TAG=10.10.7 -t jellyfin-progress-sync:10.10.7 .
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

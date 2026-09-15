#!/usr/bin/env python3
"""Install the image-only web plugin into upstream Jellyfin Web assets."""

import json
from pathlib import Path


WEB_ROOT = Path("/jellyfin/jellyfin-web")
PLUGIN_SOURCE = Path("/opt/progress-sync/community/plugin.js")
PLUGIN_PATH = WEB_ROOT / "community" / "plugin.js"
MARKER = 'community/plugin.js'


def main() -> None:
    if not WEB_ROOT.exists():
        raise SystemExit(f"Jellyfin Web directory not found: {WEB_ROOT}")
    PLUGIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLUGIN_PATH.write_bytes(PLUGIN_SOURCE.read_bytes())

    config_path = WEB_ROOT / "config.json"
    if not config_path.exists():
        raise SystemExit(f"Jellyfin Web config not found: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    plugins = config.setdefault("plugins", [])
    if "JellyfinCommunity" not in plugins:
        plugins.append("JellyfinCommunity")
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    index_paths = sorted(WEB_ROOT.glob("*.html"))
    if not index_paths:
        raise SystemExit("no Jellyfin Web HTML entry point found")
    index_path = index_paths[0]
    html = index_path.read_text(encoding="utf-8")
    script_tag = '<script src="community/plugin.js"></script>'
    if MARKER not in html:
        if "</head>" not in html:
            raise SystemExit(f"could not find </head> in {index_path}")
        html = html.replace("</head>", f"    {script_tag}\n</head>", 1)
        index_path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()

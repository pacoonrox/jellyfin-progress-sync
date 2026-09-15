/* Image-installed Jellyfin Web plugin. It deliberately uses DOM/event hooks so
 * the Docker image can stay on upstream Jellyfin Web without a source fork. */
(function installCommunityPlugin() {
  const findText = (element) => (element?.textContent || "").replace(/\s+/g, " ").trim();

  class CommunityPlugin {
    name = "Community";
    id = "community";
    type = "community";

    constructor({ ServerConnections }) {
      this.ServerConnections = ServerConnections;
      this.menu = null;
      this.observer = new MutationObserver(() => {
        this.addNavigationButton();
        this.bindContextMenus();
      });
      this.start();
    }

    start() {
      this.addStyles();
      this.addNavigationButton();
      this.bindContextMenus();
      this.observer.observe(document.body, { childList: true, subtree: true });
    }

    communityUrl(itemId = "") {
      const apiClient = this.ServerConnections.currentApiClient();
      if (!apiClient) return null;
      const url = new URL(window.location.origin);
      url.port = "8097";
      url.pathname = "/community";
      const fragment = new URLSearchParams({ token: apiClient.accessToken() });
      if (itemId) fragment.set("item", itemId);
      return `${url.href}#${fragment}`;
    }

    addNavigationButton() {
      if (document.querySelector(".jellyfin-community-nav")) return;
      const candidates = [...document.querySelectorAll(".MuiStack-root button, .MuiStack-root a, .headerTabs button, .headerTabs a")];
      const showsButton = candidates.find((element) => findText(element) === "Shows");
      if (!showsButton) return;
      const href = this.communityUrl();
      if (!href) return;
      const button = document.createElement("a");
      button.className = "jellyfin-community-nav MuiButtonBase-root MuiButton-root MuiButton-text";
      button.href = href;
      button.innerHTML = '<span class="material-icons" aria-hidden="true">forum</span><span>Community</span>';
      showsButton.insertAdjacentElement("afterend", button);
    }

    bindContextMenus() {
      if (this.contextBound) return;
      this.contextBound = true;
      document.addEventListener("contextmenu", (event) => {
        const target = event.target.closest?.("[data-id]");
        const itemId = target?.getAttribute("data-id");
        if (!itemId || target.closest(".jellyfin-community-menu")) return;
        event.preventDefault();
        this.showContextMenu(event.clientX, event.clientY, itemId);
      });
    }

    showContextMenu(x, y, itemId) {
      this.menu?.remove();
      const href = this.communityUrl(itemId);
      if (!href) return;
      const menu = document.createElement("div");
      menu.className = "jellyfin-community-menu";
      menu.style.left = `${Math.min(x, window.innerWidth - 230)}px`;
      menu.style.top = `${Math.min(y, window.innerHeight - 70)}px`;
      menu.innerHTML = '<button type="button"><span class="material-icons">star_rate</span> Rate & discuss</button>';
      menu.querySelector("button").addEventListener("click", () => { window.location.href = href; });
      document.body.appendChild(menu);
      this.menu = menu;
      const close = (event) => {
        if (!menu.contains(event.target)) {
          menu.remove();
          document.removeEventListener("click", close, true);
        }
      };
      setTimeout(() => document.addEventListener("click", close, true), 0);
    }

    addStyles() {
      if (document.getElementById("jellyfin-community-plugin-styles")) return;
      const style = document.createElement("style");
      style.id = "jellyfin-community-plugin-styles";
      style.textContent = `
        .jellyfin-community-nav { display:inline-flex; align-items:center; gap:.35rem; min-height:36px; padding:6px 10px; color:inherit; text-decoration:none; border-radius:4px; }
        .jellyfin-community-nav:hover { background:rgba(255,255,255,.08); }
        .jellyfin-community-nav .material-icons { font-size:20px; }
        .jellyfin-community-menu { position:fixed; z-index:2147483647; min-width:210px; padding:5px; background:#25263d; border:1px solid rgba(255,255,255,.16); border-radius:5px; box-shadow:0 8px 30px rgba(0,0,0,.5); }
        .jellyfin-community-menu button { display:flex; align-items:center; gap:9px; width:100%; padding:10px 12px; color:#fff; background:transparent; border:0; border-radius:3px; text-align:left; cursor:pointer; font:inherit; }
        .jellyfin-community-menu button:hover { background:rgba(196,164,255,.18); }
        .jellyfin-community-menu .material-icons { font-size:19px; }
      `;
      document.head.appendChild(style);
    }
  }

  window.JellyfinCommunity = async () => CommunityPlugin;
})();

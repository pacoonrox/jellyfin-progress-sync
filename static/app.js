const state = {
  config: null,
  users: [],
  series: [],
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || res.statusText);
  return data;
}

function readSettings() {
  const form = $("settings");
  const data = new FormData(form);
  return {
    ...state.config,
    jellyfin_url: data.get("jellyfin_url").trim(),
    api_key: data.get("api_key").trim(),
    ui_username: data.get("ui_username").trim() || "admin",
    ui_password: data.get("ui_password").trim(),
    database_path: data.get("database_path").trim(),
    listen_host: state.config.listen_host || "0.0.0.0",
    listen_port: Number(state.config.listen_port || 8097),
    sync_interval_seconds: Number(data.get("sync_interval_seconds") || 60),
    make_backups: form.elements.make_backups.checked,
    groups: state.config.groups || [],
  };
}

function writeSettings(config) {
  const form = $("settings");
  form.elements.jellyfin_url.value = config.jellyfin_url || "";
  form.elements.api_key.value = config.api_key || "";
  form.elements.ui_username.value = config.ui_username || "admin";
  form.elements.ui_password.value = config.ui_password || "";
  form.elements.database_path.value = config.database_path || "";
  form.elements.sync_interval_seconds.value = config.sync_interval_seconds || 60;
  form.elements.make_backups.checked = config.make_backups !== false;
}

function selected(container) {
  return [...container.querySelectorAll("input:checked")].map((input) => input.value);
}

function renderRows(container, items, labelFn) {
  container.innerHTML = "";
  if (!items.length) {
    container.innerHTML = `<div class="meta">Nothing loaded.</div>`;
    return;
  }
  for (const item of items) {
    const label = document.createElement("label");
    label.className = "row";
    label.innerHTML = `<input type="checkbox" value="${item.Id}"><span></span>`;
    label.querySelector("span").textContent = labelFn(item);
    container.appendChild(label);
  }
}

function renderGroups() {
  const box = $("groups");
  const groups = state.config.groups || [];
  box.innerHTML = "";
  if (!groups.length) {
    box.innerHTML = `<div class="meta">No groups yet.</div>`;
    return;
  }
  groups.forEach((group, index) => {
    const users = group.user_ids?.length || 0;
    const series = group.series_ids?.length || 0;
    const el = document.createElement("div");
    el.className = "group";
    el.innerHTML = `
      <div class="groupTop">
        <div>
          <strong></strong>
          <div class="meta">${users} users, ${series} shows</div>
        </div>
        <div class="actions">
          <label class="check"><input type="checkbox" ${group.enabled !== false ? "checked" : ""}> Enabled</label>
          <button type="button" class="danger">Delete</button>
        </div>
      </div>
    `;
    el.querySelector("strong").textContent = group.name || "Unnamed group";
    el.querySelector("input").addEventListener("change", (event) => {
      state.config.groups[index].enabled = event.target.checked;
      renderGroups();
    });
    el.querySelector("button").addEventListener("click", () => {
      state.config.groups.splice(index, 1);
      renderGroups();
    });
    box.appendChild(el);
  });
}

async function loadConfig() {
  state.config = await api("/api/config");
  writeSettings(state.config);
  renderGroups();
  $("statusLine").textContent = "Configuration loaded.";
}

async function loadChoices() {
  await saveConfig();
  $("statusLine").textContent = "Loading users and shows...";
  const users = await api("/api/users");
  state.users = users;
  renderRows($("users"), users, (user) => user.Name || user.Id);

  const firstUser = users[0]?.Id || "";
  const search = $("seriesSearch").value.trim();
  const series = await api(`/api/series?userId=${encodeURIComponent(firstUser)}&search=${encodeURIComponent(search)}`);
  state.series = series;
  renderRows($("series"), series, (item) => item.Name || item.Id);
  $("statusLine").textContent = `Loaded ${users.length} users and ${series.length} shows.`;
}

async function saveConfig() {
  state.config = readSettings();
  await api("/api/config", { method: "POST", body: JSON.stringify(state.config) });
  $("statusLine").textContent = "Saved.";
}

async function syncNow() {
  await saveConfig();
  $("statusLine").textContent = "Sync running...";
  const result = await api("/api/sync", { method: "POST", body: "{}" });
  $("lastRun").textContent = JSON.stringify(result, null, 2);
  $("statusLine").textContent = `Sync complete. ${result.changed?.length || 0} updates.`;
}

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    $("lastRun").textContent = JSON.stringify(status, null, 2);
  } catch {
  }
}

$("saveBtn").addEventListener("click", () => saveConfig().catch((err) => $("statusLine").textContent = err.message));
$("loadBtn").addEventListener("click", () => loadChoices().catch((err) => $("statusLine").textContent = err.message));
$("syncBtn").addEventListener("click", () => syncNow().catch((err) => $("statusLine").textContent = err.message));
$("schemaBtn").addEventListener("click", async () => {
  try {
    await saveConfig();
    const result = await api("/api/schema");
    $("lastRun").textContent = JSON.stringify(result, null, 2);
    $("statusLine").textContent = "Database schema looks usable.";
  } catch (err) {
    $("statusLine").textContent = err.message;
  }
});

$("seriesSearch").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    loadChoices().catch((err) => $("statusLine").textContent = err.message);
  }
});

$("addGroupBtn").addEventListener("click", () => {
  const user_ids = selected($("users"));
  const series_ids = selected($("series"));
  const name = $("groupName").value.trim() || "Shared progress";
  if (user_ids.length < 2) {
    $("statusLine").textContent = "Pick at least two users.";
    return;
  }
  if (!series_ids.length) {
    $("statusLine").textContent = "Pick at least one show.";
    return;
  }
  state.config.groups = state.config.groups || [];
  state.config.groups.push({ id: crypto.randomUUID(), name, enabled: true, user_ids, series_ids });
  renderGroups();
  $("statusLine").textContent = "Group added. Save when ready.";
});

loadConfig()
  .then(refreshStatus)
  .catch((err) => $("statusLine").textContent = err.message);

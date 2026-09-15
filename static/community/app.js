const state = {
  token: sessionStorage.getItem("jellyfin-community-token") || "",
  itemId: new URLSearchParams(location.hash.slice(1)).get("item") || "",
  chatTimer: null,
};

const $ = (id) => document.getElementById(id);

function showNotice(message) {
  const notice = $("notice");
  notice.textContent = message;
  notice.hidden = false;
}

function clearNotice() {
  $("notice").hidden = true;
}

function api(path, options = {}) {
  return fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-Emby-Token": state.token, ...(options.headers || {}) },
  }).then(async (response) => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || response.statusText);
    return data;
  });
}

function formatDate(value) {
  return new Date(value).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
}

function mediaLabel(item) {
  return item.type === "Series" ? "Show" : item.type;
}

function renderRankings(rows) {
  const list = $("rankingList");
  if (!rows.length) {
    list.innerHTML = '<div class="empty">No ratings or discussions yet. Be the first to start one.</div>';
    return;
  }
  list.innerHTML = rows.map((row, index) => {
    const item = row.item;
    const rating = row.rating.average ? `${row.rating.average.toFixed(1)} / 10` : "Not rated";
    const count = row.rating.count ? `${row.rating.count} rating${row.rating.count === 1 ? "" : "s"}` : "No ratings";
    return `<button class="ranking" data-item-id="${item.id}">
      <span class="rank">${index + 1}</span>
      <span><span class="mediaName">${escapeHtml(item.name)}</span><span class="mediaMeta">${mediaLabel(item)}${item.year ? ` · ${item.year}` : ""} · ${row.commentCount} comment${row.commentCount === 1 ? "" : "s"}</span></span>
      <span class="rating">★ ${rating}<small><br>${count}</small></span>
    </button>`;
  }).join("");
  list.querySelectorAll("[data-item-id]").forEach((button) => button.addEventListener("click", () => openItem(button.dataset.itemId)));
}

function renderChat(messages) {
  const list = $("chatList");
  if (!messages.length) {
    list.innerHTML = '<div class="empty">No messages yet.</div>';
    return;
  }
  list.innerHTML = messages.map(message => `<article class="chatMessage"><header><strong>${escapeHtml(message.userName)}</strong><time>${formatDate(message.createdAt)}</time></header><p>${escapeHtml(message.body)}</p></article>`).join("");
  list.scrollTop = list.scrollHeight;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
}

async function loadRankings() {
  const params = new URLSearchParams({ sort: $("sortSelect").value, limit: "30" });
  if ($("typeSelect").value) params.set("type", $("typeSelect").value);
  renderRankings(await api(`/api/community/feed?${params}`));
}

async function loadChat() {
  renderChat(await api("/api/community/chat"));
}

function ratingButtons(item) {
  return Array.from({ length: 10 }, (_, index) => {
    const rating = index + 1;
    return `<button type="button" data-rating="${rating}" class="${item.rating.mine === rating ? "selected" : ""}">${rating}</button>`;
  }).join("");
}

function renderItem(data) {
  const panel = $("itemPanel");
  const item = data.item;
  panel.innerHTML = `<div class="itemPanelHead"><div><h2>${escapeHtml(item.name)}</h2><div class="mediaMeta">${mediaLabel(item)}${item.year ? ` · ${item.year}` : ""}</div></div><button class="closeItem" type="button">Close</button></div>
    <div class="itemContent"><div class="itemRating"><strong>Community rating</strong><div class="rating">★ ${data.rating.average ? data.rating.average.toFixed(1) : "—"} <small>(${data.rating.count})</small></div><div class="mediaMeta">Your rating: ${data.rating.mine || "not set"}</div><div class="ratingButtons">${ratingButtons(data)}</div></div>
    <div class="comments"><strong>Discussion</strong><div id="commentsList">${renderComments(data.comments)}</div><form class="commentForm"><input name="body" maxlength="2000" placeholder="Leave a comment…" required><button type="submit">Post</button></form></div></div>`;
  panel.hidden = false;
  panel.querySelector(".closeItem").addEventListener("click", closeItem);
  panel.querySelectorAll("[data-rating]").forEach(button => button.addEventListener("click", () => saveRating(item.id, Number(button.dataset.rating))));
  panel.querySelector(".commentForm").addEventListener("submit", event => postComment(event, item.id));
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderComments(comments) {
  if (!comments.length) return '<div class="empty">No comments yet.</div>';
  return comments.map(comment => `<article class="comment"><header><strong>${escapeHtml(comment.userName)}</strong><time>${formatDate(comment.createdAt)}</time></header><p>${escapeHtml(comment.body)}</p></article>`).join("");
}

async function openItem(itemId) {
  state.itemId = itemId;
  history.replaceState(null, "", `${location.pathname}#item=${encodeURIComponent(itemId)}`);
  try {
    clearNotice();
    renderItem(await api(`/api/community/item?itemId=${encodeURIComponent(itemId)}`));
  } catch (error) {
    showNotice(error.message);
  }
}

function closeItem() {
  state.itemId = "";
  history.replaceState(null, "", location.pathname);
  $("itemPanel").hidden = true;
}

async function saveRating(itemId, rating) {
  try {
    clearNotice();
    renderItem(await api(`/api/community/item/${itemId}/rating`, { method: "PUT", body: JSON.stringify({ rating }) }));
    await loadRankings();
  } catch (error) { showNotice(error.message); }
}

async function postComment(event, itemId) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    clearNotice();
    await api(`/api/community/item/${itemId}/comments`, { method: "POST", body: JSON.stringify({ body: form.body.value }) });
    form.reset();
    await openItem(itemId);
    await loadRankings();
  } catch (error) { showNotice(error.message); }
}

$("chatForm").addEventListener("submit", async event => {
  event.preventDefault();
  const input = $("chatInput");
  try {
    clearNotice();
    await api("/api/community/chat", { method: "POST", body: JSON.stringify({ body: input.value }) });
    input.value = "";
    await loadChat();
  } catch (error) { showNotice(error.message); }
});

$("sortSelect").addEventListener("change", () => loadRankings().catch(error => showNotice(error.message)));
$("typeSelect").addEventListener("change", () => loadRankings().catch(error => showNotice(error.message)));

function consumeLaunchFragment() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get("token");
  const item = fragment.get("item");
  if (token) {
    state.token = token;
    sessionStorage.setItem("jellyfin-community-token", token);
  }
  if (item) state.itemId = item;
  if (token || item) history.replaceState(null, "", `${location.pathname}${item ? `#item=${encodeURIComponent(item)}` : ""}`);
}

async function start() {
  consumeLaunchFragment();
  $("backLink").href = document.referrer && new URL(document.referrer).origin === location.origin ? document.referrer : "/";
  try {
    await api("/api/community/health");
    await Promise.all([loadRankings(), loadChat()]);
    if (state.itemId) await openItem(state.itemId);
    state.chatTimer = window.setInterval(() => loadChat().catch(() => {}), 5000);
  } catch (error) {
    showNotice(`${error.message}. Open Community from inside Jellyfin so your session can be shared.`);
  }
}

start();

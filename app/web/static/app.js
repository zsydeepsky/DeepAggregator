const $ = (id) => document.getElementById(id);
const state = {
  offset: 0,
  searching: false,
  workspace: "",
  feed: { sourceIds: [], types: [], q: "", mode: "semantic", unread: false, starred: false, bookmarked: false },
};
let searchAbort = null;

function setFeed(patch) {
  Object.assign(state.feed, patch);
  state.offset = 0;
  syncControls();
  loadItems();
}

function syncControls() {
  const f = state.feed;
  $("searchBox").value = f.q;
  $("mode").value = f.mode;
  $("fUnread").classList.toggle("active", f.unread);
  $("fStarred").classList.toggle("active", f.starred);
  $("fBookmarked").classList.toggle("active", f.bookmarked);
  document.querySelectorAll("#sourceList .source").forEach((li) => {
    li.classList.toggle("active", f.sourceIds.includes(li.dataset.sid));
  });
  document.querySelectorAll("#sourceList .src-group").forEach((g) => {
    g.classList.toggle("active", f.types.includes(g.dataset.type));
  });
  updateSrcFilterBtn();
  // ✕ 按钮：有过滤条件时高亮，默认态时弱化
  const hasFilters = !!(
    f.q ||
    f.sourceIds.length ||
    f.types.length ||
    f.unread ||
    f.starred ||
    f.bookmarked ||
    f.mode !== "hybrid"
  );
  $("clearFiltersBtn").classList.toggle("active", hasFilters);
}

const scoreColor = (s) =>
  `hsl(${Math.round(Math.max(0, Math.min(1, s)) * 120)}, 72%, 42%)`;

function setSearching(on) {
  ["searchBox", "mode", "fUnread", "fStarred", "fBookmarked", "moreBtn"].forEach(
    (id) => ($(id).disabled = on)
  );
  $("searchOverlay").classList.toggle("hidden", !on);
}

function flash(msg) {
  const n = $("notice");
  n.textContent = msg;
  n.style.opacity = "1";
  setTimeout(() => {
    n.style.opacity = "0";
  }, 2500);
}

function token() {
  return localStorage.getItem("da_token") || "";
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (token()) headers.Authorization = "Bearer " + token();
  const resp = await fetch("/api" + path, { ...opts, headers });
  if (resp.status === 401) {
    const t = prompt("请输入访问令牌（DA_AUTH_TOKEN）");
    if (t !== null) {
      localStorage.setItem("da_token", t);
      return api(path, opts);
    }
    throw new Error("unauthorized");
  }
  if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
  return resp.status === 204 ? null : resp.json();
}

const esc = (s) =>
  (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

// 书签图标（内联 SVG，颜色随状态由 CSS 控制灰描边/彩色填充）
const BM_ICON =
  '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path class="bm-path" d="M6 3h12a1 1 0 0 1 1 1v17l-7-4.5L5 21V4a1 1 0 0 1 1-1z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>';
const STAR_ICON =
  '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path class="star-path" d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>';

// ---- 媒体载体：常驻隐形元素，POP 即挂载到载体并弹浏览器原生画中画 ----
let carrierYtPlayer = null;

const mediaCarrier = {
  mode: "", // 'video' | 'youtube'
  title: "",
  videoId: "", // youtube 恢复播放用
  lastTime: 0, // 最近记录的播放位置（秒）
  pipOpen: false, // youtube 文档级画中画窗口是否存活

  _prepare() {
    $("mediaIndicator").classList.remove("hidden");
  },

  _showIndicator() {
    const b = $("mediaIndicator");
    b.classList.remove("hidden");
    b.title = `${this.title} · 正在画中画播放，点击重新弹出`;
  },

  // 画中画被顶掉/关闭：暂停载体并点亮顶栏按钮（点击可从原进度恢复）
  _lightUp() {
    const b = $("mediaIndicator");
    b.classList.add("attention");
    b.title = `${this.title} · 已暂停于 ${fmtDuration(this.lastTime)}，点击继续播放`;
  },

  _clearAttention() {
    $("mediaIndicator").classList.remove("attention");
  },

  // 画中画不可用时的兜底：载体转为右下角可见迷你播放器（绝不隐形出声）
  _showVisibleFallback() {
    $("mediaCarrier").classList.add("visible");
    this._showIndicator();
  },

  async _popoutVideo() {
    const v = $("carrierVideo");
    if (v.paused) await v.play().catch(() => {});
    try {
      if (v.readyState === 0) {
        await new Promise((resolve, reject) => {
          v.addEventListener("loadedmetadata", resolve, { once: true });
          setTimeout(reject, 3000);
        });
      }
      await v.requestPictureInPicture();
      this._clearAttention();
      return true;
    } catch (err) {
      this._showVisibleFallback();
      return false;
    }
  },

  async attachVideoAndPop(src, time, title) {
    this.mode = "video";
    this.title = title || "视频";
    this._prepare();
    this._clearAttention();
    $("carrierYtWrap").innerHTML = "";
    const v = $("carrierVideo");
    v.src = src;
    await new Promise((resolve) => {
      if (v.readyState >= 1) return resolve();
      v.addEventListener("loadedmetadata", resolve, { once: true });
      setTimeout(resolve, 3000);
    });
    v.currentTime = time || 0;
    this.lastTime = v.currentTime;
    await v.play().catch(() => {});
    this._showIndicator();
    return this._popoutVideo();
  },

  // 通用 iframe 媒体弹出（文档级画中画窗口内嵌播放器；不可用则载体可见兜底）
  async attachEmbedAndPop(iframeSrc, title) {
    this.mode = "embed";
    this.title = title || "媒体";
    this._prepare();
    if ("documentPictureInPicture" in window) {
      try {
        const win = await documentPictureInPicture.requestWindow({
          width: 720,
          height: 450,
        });
        const safeSrc = String(iframeSrc).replace(/"/g, "%22");
        const safeTitle = (this.title || "媒体").replace(/</g, "&lt;");
        win.document.open();
        win.document.write(`<!doctype html><meta charset="utf-8"><title>${safeTitle}</title>
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}iframe{width:100vw;height:100vh;border:0}</style>
<iframe src="${safeSrc}" allowfullscreen allow="autoplay; encrypted-media; picture-in-picture"></iframe>`);
        win.document.close();
        this._showIndicator();
        return true;
      } catch (err) {
        /* 文档级画中画不可用：走可见迷你播放器兜底 */
      }
    }
    $("carrierYtWrap").innerHTML = `<iframe src="${iframeSrc}" allowfullscreen allow="autoplay; encrypted-media"></iframe>`;
    this._showVisibleFallback();
    return false;
  },

  // YouTube：iframe 内视频跨域不可直接 PiP。优先文档级画中画（系统置顶窗口 +
  // 官方 IFrame 播放器）；不支持时退回页面内可见迷你播放器。
  async attachYtAndPop(videoId, start, title) {
    this.mode = "youtube";
    this.title = title || "YouTube";
    this.videoId = videoId;
    this._prepare();
    this._clearAttention();
    const v = $("carrierVideo");
    v.pause();
    v.removeAttribute("src");
    v.load();
    const startPos = Math.max(0, Math.floor(start || 0));
    if ("documentPictureInPicture" in window) {
      try {
        const win = await documentPictureInPicture.requestWindow({
          width: 640,
          height: 360,
        });
        const safeTitle = (this.title || "YouTube").replace(/</g, "&lt;");
        win.document.open();
        win.document.write(`<!doctype html><meta charset="utf-8"><title>${safeTitle}</title>
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}iframe{width:100vw;height:100vh;border:0}</style>
<div id="player"></div>
<script src="https://www.youtube.com/iframe_api"><\/script>
<script>
window.__ytPlayer = null;
window.onYouTubeIframeAPIReady = function () {
  window.__ytPlayer = new YT.Player("player", {
    videoId: ${JSON.stringify(videoId)},
    playerVars: { autoplay: 1, start: ${startPos}, rel: 0 },
  });
};
addEventListener("pagehide", function () {
  var t = 0;
  try {
    if (window.__ytPlayer && window.__ytPlayer.getCurrentTime) t = window.__ytPlayer.getCurrentTime();
  } catch (e) {}
  try { window.opener.__docPipClosed(Math.floor(t)); } catch (e) {}
});
<\/script>`);
        win.document.close();
        this.pipOpen = true;
        this._clearAttention();
        this._showIndicator();
        return true;
      } catch (err) {
        /* 文档级画中画不可用：走可见迷你播放器兜底 */
      }
    }
    await ensureYtApiP();
    $("carrierYtWrap").innerHTML = '<div id="carrierYt"></div>';
    carrierYtPlayer = new YT.Player("carrierYt", {
      videoId,
      playerVars: { autoplay: 1, start: startPos, rel: 0 },
    });
    this._showVisibleFallback();
    return false;
  },

  // 从上次进度恢复弹出（顶栏点亮按钮的点击行为）
  async resume() {
    if (this.mode === "video") return this._popoutVideo();
    if (this.mode === "youtube")
      return this.attachYtAndPop(this.videoId, this.lastTime, this.title);
    return false;
  },
};

const ensureYtApiP = () => new Promise((resolve) => ensureYtApi(resolve));

// ---- 访问令牌（设置 → 访问令牌）：存浏览器本地，随所有 API 请求自动携带 ----
$("tokenInput").value = token();
$("saveTokenBtn").onclick = () => {
  const t = $("tokenInput").value.trim();
  t ? localStorage.setItem("da_token", t) : localStorage.removeItem("da_token");
  flash(t ? "访问令牌已保存，立即生效" : "访问令牌已清除");
};

// 文档级画中画窗口关闭时的回调（由 PiP 窗口 pagehide 事件调用，回传播放进度）
window.__docPipClosed = (lastPos = 0) => {
  if (lastPos > 0) mediaCarrier.lastTime = lastPos;
  mediaCarrier.pipOpen = false;
  mediaCarrier._lightUp();  // 点亮顶栏按钮，提示可从原进度恢复
};

document.addEventListener("leavepictureinpicture", () => {
  // 原生视频画中画被关闭（含被其它网站的画中画顶掉）：暂停并点亮顶栏按钮
  const v = $("carrierVideo");
  if (mediaCarrier.mode === "video" && v) {
    mediaCarrier.lastTime = v.currentTime || mediaCarrier.lastTime;
    v.pause();
    mediaCarrier._lightUp();
  }
});
$("mediaIndicator").onclick = () => mediaCarrier.resume();
$("mediaIndicator").onclick = async () => {
  if (mediaCarrier.mode === "video") await mediaCarrier._popoutVideo();
  else if (mediaCarrier.mode === "youtube") await mediaCarrier._popoutYt();
};

// ---- YouTube IFrame Player API 懒加载（POP/内嵌播放需要）----
let ytPlayerRef = null;
let currentYt = null;
let ytApiQueued = [];
let ytApiLoading = false;

function ensureYtApi(cb) {
  if (window.YT && window.YT.Player) return cb();
  ytApiQueued.push(cb);
  if (ytApiLoading) return;
  ytApiLoading = true;
  const s = document.createElement("script");
  s.src = "https://www.youtube.com/iframe_api";
  s.onerror = () => {
    ytApiQueued = [];
    flash("YouTube 播放器脚本加载失败");
  };
  window.onYouTubeIframeAPIReady = () => {
    const q = ytApiQueued;
    ytApiQueued = [];
    q.forEach((f) => f());
  };
  document.head.appendChild(s);
}
const fmtDate = (s) => {
  if (!s) return "";
  let iso = s.includes("T") ? s : s.replace(" ", "T");
  if (!/[Zz]|[+-]\d{2}:?\d{2}$/.test(iso)) iso += "Z";
  const d = new Date(iso);
  return isNaN(d) ? s : d.toLocaleString();
};
const STATE_LABEL = { none: "", pending: "归档中…", done: "已归档", failed: "归档失败" };

function applyCardState(id, st) {
  const badge = document.querySelector(
    `#itemList .item[data-iid="${id}"] [data-st]`
  );
  if (!badge) return;
  const label = STATE_LABEL[st] || "";
  badge.textContent = label;
  badge.className = "badge st st-" + st;
  badge.classList.toggle("hidden", !label);
}

let sourceCache = [];
const TYPE_LABEL = {
  rss: "RSS",
  arxiv: "arXiv",
  reddit: "Reddit",
  youtube: "YouTube",
  bilibili: "Bilibili",
};
const TYPE_COLOR = {
  rss: "#2e9e5b",
  arxiv: "#8a8f98",
  reddit: "#ff4500",
  youtube: "#ff0000",
  bilibili: "#fb7299",
};
let collapsedGroups = JSON.parse(localStorage.getItem("da_group_collapsed") || "[]");

function sourceItem(s) {
  const li = document.createElement("li");
  li.className = "source" + (state.feed.sourceIds.includes(String(s.id)) ? " active" : "");
  li.dataset.sid = String(s.id);
  li.draggable = true;
  li.innerHTML = `
      <div class="src-main">
        <span class="srcdot" style="background:${s.color || "var(--brand)"}"></span>
        <span class="badge ${s.type}">${s.type}</span>
        <span class="src-name">${esc(s.name)}</span>
      </div>
      <div class="src-meta">
        <span>${s.unread_count} 未读 / ${s.item_count}</span>
      </div>
      <button data-act="refresh" class="src-refresh" title="立即抓取">⟳</button>`;
  li.onclick = () => {
    setFeed({ sourceIds: state.feed.sourceIds.includes(String(s.id)) ? [] : [String(s.id)], types: [] });
    loadSources();
    closeSourceDrawer();
  };
  // 双击：直达 设置 → 设置源，并选中该源（快速自定义）
  li.ondblclick = async (e) => {
    if (e.target.closest('[data-act="refresh"]')) return;
    const ov = $("settingsOverlay");
    ov.classList.remove("hidden");
    try {
      await Promise.all([loadAppSettings(), refreshEditSources()]);
    } catch (err) {
      ov.classList.add("hidden");
      flash("打开设置失败：" + (err.message || err));
      return;
    }
    $("editSourceSel").value = String(s.id);
    $("editSourceSel").onchange();
    document.querySelector('.settings-tabs button[data-tab="edit-source"]')?.click();
  };
  li.querySelector('[data-act="refresh"]').onclick = async (e) => {
    e.stopPropagation();
    e.target.disabled = true;
    try {
      const r = await api(`/sources/${s.id}/refresh`, { method: "POST" });
      alert(`抓取完成：拉取 ${r.fetched} 条，新入库 ${r.inserted} 条`);
    } finally {
      e.target.disabled = false;
    }
    loadItems(true);
    loadSources();
  };
  return li;
}

async function loadSources() {
  const sources = await api("/sources");
  sourceCache = sources;
  const byType = {};
  for (const s of sources) {
    (byType[s.type] = byType[s.type] || []).push(s);
  }
  for (const t in byType) {
    byType[t].sort(
      (a, b) => (a.sort_order || 0) - (b.sort_order || 0) || a.id - b.id
    );
  }
  let groupOrder = [];
  try {
    groupOrder = (await api("/source-prefs")).group_order || [];
  } catch (e) {
    /* prefs 读取失败用默认顺序 */
  }
  const types = [
    ...groupOrder.filter((t) => byType[t]),
    ...Object.keys(byType).filter((t) => !groupOrder.includes(t)).sort(),
  ];
  const list = $("sourceList");
  list.innerHTML = "";
  for (const type of types) {
    const items = byType[type];
    const unread = items.reduce((n, s) => n + s.unread_count, 0);
    const total = items.reduce((n, s) => n + s.item_count, 0);
    const collapsed = collapsedGroups.includes(type);
    const li = document.createElement("li");
    li.className = "src-group" + (collapsed ? " collapsed" : "");
    li.dataset.type = type;
    li.draggable = true;
    const head = document.createElement("div");
    head.className = "group-head";
    head.innerHTML = `
      <span class="group-arrow">${collapsed ? "▶" : "▼"}</span>
      <span class="group-name">${TYPE_LABEL[type] || esc(type)}</span>
      <span class="group-stats">${unread} 未读 / ${total}</span>`;
    head.onclick = () => {
      setFeed({ sourceIds: [], types: [type] });
      closeSourceDrawer();
    };
    head.querySelector(".group-arrow").onclick = (e) => {
      e.stopPropagation();
      const i = collapsedGroups.indexOf(type);
      if (i >= 0) collapsedGroups.splice(i, 1);
      else collapsedGroups.push(type);
      localStorage.setItem("da_group_collapsed", JSON.stringify(collapsedGroups));
      const nowCollapsed = collapsedGroups.includes(type);
      li.classList.toggle("collapsed", nowCollapsed);
      e.target.textContent = nowCollapsed ? "▶" : "▼";
    };
    const ul = document.createElement("ul");
    ul.className = "group-items";
    for (const s of items) ul.appendChild(sourceItem(s));
    li.appendChild(head);
    li.appendChild(ul);
    list.appendChild(li);
  }
  syncControls();
}

// ---- 源列表长按充能：整组/单源一键已读（1 秒充能完成）----
let chargeState = null;    // {el, fill, timer, type, sid}
let chargeClickUntil = 0;  // 充能完成后短暂抑制 click（防止误触筛选/折叠）
function chargeStart(e) {
  if (chargeState) return;
  if (e.target.closest('[data-act="refresh"]')) return;
  const head = e.target.closest(".group-head");
  const src = e.target.closest(".source");
  const el = head || src;
  if (!el) return;
  const fill = document.createElement("div");
  fill.className = "charge-fill";
  el.appendChild(fill);
  requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.width = "100%"; }));
  chargeState = {
    el,
    fill,
    type: head ? head.closest(".src-group").dataset.type : null,
    sid: src ? src.dataset.sid : null,
    timer: setTimeout(chargeComplete, 1000),
  };
  e.preventDefault();
}
function chargeCancel() {
  if (!chargeState) return;
  clearTimeout(chargeState.timer);
  chargeState.fill.remove();
  chargeState = null;
}
async function chargeComplete() {
  const st = chargeState;
  chargeState = null;
  if (!st) return;
  st.fill.remove();
  chargeClickUntil = Date.now() + 600;  // 抑制长按后的 click
  st.el.classList.add("charge-done");
  setTimeout(() => st.el.classList.remove("charge-done"), 700);
  try {
    const body = st.sid ? { source_id: Number(st.sid) } : { type: st.type };
    const r = await api("/items/mark-read", {
      method: "POST",
      body: JSON.stringify(body),
    });
    flash(`已标记 ${r.marked} 条为已读`);
    await loadSources();
    await loadItems(false);
  } catch (e) {
    flash("标记失败：" + (e.message || e));
  }
}
(() => {
  const list = $("sourceList");
  list.addEventListener("pointerdown", chargeStart);
  ["pointerup", "pointerleave", "pointercancel"].forEach((ev) =>
    list.addEventListener(ev, chargeCancel)
  );
  // 充能完成后的 click 抑制（捕获阶段拦截，避免触发组筛选/源切换）
  list.addEventListener(
    "click",
    (e) => {
      if (Date.now() < chargeClickUntil) {
        e.stopPropagation();
        e.preventDefault();
      }
    },
    true
  );
})();

// ---- 源列表拖拽：组间排序 + 组内源排序（HTML5 DnD，落库持久化）----
function initSourceDnd() {
  const list = $("sourceList");
  let dragged = null; // {li, kind: 'source'|'group'}

  list.addEventListener("dragstart", (e) => {
    const srcLi = e.target.closest(".source");
    const head = e.target.closest(".group-head");
    const groupLi = e.target.closest(".src-group");
    if (srcLi && groupLi && groupLi.contains(srcLi) && !head) {
      dragged = { li: srcLi, kind: "source" };
    } else if (groupLi && head) {
      dragged = { li: groupLi, kind: "group" };
    } else {
      e.preventDefault();
      return;
    }
    dragged.li.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    try {
      e.dataTransfer.setData("text/plain", dragged.kind);
    } catch (err) {
      /* IE 兼容忽略 */
    }
  });

  list.addEventListener("dragover", (e) => {
    if (!dragged) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const selector = dragged.kind === "source" ? ".source" : ".src-group";
    const target = e.target.closest(selector);
    if (!target || target === dragged.li) return;
    if (dragged.kind === "source") {
      // 源只能在自己的类型组内排序
      if (target.closest(".src-group") !== dragged.li.closest(".src-group")) return;
    }
    const rect = target.getBoundingClientRect();
    const after = e.clientY > rect.top + rect.height / 2;
    target.parentNode.insertBefore(dragged.li, after ? target.nextSibling : target);
  });

  list.addEventListener("drop", (e) => e.preventDefault());
  list.addEventListener("dragend", () => {
    if (!dragged) return;
    dragged.li.classList.remove("dragging");
    dragged = null;
    const groupOrder = [...document.querySelectorAll("#sourceList .src-group")].map(
      (g) => g.dataset.type
    );
    const sourceOrder = [...document.querySelectorAll("#sourceList .source")].map((s) =>
      parseInt(s.dataset.sid, 10)
    );
    api("/source-prefs", {
      method: "PUT",
      body: JSON.stringify({ group_order: groupOrder, source_order: sourceOrder }),
    }).catch(() => flash("排序保存失败"));
  });
}
initSourceDnd();

const contrastText = (hex) => {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return "#fff";
  const v = m[1];
  const lum =
    0.299 * parseInt(v.substr(0, 2), 16) +
    0.587 * parseInt(v.substr(2, 2), 16) +
    0.114 * parseInt(v.substr(4, 2), 16);
  return lum > 150 ? "#1c1e21" : "#ffffff";
};
const badgeStyle = (color) =>
  color
    ? `background:${color};color:${contrastText(color)};border-color:transparent`
    : "background:var(--brand);color:#fff;border-color:transparent";

function updateCardRead(id) {
  const li = document.querySelector(`#itemList .item[data-iid="${id}"]`);
  if (li) {
    li.classList.remove("unread");
    li.classList.add("read");
  }
}

function applyStar(id, starred) {
  document
    .querySelectorAll(`#itemList .item[data-iid="${id}"] [data-act="star"]`)
    .forEach((b) => b.classList.toggle("on", !!starred));
  const d = $("detail");
  if (!d.classList.contains("hidden") && d.dataset.iid === String(id)) {
    const sb = $("detailStar");
    if (sb) sb.classList.toggle("on", !!starred);
  }
}

function applyBookmark(id, bookmarked) {
  document
    .querySelectorAll(`#itemList .item[data-iid="${id}"] [data-act="bm"]`)
    .forEach((b) => b.classList.toggle("on", !!bookmarked));
  const d = $("detail");
  if (!d.classList.contains("hidden") && d.dataset.iid === String(id)) {
    const db = $("detailBm");
    if (db) db.classList.toggle("on", !!bookmarked);
  }
}

function markDetailOpen(id) {
  document
    .querySelectorAll("#itemList .item.detail-open")
    .forEach((el) => el.classList.remove("detail-open"));
  const li = document.querySelector(`#itemList .item[data-iid="${id}"]`);
  if (li) li.classList.add("detail-open");
}

const fmtDuration = (total) => {
  const s = Math.round(total || 0);
  if (s <= 0) return "";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  const ss = String(sec).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
};

function itemCard(it) {
  const isHit = typeof it.score === "number";
  const li = document.createElement("li");
  li.dataset.iid = String(it.id);
  li.className =
    "item" + (it.is_read ? " read" : " unread");
  const textBlock = `
    <div class="item-title">${esc(it.title) || "（无标题）"}</div>
    <div class="item-summary">${esc((it.summary || "").slice(0, 220))}</div>`;
  const cardImage = it.image && it.image !== "-1" ? it.image : "";
  const thumbBlock = cardImage
    ? `<div class="thumb-wrap"><img class="item-thumb" loading="lazy" src="${esc(
        cardImage
      )}" alt="">${
        it.duration > 0
          ? `<span class="thumb-duration" title="视频时长">${fmtDuration(it.duration)}</span>`
          : ""
      }</div>`
    : "";
  li.innerHTML = `
    <div class="item-head">
      <span class="badge src" style="${badgeStyle(it.source_color)}">${esc(it.source_name)}</span>
      ${
        isHit
          ? `<span class="score-chip" title="匹配近似度"><span class="score-dot" style="background:${scoreColor(
              it.score
            )}"></span>${(it.score * 100).toFixed(1)}%</span>`
          : ""
      }
      <span class="date">${fmtDate(it.fetched_at)}</span>
      <span data-st class="badge st st-${it.fulltext_state || "none"} ${
        STATE_LABEL[it.fulltext_state] ? "" : "hidden"
      }">${STATE_LABEL[it.fulltext_state] || ""}</span>
      <span class="spacer"></span>
      <button data-act="bm" class="star-btn bm-btn ${it.is_bookmarked ? "on" : ""}" title="稍后阅读/书签">${BM_ICON}</button>
      <button data-act="star" class="star-btn ${it.is_starred ? "on" : ""}" title="星标">★</button>
    </div>
    ${
      thumbBlock
        ? `<div class="item-body">${thumbBlock}<div class="item-text">${textBlock}</div></div>`
        : textBlock
    }`;
  li.onclick = () => showDetail(it.id);
  li.querySelector('[data-act="bm"]').onclick = async (e) => {
    e.stopPropagation();
    const next = it.is_bookmarked ? 0 : 1;
    await api(`/items/${it.id}`, {
      method: "PATCH",
      body: JSON.stringify({ is_bookmarked: next }),
    });
    it.is_bookmarked = next;
    applyBookmark(it.id, next);
  };
  li.querySelector('[data-act="star"]').onclick = async (e) => {
    e.stopPropagation();
    const next = it.is_starred ? 0 : 1;
    await api(`/items/${it.id}`, {
      method: "PATCH",
      body: JSON.stringify({ is_starred: next }),
    });
    it.is_starred = next;
    applyStar(it.id, next);
  };
  return li;
}

async function loadItems(keepOffset = false) {
  if (state.searching) return;
  const f = state.feed;
  const isSearch = !!f.q;
  if (!keepOffset) state.offset = 0;
  const p = new URLSearchParams();
  if (f.q) {
    p.set("q", f.q);
    p.set("mode", f.mode);
    state.searching = true;
    setSearching(true);
    searchAbort = new AbortController();
  }
  if (f.sourceIds.length) p.set("source_ids", f.sourceIds.join(","));
  if (f.types.length) p.set("types", f.types.join(","));
  if (f.unread) p.set("unread", "true");
  if (f.starred) p.set("starred", "true");
  if (f.bookmarked) p.set("bookmarked", "true");
  p.set("limit", "50");
  p.set("offset", String(state.offset));
  try {
    const data = await api(
      "/items?" + p.toString(),
      isSearch ? { signal: searchAbort.signal } : {}
    );
    const list = $("itemList");
    if (!keepOffset) list.innerHTML = "";
    for (const it of data.items) list.appendChild(itemCard(it));
    queueFeedTranslation();
    state.offset += data.items.length;
    $("moreBtn").classList.toggle("hidden", !data.has_more);
  } catch (err) {
    if (err.name === "AbortError") flash("已取消本次查询");
    else flash("查询失败：" + err.message);
  } finally {
    if (isSearch) {
      state.searching = false;
      setSearching(false);
      searchAbort = null;
    }
  }
}

async function showDetail(id) {
  const it = await api(`/items/${id}`);
  markDetailOpen(id);
  if (!it.is_read) {
    it.is_read = 1;
    api(`/items/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ is_read: 1 }),
    }).then(() => updateCardRead(id));
  }

  const st = it.fulltext_state || "none";
  const ytId = /(?:youtube\.com\/(?:watch\?v=|shorts\/)|youtu\.be\/)([\w-]{6,})/.exec(
    it.url || ""
  );
  const ytEmbedHtml = ytId
    ? `<div class="yt-player" id="ytPlayerWrap">
        <div id="ytPlayerBox" data-iid="${id}"></div>
        <button id="ytPopBtn" class="pop-btn" title="弹出为悬浮播放器，边浏览边看">POP</button>
      </div>`
    : "";
  // Bilibili 帖子：详情内嵌官方播放器，POP 走文档级画中画
  const biliId = /bilibili\.com\/video\/(BV[\w]+)/.exec(it.url || "");
  const biliEmbedHtml = biliId
    ? `<div class="yt-player">
        <iframe loading="lazy" src="https://player.bilibili.com/player.html?bvid=${esc(
          biliId[1]
        )}&autoplay=0&danmaku=0&high_quality=1" allowfullscreen allow="encrypted-media"></iframe>
        <button id="biliPopBtn" class="pop-btn" title="弹出为悬浮播放器，边浏览边看">POP</button>
      </div>`
    : "";
  // reddit 帖子的图文嵌入：图片帖直接大图；视频帖打开时实时取播放地址内嵌（旧帖同样适用）
  const detailImage = it.image && it.image !== "-1" ? it.image : "";
  const isRedditItem = /reddit\.com\//.test(it.url || "");
  const redditEmbedHtml =
    !ytId && isRedditItem && (it.duration > 0 || detailImage)
      ? `<div class="media-embed" id="redditMedia"><span class="embed-loading">加载媒体…</span></div>`
      : "";
  const archiveBtnHtml =
    it.archive_enabled === 0 || st === "done"
      ? ""
      : st === "pending"
        ? '<button disabled>归档中…</button>'
        : `<button id="archiveBtn" class="primary">${st === "failed" ? "重新存档" : "存原文"}</button>`;
  const uploadBtnHtml =
    it.allow_upload === 1 && st !== "pending"
      ? '<button id="uploadBtn">上传存档</button>'
      : "";
  const delBtnHtml =
    st === "done" ? '<button id="delArchiveBtn" class="danger">删除存档</button>' : "";
  aiDetailAssets = it.assets || [];
  aiDetailItem = { id, title: it.title || "" };
  const d = $("detail");
  d.dataset.iid = String(id);
  d.classList.remove("hidden");
  d.innerHTML = `
    <div class="detail-head">
      <button id="closeDetail" class="ghost">✕ 关闭</button>
      <span class="spacer"></span>
      ${archiveBtnHtml}
      ${uploadBtnHtml}
      ${delBtnHtml}
      <button id="detailBm" class="star-btn bm-btn ${it.is_bookmarked ? "on" : ""}" title="稍后阅读/书签">${BM_ICON}</button>
      <button id="detailStar" class="star-btn ${it.is_starred ? "on" : ""}" title="星标">★</button>
    </div>
    <h2 id="detailTitle" class="orig-dim">${esc(it.title)} <a class="title-src" href="${esc(it.url)}" target="_blank" rel="noopener" title="打开原始链接">🔗</a></h2>
    <ul id="assetList" class="assets"></ul>
    <div id="aiRelated" class="ai-related hidden"></div>
    <p class="meta">${esc(it.source_name)} · ${esc(it.author || "佚名")} ·
      发布 ${fmtDate(it.published_at)} · 入库 ${fmtDate(it.fetched_at)}</p>
    <div id="detailSummary" class="summary">${esc(it.summary).replace(/\n/g, "<br>")}</div>
    ${ytEmbedHtml}
    ${biliEmbedHtml}
    ${redditEmbedHtml}`;
  $("closeDetail").onclick = () => d.classList.add("hidden");
  $("detailBm").onclick = async () => {
    // 以按钮当前视觉状态取反（it 副本可能因卡片操作而过期）
    const next = $("detailBm").classList.contains("on") ? 0 : 1;
    await api(`/items/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ is_bookmarked: next }),
    });
    it.is_bookmarked = next;
    applyBookmark(id, next);
  };
  $("detailStar").onclick = async () => {
    // 以按钮当前视觉状态取反（it 副本可能因卡片操作而过期）
    const next = $("detailStar").classList.contains("on") ? 0 : 1;
    await api(`/items/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ is_starred: next }),
    });
    it.is_starred = next;
    applyStar(id, next);
  };
  const renderAssets = (assets) => {
    // 文档名即查看入口：点击在页内查看器打开（等同原「查看原文」按钮）
    $("assetList").innerHTML = assets
      .map(
        (a) =>
          `<li><a href="/api/assets/${a.id}/file" target="_blank" data-aid="${a.id}">` +
          `${esc(a.name || a.kind)} · ${(a.size / 1024).toFixed(1)} KB</a></li>`
      )
      .join("");
    $("assetList").querySelectorAll("a").forEach((a) => {
      a.onclick = (e) => {
        const aid = Number(a.dataset.aid);
        // 主文档（原「查看原文」的目标）走页内查看器；其余文档新窗口打开
        if (it.viewer && it.viewer.asset && it.viewer.asset.id === aid) {
          e.preventDefault();
          openViewerAsset(it.viewer);
          $("viewerOverlay").classList.remove("hidden");
        }
      };
    });
  };
  renderAssets(it.assets);
  transDetail(it);
  if (ytId) {
    currentYt = { videoId: ytId[1], title: it.title, player: null };
    ensureYtApi(() => {
      const box = document.getElementById("ytPlayerBox");
      if (!box || box.dataset.iid !== String(id) || !currentYt || currentYt.videoId !== ytId[1])
        return;
      currentYt.player = new YT.Player("ytPlayerBox", {
        videoId: currentYt.videoId,
        playerVars: { rel: 0 },
      });
    });
    $("ytPopBtn").onclick = async () => {
      if (!currentYt || !currentYt.player || !currentYt.player.getCurrentTime) {
        flash("播放器尚未就绪，稍后再试");
        return;
      }
      const t = currentYt.player.getCurrentTime() || 0;
      const ok = await mediaCarrier.attachYtAndPop(currentYt.videoId, t, it.title);
      // 弹出成功（画中画）或转投可见迷你窗后，都停掉页内播放器，避免双视口
      try { currentYt.player.destroy(); } catch (e) {}
      currentYt.player = null;
      $("ytPlayerWrap").innerHTML =
        '<span class="embed-loading">视频已在悬浮窗/画中画播放</span>';
      flash(ok ? "视频已在画中画窗口播放" : "画中画不可用：视频在右下角迷你窗播放");
    };
  }
  if ($("biliPopBtn")) {
    $("biliPopBtn").onclick = async () => {
      const ok = await mediaCarrier.attachEmbedAndPop(
        `https://player.bilibili.com/player.html?bvid=${biliId[1]}&autoplay=1&danmaku=0&high_quality=1`,
        it.title
      );
      // 移除页内 iframe 播放器（跨域无法暂停，移除即停止），避免双视口
      const wrap = $("biliPopBtn").closest(".yt-player");
      if (wrap) wrap.innerHTML = '<span class="embed-loading">视频已在悬浮窗/画中画播放</span>';
      flash(ok ? "视频已在画中画窗口播放" : "画中画不可用：视频在右下角迷你窗播放");
    };
  }
  // 相关 AI 讨论：曾以该条目为 context 的会话
  (async () => {
    try {
      const rel = await (await fetch(`/api/chats/for-item/${id}`)).json();
      const box = document.getElementById("aiRelated");
      if (!box) return;
      if (!rel.sessions.length) {
        box.classList.add("hidden");
        return;
      }
      box.classList.remove("hidden");
      box.innerHTML =
        '<div class="ai-related-h">相关讨论</div>' +
        rel.sessions
          .map(
            (s) =>
              `<button class="ai-rel-item" data-sid="${s.id}">${esc(s.title)} · ${s.message_count} 条 · ${fmtDate(s.updated_at)}</button>`
          )
          .join("");
      box.querySelectorAll(".ai-rel-item").forEach((b) => {
        b.onclick = () => {
          aiOpenStack();
          aiLoadSession(Number(b.dataset.sid));
        };
      });
    } catch (e) {
      /* 相关讨论加载失败不影响详情 */
    }
  })();

  if (isRedditItem && document.getElementById("redditMedia")) {
    (async () => {
      const host = document.getElementById("redditMedia");
      if (!host) return;
      // 播放地址优先取自已存档的帖子 JSON（同源、不依赖外网）；视频帖未存档时给静态封面 + 提示
      let videoSrc = "";
      try {
        const jsonAsset = (it.assets || []).find((a) => a.kind === "json");
        if (jsonAsset) {
          const data = await (
            await fetch(`/api/assets/${jsonAsset.id}/file`)
          ).json();
          videoSrc =
            data?.[0]?.data?.children?.[0]?.data?.media?.reddit_video
              ?.fallback_url || "";
        }
      } catch (err) {
        /* 存档读取失败按无视频处理 */
      }
      const box = document.createElement("div");
      box.className = "media-embed-box";
      if (videoSrc) {
        const v = document.createElement("video");
        v.controls = true;
        v.preload = "metadata";
        v.src = videoSrc;
        box.appendChild(v);
        const btn = document.createElement("button");
        btn.className = "pop-btn";
        btn.textContent = "POP";
        btn.onclick = async () => {
          const ok = await mediaCarrier.attachVideoAndPop(
            v.currentSrc || v.src,
            v.currentTime,
            it.title
          );
          if (ok) {
            v.pause();
            flash("视频已在画中画窗口播放");
          } else {
            flash("画中画不可用：视频仍在页面内播放");
          }
        };
        box.appendChild(btn);
        const note = document.createElement("p");
        note.className = "embed-note";
        note.innerHTML = `内嵌播放不含音轨 · <a href="${esc(
          it.url
        )}" target="_blank" rel="noopener">跳转原帖</a>`;
        box.appendChild(note);
      } else if (detailImage) {
        const img = document.createElement("img");
        img.loading = "lazy";
        img.src = detailImage;
        box.appendChild(img);
      }
      if (box.childNodes.length) {
        host.innerHTML = "";
        host.appendChild(box);
      } else {
        host.remove();
      }
    })();
  }
  if ($("archiveBtn")) {
    $("archiveBtn").onclick = async () => {
      $("archiveBtn").disabled = true;
      try {
        await api(`/items/${id}/archive`, { method: "POST" });
      } catch (err) {
        flash("入队失败：" + err.message);
        $("archiveBtn").disabled = false;
      }
    };
  }
  if ($("delArchiveBtn")) {
    $("delArchiveBtn").onclick = async () => {
      if (!confirm("删除该条目的已存档原文？对应文件将从磁盘移除，且不可恢复。")) return;
      try {
        await api(`/items/${id}/archive`, { method: "DELETE" });
        flash("存档已删除");
      } catch (err) {
        flash("删除失败：" + err.message);
      }
    };
  }
  if ($("uploadBtn")) {
    $("uploadBtn").onclick = () => {
      const input = $("uploadInput");
      input.value = "";
      input.onchange = async () => {
        const file = input.files[0];
        if (!file) return;
        const btn = $("uploadBtn");
        btn.disabled = true;
        btn.textContent = "上传中…";
        try {
          const headers = { "Content-Type": file.type || "application/octet-stream" };
          if (token()) headers.Authorization = "Bearer " + token();
          const resp = await fetch(
            `/api/items/${id}/archive/upload?filename=${encodeURIComponent(file.name)}`,
            { method: "POST", headers, body: file }
          );
          if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
          flash("上传存档完成");
        } catch (err) {
          flash("上传失败：" + err.message);
          btn.disabled = false;
          btn.textContent = "上传存档";
        }
      };
      input.click();
    };
  }
}

async function openViewerAsset(viewer) {
  const frame = $("viewerFrame");
  $("viewerPopBtn").classList.toggle("hidden", viewer.kind !== "video");
  if (viewer.kind !== "json") {
    // srcdoc 属性存在时优先于 src（空 srcdoc 会渲染成空白页），必须移除
    frame.removeAttribute("srcdoc");
    frame.src = `/api/assets/${viewer.asset.id}/file`;
    return;
  }
  frame.src = "about:blank";
  const resp = await fetch(`/api/assets/${viewer.asset.id}/file`);
  const data = await resp.json();
  frame.srcdoc = renderRedditDoc(data);
}

const unescapeRedditUrl = (u) => (u || "").replace(/&amp;/g, "&");

// Reddit 帖子的媒体嵌入：视频 > 图集 > 直链图片 > 预览图 > 纯链接
function redditMediaHtml(post) {
  const src = (u) => esc(unescapeRedditUrl(u));
  const cross = (post.crosspost_parent_list || [])[0] || {};
  const video =
    post.media?.reddit_video ||
    post.preview?.reddit_video_preview ||
    cross.media?.reddit_video ||
    cross.preview?.reddit_video_preview;
  if (video && video.fallback_url) {
    return `<div class="media">
      <video controls preload="metadata" src="${src(video.fallback_url)}"></video>
      <button class="pop-btn" onclick="parent.__popRedditVideo(this)">POP</button>
      <p class="media-note">内嵌播放不含音轨 · <a href="https://www.reddit.com${esc(
        post.permalink
      )}" target="_blank" rel="noopener">跳转原帖</a></p>
    </div>`;
  }
  const destUrl = unescapeRedditUrl(post.url_overridden_by_dest || post.url || "");
  const ytMatch = /(?:youtube\.com\/(?:watch\?v=|shorts\/)|youtu\.be\/)([\w-]{6,})/.exec(destUrl);
  if (ytMatch) {
    return `<div class="media">
      <iframe class="yt-embed" loading="lazy" src="https://www.youtube.com/embed/${esc(
        ytMatch[1]
      )}" allowfullscreen allow="encrypted-media; picture-in-picture"></iframe>
      <p class="media-note"><a href="${esc(destUrl)}" target="_blank" rel="noopener">在 YouTube 打开</a></p>
    </div>`;
  }
  const metadata = post.media_metadata || {};
  const metadataList = post.gallery_data?.items
    ? post.gallery_data.items.map((g) => metadata[g.media_id])
    : Object.values(metadata);
  const galleryItems = metadataList.filter(
    (m) => m && m.e === "Image" && (m.s?.u || m.p?.length)
  );
  if (galleryItems.length) {
    return `<div class="media">${galleryItems
      .map(
        (m) =>
          `<img loading="lazy" src="${src(m.s?.u || m.p[m.p.length - 1].u)}">`
      )
      .join("")}</div>`;
  }
  const dest = unescapeRedditUrl(post.url_overridden_by_dest || "");
  if (/\.(jpg|jpeg|png|gif|webp)(\?|$)/i.test(dest)) {
    return `<div class="media"><img loading="lazy" src="${src(dest)}"></div>`;
  }
  const preview = post.preview?.images?.[0]?.source?.url;
  if (preview) {
    return `<div class="media"><img loading="lazy" src="${src(preview)}"></div>`;
  }
  if (dest) {
    return `<div class="link"><a href="${esc(dest)}" target="_blank" rel="noopener">${esc(dest)}</a></div>`;
  }
  return "";
}

function renderRedditDoc(data) {
  const post = data?.[0]?.data?.children?.[0]?.data;
  if (!post) return "<meta charset='utf-8'><p>无法解析帖子数据</p>";
  const comments = ((data?.[1]?.data?.children) || []).filter((c) => c.kind === "t1");
  const fmtDate = (sec) => new Date((sec || 0) * 1000).toISOString().slice(0, 10);
  const commentHtml = (node, depth) => {
    const d = node.data;
    const replies = ((d.replies?.data?.children) || []).filter((r) => r.kind === "t1");
    return `<div class="rc" style="margin-left:${Math.min(depth, 6) * 14}px">
      <div class="rca">${esc(d.author || "?")} · ${fmtDate(d.created_utc)} · ↑${d.score ?? 0}</div>
      <div class="rcb">${esc(d.body || "")}</div>
      ${replies.map((r) => commentHtml(r, depth + 1)).join("")}
    </div>`;
  };
  return `<!doctype html><meta charset="utf-8"><style>
    body{font-family:system-ui,-apple-system,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#1a1a1b;background:#fff;line-height:1.65}
    h1{font-size:22px;margin:0 0 6px} h3{margin:20px 0 4px}
    .meta{color:#7c7c7c;font-size:13px;margin-bottom:12px}
    .self{white-space:pre-wrap;background:#f6f7f8;border-radius:8px;padding:12px 14px;margin:10px 0}
    .link a{color:#0079d3;word-break:break-all}
    .media{margin:12px 0;position:relative}
    .media img{max-width:100%;border-radius:8px;display:block;margin:8px 0}
    .media video{width:100%;max-height:70vh;border-radius:8px;background:#000}
    .media .pop-btn{position:absolute;top:8px;right:8px;z-index:3;background:rgba(0,0,0,0.65);color:#fff;border:1px solid rgba(255,255,255,0.4);border-radius:6px;padding:2px 8px;font-size:11px;cursor:pointer}
    .media .pop-btn:hover{background:rgba(0,0,0,0.85)}
    .media iframe.yt-embed{width:100%;aspect-ratio:16/9;border:0;border-radius:8px}
    .media-note{color:#7c7c7c;font-size:12px;margin:6px 0 0}
    .media-note a{color:#0079d3}
    .rc{border-left:2px solid #edeff1;padding-left:10px;margin:8px 0}
    .rca{color:#7c7c7c;font-size:12px} .rcb{white-space:pre-wrap;font-size:14px}
  </style>
  <h1>${esc(post.title)}</h1>
  <div class="meta">r/${esc(post.subreddit)} · u/${esc(post.author || "?")} · ↑${post.score ?? 0} · ${post.num_comments ?? 0} 条评论 · ${fmtDate(post.created_utc)}</div>
  ${post.selftext ? `<div class="self">${esc(post.selftext)}</div>` : ""}
  ${redditMediaHtml(post)}
  <h3>热门评论</h3>
  ${comments.map((c) => commentHtml(c, 0)).join("") || "<p>暂无评论</p>"}`;
}

async function loadWorkspaces() {
  const list = await api("/workspaces");
  const sel = $("wsSelect");
  sel.innerHTML = "";
  for (const w of list) {
    if (w.active) state.workspace = w.name;
    sel.insertAdjacentHTML(
      "beforeend",
      `<option value="${esc(w.name)}"${w.active ? " selected" : ""}>` +
        `${esc(w.name)} · ${w.unread}未读/${w.items}条</option>`
    );
  }
  sel.disabled = list.length === 0;
}

$("wsSelect").onchange = async () => {
  await api(`/workspaces/${encodeURIComponent($("wsSelect").value)}/activate`, {
    method: "POST",
  });
  await loadWorkspaces();
  loadSources();
  loadItems();
  loadStats();
  refreshQueue();
};

$("wsAddBtn").onclick = async () => {
  const name = prompt("新工作区名称（如：academic / gaming）");
  if (!name || !name.trim()) return;
  await api("/workspaces", {
    method: "POST",
    body: JSON.stringify({ name: name.trim() }),
  });
  await api(`/workspaces/${encodeURIComponent(name.trim())}/activate`, {
    method: "POST",
  });
  await loadWorkspaces();
  loadSources();
  loadItems();
  loadStats();
  refreshQueue();
};

async function loadStats() {
  try {
    const s = await api("/stats");
    $("stats").textContent =
      `${s.workspace} · ${s.items} 条 · 未读 ${s.unread} · 向量 ${s.vectors} · ${s.embedder}`;
  } catch (e) {
    $("stats").textContent = "";
  }
}

$("sourceForm").onsubmit = async (e) => {
  e.preventDefault();
  const type = $("srcType").value;
  const body = {
    type,
    name: $("srcName").value.trim(),
    url: $("srcUrl").value.trim(),
    fetch_interval_min: parseInt($("srcInterval").value, 10) || 60,
    config: {},
    color: $("srcColor").value,
    archive_enabled: $("srcArchiveEn").checked,
    archive_markdown: $("srcMd").checked,
    allow_upload: $("srcAllowUpload").checked,
  };
  if (type === "arxiv") {
    body.config = {
      categories: $("srcCats").value.split(",").map((s) => s.trim()).filter(Boolean),
      max_results: parseInt($("srcMax").value, 10) || 50,
    };
  } else if (type === "reddit") {
    body.config = {
      subreddits: $("srcSubs").value.split(",").map((s) => s.trim()).filter(Boolean),
    };
  } else if (type === "youtube") {
    body.config = { channel: $("srcChannel").value.trim() };
    if (!body.url) body.url = body.config.channel;
  } else if (type === "bilibili") {
    body.config = { channel: $("srcBiliChannel").value.trim() };
    if (!body.url) body.url = body.config.channel;
  }
  await api("/sources", { method: "POST", body: JSON.stringify(body) });
  $("sourceForm").reset();
  loadSources();
  loadStats();
};

$("srcType").onchange = () => {
  const type = $("srcType").value;
  $("arxivCfg").classList.toggle("hidden", type !== "arxiv");
  $("redditCfg").classList.toggle("hidden", type !== "reddit");
  $("ytCfg").classList.toggle("hidden", type !== "youtube");
  $("biliCfg").classList.toggle("hidden", type !== "bilibili");
};
$("searchBox").addEventListener("keydown", (e) => {
  if (e.key === "Enter") setFeed({ q: $("searchBox").value.trim() });
});
$("mode").onchange = (e) => setFeed({ mode: e.target.value });
$("fUnread").onclick = () => setFeed({ unread: !state.feed.unread });
$("fStarred").onclick = () => setFeed({ starred: !state.feed.starred });
$("fBookmarked").innerHTML = BM_ICON;
$("fStarred").innerHTML = STAR_ICON;
$("fStarred").innerHTML = STAR_ICON;
$("fBookmarked").onclick = () => setFeed({ bookmarked: !state.feed.bookmarked });
$("clearFiltersBtn").onclick = () =>
  setFeed({
    q: "",
    sourceIds: [],
    types: [],
    unread: false,
    starred: false,
    bookmarked: false,
    mode: "semantic",
  });
$("moreBtn").onclick = () => loadItems(true);
$("cancelSearchBtn").onclick = () => searchAbort && searchAbort.abort();
$("settingsBtn").onclick = async () => {
  $("settingsOverlay").classList.remove("hidden");
  try {
    await Promise.all([loadAppSettings(), refreshEditSources()]);
  } catch (e) {
    $("settingsOverlay").classList.add("hidden");
    flash("设置加载失败：" + e.message);
  }
};
$("settingsClose").onclick = () => $("settingsOverlay").classList.add("hidden");
document.querySelectorAll(".settings-tabs button").forEach((b) => {
  b.onclick = () => {
    document
      .querySelectorAll(".settings-tabs button")
      .forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".settings-page").forEach((p) => {
      p.classList.toggle("hidden", p.id !== "tab-" + b.dataset.tab);
    });
  };
});

const AI_PRESETS = {
  deepseek: "https://api.deepseek.com/v1",
  openrouter: "https://openrouter.ai/api/v1",
  local: "http://localhost:11434/v1",
};

function syncEmbeddingFields() {
  const isApi = $("embProvider").value === "api";
  $("embLocalFields").classList.toggle("hidden", isApi);
  $("embApiFields").classList.toggle("hidden", !isApi);
}

async function loadAppSettings() {
  const s = await api("/settings");
  $("aiProvider").value = s.ai.provider || "deepseek";
  $("aiBaseUrl").value = s.ai.base_url || "";
  $("aiApiKey").value = s.ai.api_key || "";
  $("aiModel").value = s.ai.model || "";
  $("embProvider").value = s.embedding.provider || "local";
  $("embModel").value = s.embedding.model || "";
  $("embLocalPath").value = s.embedding.local_path || "";
  $("embDevice").value = s.embedding.device || "cpu";
  $("embApiBase").value = s.embedding.api_base || "";
  $("embApiKey").value = s.embedding.api_key || "";
  $("redditClientId").value = (s.reddit && s.reddit.client_id) || "";
  $("redditClientSecret").value = (s.reddit && s.reddit.client_secret) || "";
  const t = s.translate || {};
  transProvider = t.provider === "google" ? "cloud" : t.provider || "none";  // 旧 google 档并入 cloud
  $("transProvider").value = transProvider;
  $("transCloudEngine").value = t.cloud_engine || "google";
  $("transApiType").value = t.api_type || "openai";
  $("transApiUrl").value = t.api_url || "";
  $("transApiModel").value = t.api_model || "";
  $("transApiKey").value = t.api_key || "";
  $("transApiId").value = t.api_id || "";
  $("transApiRegion").value = t.api_region || "";
  syncEmbeddingFields();
  syncTransFields();
}

function syncTransFields() {
  $("transCloudFields").classList.toggle("hidden", $("transProvider").value !== "cloud");
  $("transApiFields").classList.toggle("hidden", $("transProvider").value !== "api");
  const at = $("transApiType").value;
  $("transApiOpenaiFields").classList.toggle("hidden", at !== "openai");
  $("transApiKeyOnly").classList.toggle("hidden", at !== "deepl");
  $("transApiIdKey").classList.toggle("hidden", !(at === "tencent" || at === "volc" || at === "aliyun" || at === "baidu"));
  $("transApiRegion").parentElement.classList.toggle("hidden", at !== "tencent" && at !== "volc" && at !== "aliyun");
}

$("transProvider").onchange = syncTransFields;
$("transApiType").onchange = syncTransFields;

$("saveTransBtn").onclick = async () => {
  await api("/settings", {
    method: "PUT",
    body: JSON.stringify({
      translate: {
        provider: $("transProvider").value,
        cloud_engine: $("transCloudEngine").value,
        api_type: $("transApiType").value,
        api_url: $("transApiUrl").value.trim(),
        api_model: $("transApiModel").value.trim(),
        api_key: $("transApiKey").value.trim(),
        api_id: $("transApiId").value.trim(),
        api_region: $("transApiRegion").value.trim(),
      },
    }),
  });
  flash("翻译设置已保存");
};

$("transTestBtn").onclick = async () => {
  const out = $("transTestOut");
  out.textContent = "测试中…";
  try {
    const cfg = {
      provider: $("transProvider").value,
      cloud_engine: $("transCloudEngine").value,
      api_type: $("transApiType").value,
      api_url: $("transApiUrl").value.trim(),
      api_model: $("transApiModel").value.trim(),
      api_key: $("transApiKey").value.trim(),
      api_id: $("transApiId").value.trim(),
      api_region: $("transApiRegion").value.trim(),
      target: transState.lang,
    };
    const r = await fetch("/api/translate/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cfg }),
    });
    const d = await r.json();
    out.textContent = d.ok ? "✓ " + d.text : "✗ " + (d.error || "失败");
  } catch (e) {
    out.textContent = "✗ " + (e.message || e);
  }
};

$("aiProvider").onchange = (e) => {
  const preset = AI_PRESETS[e.target.value];
  if (preset) $("aiBaseUrl").value = preset;
};
$("embProvider").onchange = syncEmbeddingFields;

$("saveAiBtn").onclick = async () => {
  await api("/settings", {
    method: "PUT",
    body: JSON.stringify({
      ai: {
        provider: $("aiProvider").value,
        base_url: $("aiBaseUrl").value.trim(),
        api_key: $("aiApiKey").value.trim(),
        model: $("aiModel").value.trim(),
      },
    }),
  });
  flash("AI 服务设置已保存");
};

$("saveEmbBtn").onclick = async () => {
  await api("/settings", {
    method: "PUT",
    body: JSON.stringify({
      embedding: {
        provider: $("embProvider").value,
        model: $("embModel").value.trim(),
        local_path: $("embLocalPath").value.trim(),
        device: $("embDevice").value,
        api_base: $("embApiBase").value.trim(),
        api_key: $("embApiKey").value.trim(),
      },
    }),
  });
  loadStats();
  flash("向量设置已保存并即时应用");
};

$("saveRedditBtn").onclick = async () => {
  await api("/settings", {
    method: "PUT",
    body: JSON.stringify({
      reddit: {
        client_id: $("redditClientId").value.trim(),
        client_secret: $("redditClientSecret").value.trim(),
      },
    }),
  });
  flash("Reddit 凭据已保存");
};

// ---- 凭据问题：源抓取遇"凭据失效"时顶栏 ❌ 提示，下拉直达对应设置页 ----
let credIssues = [];
let credPanelOpen = false;

async function refreshCredIssues() {
  try {
    credIssues = (await api("/credential-issues")).issues || [];
  } catch (e) {
    credIssues = [];
  }
  renderCredUI();
}

function renderCredUI() {
  $("credBtn").classList.toggle("hidden", credIssues.length === 0);
  $("credList").innerHTML = credIssues
    .map(
      (i) => `
    <button class="cred-item" data-tab="${esc(i.type)}">
      <span class="group-name">${TYPE_LABEL[i.type] || esc(i.type)}</span>
      <span class="cred-sources" title="${esc(i.sources.join("、"))}">${esc(
        i.sources.join("、")
      )}</span>
      <span class="cred-error" title="${esc(i.error)}">${esc(i.error.slice(0, 70))}</span>
    </button>`
    )
    .join("");
  document.querySelectorAll("#credList .cred-item").forEach((b) => {
    b.onclick = () => {
      $("credPanel").classList.add("hidden");
      credPanelOpen = false;
      $("settingsBtn").click();
      document
        .querySelector(`.settings-tabs button[data-tab="${b.dataset.tab}"]`)
        ?.click();
    };
  });
}

$("credBtn").onclick = () => {
  credPanelOpen = !credPanelOpen;
  $("credPanel").classList.toggle("hidden", !credPanelOpen);
  if (credPanelOpen) refreshCredIssues();
};
// 点击面板外部时收起
document.addEventListener("click", (e) => {
  if (
    credPanelOpen &&
    !e.target.closest("#credPanel") &&
    !e.target.closest("#credBtn")
  ) {
    $("credPanel").classList.add("hidden");
    credPanelOpen = false;
  }
});

let biliQrTimer = null;

// ---- 窄屏抽屉式订阅源面板（宽屏侧栏 pin 常驻，☰ 按钮仅在窄屏显示）----
function closeSourceDrawer() {
  document.getElementById("sidebar")?.classList.remove("open");
  $("sidebarBackdrop")?.classList.add("hidden");
}
$("sidebarBtn").onclick = () => {
  const sb = document.getElementById("sidebar");
  const open = !sb.classList.contains("open");
  sb.classList.toggle("open", open);
  $("sidebarBackdrop").classList.toggle("hidden", !open);
};
$("sidebarBackdrop").onclick = closeSourceDrawer;
window.addEventListener("resize", () => {
  if (window.innerWidth > 1100) closeSourceDrawer();
});

// ---- 来源筛选树形面板（多选，确认后生效）----
let srcPanelOpen = false;
let panelSel = new Set();  // 选中的源 id（字符串）

function sourcesOfType(type) {
  return sourceCache.filter((s) => s.type === type);
}

function sourceFilterLabel() {
  const f = state.feed;
  if (f.types.length === 1 && f.sourceIds.length === 0) {
    return TYPE_LABEL[f.types[0]] || f.types[0];
  }
  if (f.sourceIds.length === 1) {
    const s = sourceCache.find((x) => String(x.id) === f.sourceIds[0]);
    return s ? s.name : "1 源";
  }
  if (f.sourceIds.length > 1 || f.types.length > 1) {
    return `已选 ${f.sourceIds.length + countTypeOnly(f)} 源`;
  }
  return "全部来源";
}

function countTypeOnly(f) {
  // types 选中但未确认展开成 ids 的数量（打开面板时才展开），这里仅用于展示
  let n = 0;
  for (const t of f.types) n += sourcesOfType(t).length;
  return n;
}

function updateSrcFilterBtn() {
  $("srcFilterBtn").textContent = sourceFilterLabel() + " ▾";
  $("srcFilterBtn").classList.toggle("active", state.feed.sourceIds.length > 0 || state.feed.types.length > 0);
}

function renderSrcTree() {
  const tree = $("srcTree");
  tree.innerHTML = "";
  const types = [...new Set(sourceCache.map((s) => s.type))];
  for (const type of types) {
    const sources = sourcesOfType(type);
    const checked = sources.filter((s) => panelSel.has(String(s.id))).length;
    const stateCls =
      checked === 0 ? "" : checked === sources.length ? "on" : "half";
    const btn = document.createElement("button");
    btn.className = "tree-btn type-btn " + stateCls;
    btn.dataset.type = type;
    btn.innerHTML = `<span class="tree-name">${TYPE_LABEL[type] || type}</span><span class="tree-count">${checked}/${sources.length}</span>`;
    tree.appendChild(btn);
    for (const s of sources) {
      const on = panelSel.has(String(s.id));
      const child = document.createElement("button");
      child.className = "tree-btn src-btn" + (on ? " on" : "");
      child.dataset.sid = String(s.id);
      child.innerHTML = `<span class="tree-name">${esc(s.name)}</span>`;
      tree.appendChild(child);
    }
  }
}

function syncTreeChecks() {
  document.querySelectorAll("#srcTree .type-btn").forEach((btn) => {
    const sources = sourcesOfType(btn.dataset.type);
    const checked = sources.filter((s) => panelSel.has(String(s.id))).length;
    btn.classList.toggle("on", sources.length > 0 && checked === sources.length);
    btn.classList.toggle("half", checked > 0 && checked < sources.length);
    const cnt = btn.querySelector(".tree-count");
    if (cnt) cnt.textContent = `${checked}/${sources.length}`;
  });
  document.querySelectorAll("#srcTree .src-btn").forEach((b) => {
    b.classList.toggle("on", panelSel.has(b.dataset.sid));
  });
}

$("srcTree").addEventListener("click", (e) => {
  const typeBtn = e.target.closest(".type-btn");
  if (typeBtn) {
    const type = typeBtn.dataset.type;
    const sources = sourcesOfType(type);
    const allOn =
      sources.length > 0 && sources.every((s) => panelSel.has(String(s.id)));
    for (const s of sources) {
      allOn ? panelSel.delete(String(s.id)) : panelSel.add(String(s.id));
    }
    syncTreeChecks();
    return;
  }
  const srcBtn = e.target.closest(".src-btn");
  if (srcBtn) {
    const sid = srcBtn.dataset.sid;
    panelSel.has(sid) ? panelSel.delete(sid) : panelSel.add(sid);
    syncTreeChecks();
  }
});

function openSrcPanel() {
  panelSel = new Set(state.feed.sourceIds.map(String));
  for (const t of state.feed.types) {
    for (const s of sourcesOfType(t)) panelSel.add(String(s.id));
  }
  renderSrcTree();
  $("srcPanel").classList.remove("hidden");
  srcPanelOpen = true;
}
function closeSrcPanel() {
  $("srcPanel").classList.add("hidden");
  srcPanelOpen = false;
}

$("srcFilterBtn").onclick = () => (srcPanelOpen ? closeSrcPanel() : openSrcPanel());
$("srcPanelCancel").onclick = closeSrcPanel;
$("srcPanelClose").onclick = closeSrcPanel;
document.addEventListener("click", (e) => {
  if (srcPanelOpen && !e.target.closest("#srcPanel") && !e.target.closest("#srcFilterBtn")) {
    closeSrcPanel();
  }
});
$("srcPanelOk").onclick = () => {
  // 折叠：某类型全选 → 记类型；部分选中 → 记具体 id
  const sourceIds = [];
  const types = [];
  const typesPresent = [...new Set(sourceCache.map((s) => s.type))];
  for (const type of typesPresent) {
    const sources = sourcesOfType(type);
    const checked = sources.filter((s) => panelSel.has(String(s.id)));
    if (!checked.length) continue;
    if (checked.length === sources.length) types.push(type);
    else sourceIds.push(...checked.map((s) => String(s.id)));
  }
  setFeed({ sourceIds, types });
  closeSrcPanel();
};
$("biliQrBtn").onclick = async () => {
  try {
    const d = await api("/bilibili/login/qr", { method: "POST" });
    if (!d.qrcode_key) throw new Error("未获取到二维码");
    $("biliQrBox").classList.remove("hidden");
    $("biliQrImg").innerHTML = "";
    $("biliQrOverlay").classList.add("hidden");
    new QRCode($("biliQrImg"), {
      text: d.url,
      width: 180,
      height: 180,
      correctLevel: QRCode.CorrectLevel.M,
    });
    $("biliQrStatus").textContent = "等待扫码…（B站 App → 扫一扫，约 3 分钟有效）";
    clearInterval(biliQrTimer);
    let tries = 0;
    let pollFails = 0;
    biliQrTimer = setInterval(async () => {
      tries += 1;
      if (tries > 90) {
        clearInterval(biliQrTimer);
        $("biliQrStatus").textContent = "二维码已过期，请重新点击「扫码登录」";
        return;
      }
      try {
        const r = await api(
          "/bilibili/login/qr/poll?key=" + encodeURIComponent(d.qrcode_key)
        );
        pollFails = 0;
        if (r.code === 86090) $("biliQrStatus").textContent = "已扫码，请在手机上确认…";
        if (r.code === 0) {
          clearInterval(biliQrTimer);
          $("biliQrStatus").textContent = "✓ 登录成功，凭据已保存。点源列表的 ⟳ 即可抓取";
          $("biliQrOverlay").classList.remove("hidden");
          loadAppSettings();
        }
      } catch (e) {
        pollFails += 1;
        if (pollFails >= 3) {
          $("biliQrStatus").textContent = "登录状态轮询出错（" + e.message + "），仍在重试…";
        }
      }
    }, 2000);
  } catch (err) {
    flash("二维码生成失败：" + err.message);
  }
};

let editSourcesCache = [];
async function refreshEditSources() {
  editSourcesCache = await api("/sources");
  const options =
    '<option value="">— 选择源 —</option>' +
    editSourcesCache
      .map((s) => `<option value="${s.id}">${esc(s.type)}: ${esc(s.name)}</option>`)
      .join("");
  const sel = $("editSourceSel");
  sel.innerHTML = options;
  $("editSourceForm").classList.add("hidden");
  const delSel = $("delSourceSel");
  if (delSel) delSel.innerHTML = options;
}

$("delSourceBtn").onclick = async () => {
  const sel = $("delSourceSel");
  if (!sel.value) return flash("请先选择要删除的订阅源");
  if (!confirm(`删除订阅「${sel.options[sel.selectedIndex]?.text || "该源"}」及其全部条目与存档？不可恢复。`)) return;
  await api(`/sources/${sel.value}`, { method: "DELETE" });
  state.feed.sourceIds = state.feed.sourceIds.filter((x) => x !== sel.value);
  loadItems();
  loadSources();
  loadStats();
  refreshEditSources();
  flash("订阅源已删除");
};

$("editSourceSel").onchange = () => {
  const s = editSourcesCache.find(
    (x) => String(x.id) === $("editSourceSel").value
  );
  const box = $("editSourceForm");
  if (!s) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  $("editName").value = s.name;
  $("editUrl").value = s.url;
  $("editInterval").value = s.fetch_interval_min;
  $("editColor").value = s.color || "#6c63ff";
  $("editArchiveEn").checked = !!s.archive_enabled;
  $("editArchiveMd").checked = !!s.archive_markdown;
  $("editAllowUpload").checked = !!s.allow_upload;
  const isReddit = s.type === "reddit";
  $("editRedditCfg").classList.toggle("hidden", !isReddit);
  if (isReddit) $("editSubs").value = ((s.config && s.config.subreddits) || []).join(", ");
};

$("editSaveBtn").onclick = async () => {
  const sid = $("editSourceSel").value;
  if (!sid) return;
  const current = editSourcesCache.find((x) => String(x.id) === sid);
  const patchBody = {
    name: $("editName").value.trim(),
    url: $("editUrl").value.trim(),
    fetch_interval_min: parseInt($("editInterval").value, 10) || 60,
    color: $("editColor").value,
    archive_enabled: $("editArchiveEn").checked,
    archive_markdown: $("editArchiveMd").checked,
    allow_upload: $("editAllowUpload").checked,
  };
  if (current && current.type === "reddit") {
    patchBody.config = {
      subreddits: $("editSubs").value.split(",").map((x) => x.trim()).filter(Boolean),
    };
  }
  await api(`/sources/${sid}`, { method: "PATCH", body: JSON.stringify(patchBody) });
  await refreshEditSources();
  $("editSourceSel").value = sid;
  $("editSourceSel").dispatchEvent(new Event("change"));
  loadSources();
  flash("源设置已保存");
};

$("viewerClose").onclick = () => {
  $("viewerOverlay").classList.add("hidden");
  $("viewerFrame").removeAttribute("srcdoc");
  $("viewerFrame").src = "about:blank";
};
$("viewerPopBtn").onclick = async () => {
  const doc = $("viewerFrame").contentDocument;
  const v = doc && doc.querySelector("video");
  if (!v) return flash("未找到可弹出的视频");
  const title = $("detail").dataset.iid
    ? ($("detail").querySelector("h2") || {}).textContent || "视频播放"
    : "视频播放";
  const ok = await mediaCarrier.attachVideoAndPop(
    v.currentSrc || v.src,
    v.currentTime,
    title.trim()
  );
  if (ok) {
    $("viewerClose").click();
    flash("视频已在画中画窗口播放");
  } else {
    flash("画中画不可用：视频仍在页面内播放");
  }
};

// 供 reddit 帖子视图（srcdoc）内的 POP 按钮调用
window.__popRedditVideo = async (btn) => {
  const v = btn.closest(".media")?.querySelector("video");
  if (!v) return;
  const ok = await mediaCarrier.attachVideoAndPop(
    v.currentSrc || v.src,
    v.currentTime,
    "Reddit 视频"
  );
  if (ok) {
    v.pause();
    if (typeof parent.flash === "function") parent.flash("视频已在画中画窗口播放");
  } else if (typeof parent.flash === "function") {
    parent.flash("画中画不可用：视频仍在页面内播放");
  }
};

// ---- 存档队列：SSE 事件驱动，卡片/详情/顶栏/面板统一由事件同步 ----
let queueTasks = [];
let taskSeq = 0;
let detailRefreshTimer = null;
let es = null;

const fromCurrentWs = (t) => !t.workspace || t.workspace === state.workspace;
const sourceInfo = (sid) =>
  sourceCache.find((s) => String(s.id) === String(sid)) || null;

async function refreshQueue() {
  try {
    const data = await api("/archive/queue");
    queueTasks = (data.tasks || []).map((t, i) => ({ ...t, seq: i }));
    renderQueueUI();
  } catch (e) {
    /* 快照失败不打断页面 */
  }
}

function upsertTask(v) {
  taskSeq += 1;
  const i = queueTasks.findIndex(
    (t) => t.item_id === v.item_id && t.workspace === v.workspace
  );
  if (i >= 0) queueTasks[i] = { ...queueTasks[i], ...v, seq: queueTasks[i].seq };
  else queueTasks.unshift({ ...v, seq: taskSeq });
}

function removeTask(itemId) {
  queueTasks = queueTasks.filter((t) => t.item_id !== itemId);
}

function renderQueueUI() {
  const tasks = queueTasks.filter(fromCurrentWs);
  const total = tasks.length;
  const finished = tasks.filter(
    (t) => t.state === "done" || t.state === "failed"
  ).length;
  const running = tasks.some((t) => t.state === "running");
  $("queueBtn").classList.toggle("hidden", total === 0);
  $("queueSpin").classList.toggle("hidden", !running);
  $("queueText").textContent = `存档 ${finished}/${total}`;
  if (!$("queuePanel").classList.contains("hidden")) renderQueuePanel(tasks);
}

function renderQueuePanel(tasks) {
  const body = $("queueBody");
  if (!tasks.length) {
    body.innerHTML = '<p class="queue-empty">当前没有存档任务</p>';
    return;
  }
  body.onclick = (e) => {
    const row = e.target.closest(".q-task");
    if (!row) return;
    $("queuePanel").classList.add("hidden");
    showDetail(Number(row.dataset.iid));
  };
  const bySrc = new Map();
  for (const t of tasks) {
    if (!bySrc.has(t.source_id)) bySrc.set(t.source_id, []);
    bySrc.get(t.source_id).push(t);
  }
  const chip = (t) => {
    if (t.state === "queued") return '<span class="qchip q-queued">排队中</span>';
    if (t.state === "running") {
      const prog = t.request_count > 1 ? ` ${t.request_index}/${t.request_count}` : "";
      return `<span class="qchip q-running">下载中${prog}</span>`;
    }
    if (t.state === "done")
      return `<span class="qchip q-done">✓ ${esc(t.name || "已完成")}</span>`;
    return `<span class="qchip q-failed" title="${esc(t.error || "归档失败")}">✕ 失败</span>`;
  };
  body.innerHTML = [...bySrc.entries()]
    .map(([sid, list]) => {
      const src = sourceInfo(sid);
      const dot = src ? `<span class="srcdot" style="background:${src.color || "var(--brand)"}"></span>` : "";
      const name = esc((src && src.name) || list[0].source_name || `源 #${sid}`);
      return `
      <div class="q-src">
        <div class="q-src-head">${dot}<span>${name}</span></div>
        ${list
          .map(
            (t) => `
        <div class="q-task st-${t.state}" data-iid="${t.item_id}" title="点击查看该条目详情">
          <span class="q-title" title="${esc(t.title)}">${esc(t.title || `条目 #${t.item_id}`)}</span>
          ${chip(t)}
        </div>`
          )
          .join("")}
      </div>`;
    })
    .join("");
}

function scheduleDetailRefresh(id) {
  const d = $("detail");
  if (d.classList.contains("hidden") || d.dataset.iid !== String(id)) return;
  clearTimeout(detailRefreshTimer);
  detailRefreshTimer = setTimeout(() => showDetail(id), 200);
}

function handleArchiveEvent(ev) {
  if (!fromCurrentWs(ev)) return;
  const st = ev.state || "";
  switch (ev.type) {
    case "archive.queued":
    case "archive.started":
    case "archive.progress":
    case "archive.done":
    case "archive.failed":
      upsertTask(ev);
      renderQueueUI();
      applyCardState(ev.item_id, st === "done" || st === "failed" ? st : "pending");
      scheduleDetailRefresh(ev.item_id);
      break;
    case "archive.deleted":
      removeTask(ev.item_id);
      renderQueueUI();
      applyCardState(ev.item_id, "none");
      scheduleDetailRefresh(ev.item_id);
      break;
  }
}

function connectEvents() {
  if (es) es.close();
  const url = "/api/events" + (token() ? "?token=" + encodeURIComponent(token()) : "");
  es = new EventSource(url);
  es.onmessage = (e) => {
    try {
      handleArchiveEvent(JSON.parse(e.data));
    } catch (err) {
      /* 忽略无法解析的事件 */
    }
  };
  es.onopen = () => refreshQueue();
  es.onerror = () => {
    // 401 等致命错误会让浏览器彻底关闭连接，此时换新令牌重连
    if (es && es.readyState === EventSource.CLOSED) {
      setTimeout(connectEvents, 5000);
    }
  };
}

$("queueBtn").onclick = () => {
  const p = $("queuePanel");
  p.classList.toggle("hidden");
  if (!p.classList.contains("hidden")) refreshQueue();
};
$("queueClose").onclick = () => $("queuePanel").classList.add("hidden");

loadWorkspaces();
loadSources();
loadItems();
loadStats();
refreshQueue();
refreshCredIssues();
connectEvents();
setInterval(loadStats, 30000);
setInterval(refreshCredIssues, 30000);

// ================= 内置翻译（Google gtx 免费端点，替代浏览器全页翻译） =================
const TRANS_LANGS = [
  { code: "zh", name: "中文" },
  { code: "zh-TW", name: "繁體中文" },
  { code: "en", name: "English" },
  { code: "ja", name: "日本語" },
  { code: "ko", name: "한국어" },
  { code: "fr", name: "Français" },
  { code: "de", name: "Deutsch" },
  { code: "es", name: "Español" },
  { code: "ru", name: "Русский" },
];
const transState = (() => {
  try {
    const s = JSON.parse(localStorage.getItem("da_trans") || "{}");
    return { lang: TRANS_LANGS.some((l) => l.code === s.lang) ? s.lang : "zh" };
  } catch {
    return { lang: "zh" };
  }
})();
let transProvider = "none";  // none | google | api（来自服务器设置）
function transSave() {
  localStorage.setItem("da_trans", JSON.stringify(transState));
}
function transActive() {
  return transProvider !== "none";
}
const transCache = new Map();
function transKey(t) {
  let h = 5381;
  for (let i = 0; i < t.length; i++) h = ((h << 5) + h + t.charCodeAt(i)) | 0;
  return h + ":" + transState.lang;
}
function transNorm(t) {
  return (t || "").replace(/\s+/g, "").toLowerCase();
}
function transSame(a, b) {
  return transNorm(a) === transNorm(b);
}
function alreadyTarget(t) {
  if (transState.lang !== "zh" && transState.lang !== "zh-TW") return false;
  const cjk = (t.match(/[一-鿿]/g) || []).length;
  return cjk / Math.max(1, t.length) > 0.2;
}
async function translateTexts(texts, signal) {
  const need = [];
  const idx = [];
  texts.forEach((t, i) => {
    const k = transKey(t);
    if (!t.trim() || transCache.has(k) || alreadyTarget(t)) return;
    need.push(t);
    idx.push(k);
  });
  if (need.length) {
    for (let s = 0; s < need.length; s += 30) {
      const chunk = need.slice(s, s + 30);
      const d = await api("/translate", {
        method: "POST",
        signal,
        body: JSON.stringify({ texts: chunk, target: transState.lang }),
      });
      chunk.forEach((t, i) => transCache.set(idx[s + i], d.translations[i] || t));
    }
  }
  return texts.map((t) => transCache.get(transKey(t)) ?? t);
}
async function transLoadProvider() {
  try {
    const s = await api("/settings");
    transProvider = (s.translate && s.translate.provider) || "none";
  } catch {
    transProvider = "none";
  }
}
function transRenderMenu() {
  const on = transProvider !== "none";
  $("transBtn").classList.toggle("on", on);
  const ENGINE_NAMES = { google: "Google", mymemory: "MyMemory" };
  const API_TYPE_NAMES = { openai: "OpenAI兼容", deepl: "DeepL", baidu: "百度", tencent: "腾讯云", volc: "火山", aliyun: "阿里" };
  const tiers = $("transTierList");
  const langs = $("transLangList");
  tiers.innerHTML = "";
  langs.innerHTML = "";
  const TIERS = [
    { code: "none", label: "无翻译（用浏览器自带翻译）" },
    { code: "cloud", label: "云端免费翻译（" + (ENGINE_NAMES[transApiCfg.cloudEngine] || "Google") + "）" },
    { code: "api", label: "翻译API" + (transApiCfg.type ? "（" + (API_TYPE_NAMES[transApiCfg.type] || transApiCfg.type) + " " + (transApiCfg.url || transApiCfg.id || "").replace(/^https?:\/\//, "").slice(0, 20) + "）" : "（未配置）") },
  ];
  for (const t of TIERS) {
    const b = document.createElement("button");
    b.className = "tree-btn" + (transProvider === t.code ? " on" : "");
    b.innerHTML = "<span>" + t.label + "</span>";
    b.onclick = async () => {
      if (transProvider !== t.code) {
        await api("/settings", {
          method: "PUT",
          body: JSON.stringify({ translate: { provider: t.code } }),
        });
        transProvider = t.code;
        transRenderMenu();
        transRefreshAll();
      }
    };
    tiers.appendChild(b);
  }
  for (const l of TRANS_LANGS) {
    const b = document.createElement("button");
    b.className = "tree-btn" + (transState.lang === l.code ? " on" : "");
    b.innerHTML = "<span>" + l.name + "</span>";
    b.onclick = () => {
      if (transState.lang !== l.code) {
        transState.lang = l.code;
        transSave();
        transRefreshAll();
      }
      transRenderMenu();
    };
    langs.appendChild(b);
  }
}
async function transRefreshAll() {
  transQueue.length = 0;  // 语言/通道变更：丢弃未完成的旧任务
  if (typeof loadItems === "function") await loadItems(false);
  const iid = $("detail").dataset.iid;
  if (iid && !$("detail").classList.contains("hidden")) showDetail(Number(iid));
}
let transApiCfg = { url: "", model: "" };
transLoadProvider().then(() => {
  if (transActive()) queueFeedTranslation();  // 刷新后自动继续翻译
});

$("transMenuClose").onclick = () => $("transMenu").classList.add("hidden");
$("transSetupBtn").onclick = () => {
  $("transMenu").classList.add("hidden");
  $("settingsOverlay").classList.remove("hidden");
  document.querySelector('.settings-tabs button[data-tab="translate"]')?.click();
};

$("transBtn").onclick = async () => {
  const m = $("transMenu");
  m.classList.toggle("hidden");
  if (!m.classList.contains("hidden")) {
    try {
      const s = await api("/settings");
      const t = s.translate || {};
      transProvider = t.provider || "none";
      transApiCfg = {
        cloudEngine: t.cloud_engine || "google",
        type: t.api_type || "openai",
        url: t.api_url || "",
        model: t.api_model || "",
        id: t.api_id || "",
      };
    } catch { /* 读取失败按未配置处理 */ }
    transRenderMenu();
  }
};
document.addEventListener("click", (e) => {
  if (!e.target.closest("#transMenuWrap")) $("transMenu").classList.add("hidden");
});
// ===== 翻译任务队列：详情（高优先级）可插队，Feed 卡片进视口才排队 =====
const transQueue = [];
let transPumping = false;
function transEnqueue(texts, priority, alive) {
  return new Promise((resolve) => {
    const ac = new AbortController();
    const task = { texts, resolve, alive: alive || (() => true), ac, fail: 0 };
    if (priority) transQueue.unshift(task);
    else transQueue.push(task);
    transPurge();
    transPump();
  });
}
let transCurrent = null;  // 在途任务引用（目标失效时可中止请求）
function transPurge() {
  // 任务目标已销毁/失效（卡片移出视口后被销毁、详情已切换等）→ 出队/中止
  for (let i = transQueue.length - 1; i >= 0; i--) {
    const t = transQueue[i];
    if (t.alive && !t.alive()) {
      transQueue.splice(i, 1);
      t.ac.abort();
      t.resolve(null);
    }
  }
  if (transCurrent && transCurrent.alive && !transCurrent.alive()) {
    transCurrent.ac.abort();  // 在途请求一并中止
  }
}
async function transPump() {
  if (transPumping) return;
  transPumping = true;
  try {
    while (transQueue.length) {
      if (!transActive()) {
        transQueue.length = 0;
        break;
      }
      transPurge();
      const task = transQueue.shift();
      if (!task) continue;
      if (task.alive && !task.alive()) {
        task.ac.abort();
        task.resolve(null);
        continue;
      }
      transCurrent = task;
      let out = null;
      try {
        out = await translateTexts(task.texts, task.ac.signal);
      } catch (e) {
        out = null;  // 通道故障/已中止：结果置空，由调用方决定重试
      }
      transCurrent = null;
      if (task.alive && !task.alive()) out = null;  // 译完也已失效：丢弃
      task.resolve(out);
    }
  } finally {
    transPumping = false;
  }
}

let transObserver = null;
function observeFeedCards() {
  if (!transActive()) return;
  if (!transObserver) {
    transObserver = new IntersectionObserver(
      (entries) => {
        for (const en of entries) {
          if (!en.isIntersecting) continue;
          transObserver.unobserve(en.target);
          transCard(en.target);
        }
      },
      { root: document.getElementById("itemList"), rootMargin: "160px" }
    );
  }
  document.querySelectorAll("#itemList .item").forEach((c) => {
    if (c.dataset.trans !== transState.lang && !c.dataset.transFail) transObserver.observe(c);
  });
}
function queueFeedTranslation() {
  if (!transActive()) return;
  transPurge();
  observeFeedCards();
}

// 单卡翻译（进入视口后调用）：原始 title 保留首行、译文插其下，摘要整体替换
async function transCard(c) {
  const title = c.querySelector(".item-title");
  const summary = c.querySelector(".item-summary");
  const texts = [];
  if (title && title.textContent.trim() && title.textContent !== "（无标题）") {
    texts.push(title.textContent);
  }
  if (summary && summary.textContent.trim()) {
    texts.push(summary.textContent);
  }
  if (!texts.length) {
    c.dataset.trans = transState.lang;
    return;
  }
  const out = await transEnqueue(texts, false, () =>
    c.isConnected && c.dataset.trans !== transState.lang
  );
  if (!out || !transActive() || c.dataset.trans === transState.lang) {
    // 失败：冷却 20s 后允许重试（重新进视口触发）
    c.dataset.transFail = "1";
    setTimeout(() => {
      delete c.dataset.transFail;
      observeFeedCards();
    }, 20000);
    return;
  }
  delete c.dataset.transFail;
  let i = 0;
  if (title && texts.length > 0) {
    // 原始 title 保留在首行（AI 引用原始标题时可定位卡片），译文插其下；与原文一致则不加
    const tr = out[i++];
    if (!transSame(tr, texts[0]) || !title.nextElementSibling || !title.nextElementSibling.classList.contains("item-title-trans")) {
      if (!title.nextElementSibling || !title.nextElementSibling.classList.contains("item-title-trans")) {
        const t = document.createElement("div");
        t.className = "item-title-trans";
        title.after(t);
      }
      title.nextElementSibling.textContent = tr;
    }
  }
  if (summary && texts.length > 1) {
    const tr = out[i++];
    if (!transSame(tr, texts[1])) {
      summary.textContent = tr;
      summary.title = texts[1];
    }
  }
  c.dataset.trans = transState.lang;
}

// 详情面板双语：逐段对照——每段原文（变暗）后紧跟其译文
async function transDetail(it) {
  if (!transActive()) return;
  const titleEl = $("detailTitle");
  const sumEl = $("detailSummary");
  if (!titleEl || !sumEl) return;
  const titleText = titleEl.textContent.replace(/🔗$/, "").trim();
  const paras = (it.summary || sumEl.textContent).split(/\n+/).map((s) => s.trim()).filter(Boolean);
  const alive = () => transActive() && $("detail").dataset.iid === String(it.id);

  // 任务序列：标题译文（若有）+ 各段落译文
  const jobs = [];
  if (titleText && !alreadyTarget(titleText)) {
    jobs.push({ text: titleText });
  }
  for (const p of paras) {
    if (!alreadyTarget(p)) jobs.push({ text: p });
  }
  if (!jobs.length) return;

  // 先搭骨架：标题译文占位 + 逐段（译文占位 → 原文变暗）
  let titleSlot = null;
  if (jobs[0].onTitle) {
    const d = document.createElement("div");
    d.className = "trans-text trans-title trans-pending";
    d.textContent = "翻译中…";
    titleEl.parentElement.insertBefore(d, titleEl);
    titleSlot = d;
  }
  const frag = document.createDocumentFragment();
  for (let k = 1; k < jobs.length; k++) {
    const t = document.createElement("p");
    t.className = "trans-text trans-pending";
    t.textContent = "翻译中…";
    t.style.margin = "6px 0 2px";
    frag.appendChild(t);
    jobs[k].el = t;
    const o = document.createElement("p");
    o.className = "orig-dim";
    o.style.margin = "2px 0 12px";
    o.textContent = jobs[k].text;
    frag.appendChild(o);
  }
  sumEl.textContent = "";
  sumEl.appendChild(frag);

  // 逐段入队（倒序 unshift → 队列顺序 = 段落顺序）；每段译完立刻回填
  for (let k = jobs.length - 1; k >= 0; k--) {
    const job = jobs[k];
    const el = job.el || titleSlot;
    transEnqueue([job.text], true, alive).then((out) => {
      if (!el || !el.isConnected) return;
      const txt = out && out[0] ? out[0] : job.text;
      if (transSame(txt, job.text)) {
        // 译文与原文一致：撤掉译文占位，恢复原文亮度
        el.remove();
        const o = el.nextElementSibling;
        if (o && o.classList.contains("orig-dim")) o.classList.remove("orig-dim");
        return;
      }
      el.classList.remove("trans-pending");
      el.textContent = txt;
    });
  }
}

// ================= AI 阅读助手（交互/排版全面移植自 Open WebUI） =================
let aiAbort = null;  // 流式中断控制器
const aiState = {
  open: false,
  sessionId: null,
  sessions: [],
  messages: [],       // [{id, role, content, media, rating, created_at}]
  busy: false,
  autoScroll: true,
  searchQ: "",
  model: "",
  pinOpen: localStorage.getItem("da_ai_pin_open") !== "0",
};
let aiCtx = [];            // 待发送上下文：{type:'list',count,text} | {type:'item',id,title} | {type:'doc',id,name}
let aiDetailAssets = [];   // 当前详情打开条目的存档资产
let aiDetailItem = null;   // 当前详情条目 {id, title}
const aiPendingMedia = []; // 待发送图片 [{url, name}]
let aiMermaidReady = false;

/* ---------- 小工具 ---------- */
function aiToast(msg) {
  const t = $("aiToast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._tm);
  t._tm = setTimeout(() => t.classList.add("hidden"), 1800);
}
function aiConfirm(title, body, okLabel = "删除") {
  return new Promise((resolve) => {
    $("aiConfirmTitle").textContent = title;
    $("aiConfirmBody").textContent = body;
    $("aiConfirmOk").textContent = okLabel;
    const ov = $("aiConfirmOverlay");
    ov.classList.remove("hidden");
    const done = (v) => {
      ov.classList.add("hidden");
      $("aiConfirmOk").onclick = $("aiConfirmCancel").onclick = ov.onclick = null;
      resolve(v);
    };
    $("aiConfirmOk").onclick = () => done(true);
    $("aiConfirmCancel").onclick = () => done(false);
    ov.onclick = (e) => { if (e.target === ov) done(false); };
  });
}
function aiDownload(name, content, mime = "text/plain") {
  const blob = content instanceof Blob ? content : new Blob([content], { type: mime + ";charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
async function aiCopy(text) {
  try {
    await navigator.clipboard.writeText(text);
    aiToast("已复制到剪贴板");
  } catch {
    flash("复制失败");
  }
}
function aiFmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso.replace(" ", "T") + (iso.length === 16 ? ":00" : ""));
  if (isNaN(d)) return "";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
function aiFmtAgo(iso) {
  const d = new Date((iso || "").replace(" ", "T") + "Z");  // DB 为 UTC
  if (isNaN(d)) return "";
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return "1m";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h";
  const dd = Math.floor(h / 24);
  if (dd < 7) return dd + "d";
  const w = Math.floor(dd / 7);
  if (w < 5) return w + "w";
  return Math.floor(dd / 365) + "y";
}
function aiTimeRange(iso) {
  const d = new Date((iso || "").replace(" ", "T") + "Z");
  if (isNaN(d)) return "更早";
  const now = new Date();
  const diffDays = (now - d) / 86400000;
  const same = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  const yest = new Date(now); yest.setDate(now.getDate() - 1);
  if (same(d, now)) return "今天";
  if (same(d, yest)) return "昨天";
  if (diffDays <= 7) return "过去 7 天";
  if (diffDays <= 30) return "过去 30 天";
  if (d.getFullYear() === now.getFullYear()) return (d.getMonth() + 1) + " 月";
  return String(d.getFullYear());
}
const AI_MONTHS = ["一月","二月","三月","四月","五月","六月","七月","八月","九月","十月","十一月","十二月"];

/* 弹出菜单：items = [{label, icon?, danger?, fn}] */
function aiPopMenu(items, anchor, extraClass = "") {
  let menu = document.getElementById("aiPopMenu");
  if (!menu) {
    menu = document.createElement("div");
    menu.id = "aiPopMenu";
    menu.className = "ai-menu";
    $("aiStack").appendChild(menu);
  }
  menu.innerHTML = "";
  for (const it of items) {
    if (it === "-") {
      const hr = document.createElement("div");
      hr.className = "ai-menu-divider";
      menu.appendChild(hr);
      continue;
    }
    const b = document.createElement("button");
    b.className = "ai-menu-item" + (it.danger ? " danger" : "");
    b.innerHTML = (it.icon || "") + `<span>${esc(it.label)}</span>`;
    b.onclick = () => { aiPopClose(); it.fn(); };
    menu.appendChild(b);
  }
  menu.classList.remove("hidden");
  const r = anchor.getBoundingClientRect();
  const stackR = $("aiStack").getBoundingClientRect();
  menu.style.visibility = "hidden";
  menu.style.left = "0px";
  menu.style.top = "0px";
  requestAnimationFrame(() => {
    const mw = menu.offsetWidth, mh = menu.offsetHeight;
    let x = r.right - mw;
    x = Math.max(stackR.left + 6, Math.min(x, stackR.right - mw - 6));
    let y = r.bottom + 4;
    if (y + mh > stackR.bottom - 6) y = r.top - mh - 4;
    menu.style.right = "auto";  // .ai-menu 默认 right:0，与 left 并存会拉伸全宽
    menu.style.width = "max-content";
    menu.style.maxWidth = Math.min(320, stackR.width - 12) + "px";
    menu.style.left = x - stackR.left + "px";
    menu.style.top = y - stackR.top + "px";
    menu.style.visibility = "";
  });
  setTimeout(() => {
    document.addEventListener("click", aiPopDocClose, { capture: true, once: true });
  }, 0);
}
function aiPopClose() {
  const m = document.getElementById("aiPopMenu");
  if (m) m.classList.add("hidden");
  document.removeEventListener("click", aiPopDocClose, true);
}
function aiPopDocClose(e) {
  const m = document.getElementById("aiPopMenu");
  if (m && !m.contains(e.target)) aiPopClose();
}

/* ---------- SVG 图标（线性，16px stroke≈2） ---------- */
const AI_ICONS = {
  copy: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  check: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m4 12 5 5L20 6"/></svg>',
  edit: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
  trash: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>',
  up: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/></svg>',
  down: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/></svg>',
  regen: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15.36-6.36L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.36 6.36L3 16"/><path d="M3 21v-5h5"/></svg>',
  cont: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m10 8.5 5 3.5-5 3.5v-7Z"/></svg>',
  tts: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4V5Z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>',
  ttsStop: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4V5Z"/><path d="m16 9 5 5M21 9l-5 5"/></svg>',
  info: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/></svg>',
  pin: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1Z"/></svg>',
  pinOff: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1Z"/><path d="m3 3 18 18"/></svg>',
  archive: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="5" rx="1"/><path d="M4 9v9a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9"/><path d="M10 13h4"/></svg>',
  clone: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  pen: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
  dl: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>',
  more: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>',
  stop: '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="10" fill="currentColor"/><rect x="8.5" y="8.5" width="7" height="7" rx="1.5" fill="var(--panel)"/></svg>',
  send: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="m5 12 7-7 7 7"/></svg>',
  bolt: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M13 2 3 14h7l-1 8 10-12h-7l1-8Z"/></svg>',
  fold: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m7 9 5-5 5 5"/><path d="m7 15 5 5 5-5"/></svg>',
  dcsv: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>',
};

/* ---------- 内容渲染管线：marked → sanitize → 代码块/mermaid/表格/告警/KaTeX ---------- */
function aiMathish(t) {
  return /\$|\[a-zA-Z]{2,}|[_^][{(a-zA-Z0-9]/.test(t);
}
function aiAutoFormula(t) {
  // 无 $ 定界符但含 LaTeX 命令与上下标 → 整段按公式块发送，避免被 markdown 吃掉
  if (!t.includes("$") && /\\[a-zA-Z]{2,}/.test(t) && /[_^]/.test(t)) {
    return "$$" + "\n" + t + "\n" + "$$";
  }
  return t;
}

function aiMdProcessRaw(text) {
  // 中文粗斜体定界符间距修正（Open WebUI processResponseContent 的简化版）
  return text
    .replace(/([\u4e00-\u9fff])\*\*([^*\n]+)\*\*/g, "$1 **$2**")
    .replace(/\*\*([^*\n]+)\*\*([\u4e00-\u9fff])/g, "**$1** $2")
    .trim();
}

// 公式提取：markdown 解析前把公式换成占位符，避免 _ ^ {} 被 markdown 吃掉
function aiSplitMath(text) {
  const math = [];
  let n = 0;
  const parts = text.split(/(```[\s\S]*?```|`[^`\n]*`)/);
  const out = parts.map((seg, idx) => {
    if (idx % 2 === 1) return seg;  // 代码段不动
    return seg.replace(
      /(\$\$[\s\S]+?\$\$)|(\\\[[\s\S]+?\\\])|(\\\([\s\S]+?\\\))|(\$[^$\n]+?\$)/g,
      (m) => {
        let display = false, inner = m;
        if (m.startsWith("$$")) { display = true; inner = m.slice(2, -2); }
        else if (m.startsWith("\\[")) { display = true; inner = m.slice(2, -2); }
        else if (m.startsWith("\\(")) { inner = m.slice(2, -2); }
        else { inner = m.slice(1, -1); }
        const token = "MATHTOK" + n++ + "ENDTOK";
        math.push({ token, tex: inner.trim(), display, orig: m });
        return " " + token + " ";
      }
    );
  });
  return { text: out.join(""), math };
}

function aiRenderMd(container, text, opts = {}) {
  const done = opts.done !== false;
  const { text: tokenized, math } = aiSplitMath(text);
  let html;
  try {
    html = window.marked ? marked.parse(aiMdProcessRaw(tokenized)) : esc(tokenized);
  } catch {
    html = "<p>" + esc(tokenized) + "</p>";
  }
  if (window.DOMPurify) html = DOMPurify.sanitize(html, { ADD_ATTR: ["target"] });
  container.innerHTML = '<div class="ai-md">' + html + "</div>";
  const md = container.firstElementChild;
  // 在文本节点上还原公式为 KaTeX（跳过代码块；还原不到的占位符回写原文）
  if (math.length && window.katex) {
    const walker = document.createTreeWalker(md, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        return node.nodeValue.includes("MATHTOK") ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      },
    });
    const hits = [];
    while (walker.nextNode()) hits.push(walker.currentNode);
    const re = /MATHTOK(\d+)ENDTOK/g;
    for (const node of hits) {
      const v = node.nodeValue;
      const frag = document.createDocumentFragment();
      let last = 0, m;
      while ((m = re.exec(v))) {
        if (m.index > last) frag.appendChild(document.createTextNode(v.slice(last, m.index)));
        const item = math[+m[1]];
        const inCode = node.parentElement && node.parentElement.closest("pre, code");
        if (inCode) {
          // 代码语境：占位符回写公式原文
          frag.appendChild(document.createTextNode(item.orig));
        } else {
          const host = document.createElement(item.display ? "div" : "span");
          host.className = item.display ? "ai-katex-display" : "ai-katex-inline";
          try {
            katex.render(item.tex, host, { displayMode: item.display, throwOnError: false });
          } catch {
            host.textContent = item.tex;
          }
          frag.appendChild(host);
        }
        last = m.index + m[0].length;
      }
      if (last < v.length) frag.appendChild(document.createTextNode(v.slice(last)));
      node.replaceWith(frag);
    }
  }

  // 外链安全化
  md.querySelectorAll("a[href]").forEach((a) => {
    const h = a.getAttribute("href") || "";
    if (!/^(https?:|mailto:|tel:)/i.test(h)) a.removeAttribute("href");
    else { a.target = "_blank"; a.rel = "nofollow noopener"; }
  });
  // 行内代码点击复制
  md.querySelectorAll(".ai-md :not(pre) > code, .ai-md .codespan").forEach((c) => {
    c.classList.add("codespan");
    c.title = "点击复制";
    c.onclick = () => aiCopy(c.textContent);
  });
  // 图片 → 灯箱
  md.querySelectorAll("img").forEach((img) => {
    img.onclick = () => aiLightboxOpen(img.src);
  });
  // GitHub 风格提示块
  md.querySelectorAll("blockquote").forEach((bq) => {
    const m = /^\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*(\n|<br\s*\/?>)?/i.exec(bq.innerHTML);
    if (!m) return;
    const type = m[1].toLowerCase();
    bq.className = "ai-alert " + type;
    bq.innerHTML = bq.innerHTML.replace(m[0], "");
    const ICONS = {
      note: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
      tip: '<path d="M9 18h6"/><path d="M10 21h4"/><path d="M12 3a6 6 0 0 1 3.6 10.8c-.6.5-.6 1.2-.6 2.2h-6c0-1-.0-1.7-.6-2.2A6 6 0 0 1 12 3Z"/>',
      important: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/>',
      warning: '<circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
      caution: '<path d="M13 2 3 14h7l-1 8 10-12h-7l1-8Z"/>',
    };
    const title = document.createElement("div");
    title.className = "ai-alert-title";
    title.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[type]}</svg><span>${m[1].toUpperCase()}</span>`;
    bq.prepend(title);
  });
  // 表格：横向滚动 + 悬停复制/导出
  md.querySelectorAll("table").forEach((tbl) => {
    const wrap = document.createElement("div");
    wrap.className = "ai-tbl-wrap";
    const scroll = document.createElement("div");
    scroll.className = "ai-tbl-scroll";
    tbl.replaceWith(wrap);
    wrap.appendChild(scroll);
    scroll.appendChild(tbl);
    const btns = document.createElement("div");
    btns.className = "ai-tbl-btns ai-reveal";
    const bCopy = document.createElement("button");
    bCopy.className = "ai-act";
    bCopy.title = "复制表格";
    bCopy.innerHTML = AI_ICONS.copy;
    bCopy.onclick = () => {
      const rows = [...tbl.rows].map((tr) => [...tr.cells].map((td) => td.textContent.trim()));
      aiCopy(rows.map((r) => r.join("\t")).join("\n"));
    };
    const bCsv = document.createElement("button");
    bCsv.className = "ai-act";
    bCsv.title = "导出 CSV";
    bCsv.innerHTML = AI_ICONS.dcsv;
    bCsv.onclick = () => {
      const rows = [...tbl.rows].map((tr) =>
        [...tr.cells].map((td) => '"' + td.textContent.trim().replace(/"/g, '""') + '"')
      );
      aiDownload("table.csv", "\ufeff" + rows.map((r) => r.join(",")).join("\n"), "text/csv");
    };
    btns.append(bCopy, bCsv);
    wrap.appendChild(btns);
  });

  // 代码块改造
  md.querySelectorAll("pre > code").forEach((code) => {
    const pre = code.parentElement;
    const langM = /language-([\w+-]+)/.exec(code.className || "");
    const lang = (langM ? langM[1] : "").toLowerCase();
    const src = code.textContent;
    try {
      if (
        ["mermaid", "quadrantchart", "xychart", "xychart-beta"].includes(lang) &&
        window.mermaid
      ) {
        aiBuildMermaid(pre, src, lang);
      } else {
        aiBuildCodeBlock(pre, lang, src, done);
      }
    } catch (e) {
      console.warn("code block render failed:", e);  // 单块失败保留原文
    }
  });

  // KaTeX
  if (window.renderMathInElement) {
    try {
      renderMathInElement(md, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "\\[", right: "\\]", display: true },
          { left: "\\(", right: "\\)", display: false },
          { left: "$", right: "$", display: false },
        ],
        throwOnError: false,
      });
    } catch { /* 公式失败不影响正文 */ }
  }
  // 每个公式悬停出复制按钮：复制原始 LaTeX（KaTeX 的 annotation 保留源码）
  md.querySelectorAll(".katex").forEach((k) => {
    const tex = k.querySelector('annotation[encoding="application/x-tex"]')?.textContent;
    if (!tex) return;
    const wrap = document.createElement("span");
    wrap.className =
      "ai-katex-wrap" + (k.parentElement && k.parentElement.classList.contains("katex-display") ? " katex-display-wrap" : "");
    k.replaceWith(wrap);
    wrap.appendChild(k);
    const b = document.createElement("button");
    b.className = "ai-katex-copy";
    b.title = "复制 LaTeX 源码";
    b.innerHTML =
      '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
    b.onclick = (e) => {
      e.stopPropagation();
      const delim = wrap.classList.contains("katex-display-wrap") ? "$$" : "$";
      navigator.clipboard.writeText(delim + tex + delim).then(() => aiToast("已复制 LaTeX（含定界符）"));
    };
    wrap.appendChild(b);
  });
  return md;
}

function aiBuildCodeBlock(pre, lang, src, done) {
  const box = document.createElement("div");
  box.className = "ai-code";
  const head = document.createElement("div");
  head.className = "ai-code-head";
  const label = document.createElement("div");
  label.className = "ai-code-lang";
  label.textContent = lang || "text";
  head.appendChild(label);
  const body = document.createElement("pre");
  const codeEl = document.createElement("code");
  codeEl.className = "language-" + (lang || "text");
  if (window.hljs) {
    try {
      if (lang && hljs.getLanguage(lang)) {
        codeEl.innerHTML = hljs.highlight(src, { language: lang, ignoreIllegals: true }).value;
      } else {
        codeEl.textContent = src;
      }
    } catch { codeEl.textContent = src; }
  } else {
    codeEl.textContent = src;
  }
  body.appendChild(codeEl);
  let folded = false, foldedBody = null;
  const nLines = src.split("\n").length;

  const mkBtn = (label, onClick) => {
    const b = document.createElement("button");
    b.className = "ai-code-btn";
    b.textContent = label;
    b.onclick = () => onClick(b);
    return b;
  };
  // 折叠/展开
  const foldBtn = mkBtn("折叠", (b) => {
    folded = !folded;
    b.textContent = folded ? "展开" : "折叠";
    if (folded) {
      foldedBody = document.createElement("div");
      foldedBody.className = "ai-code-fold";
      foldedBody.textContent = `${nLines} hidden lines`;
      body.style.display = "none";
      box.insertBefore(foldedBody, box.querySelector(".ai-code-fold") || null);
    } else {
      body.style.display = "";
      const f = box.querySelector(".ai-code-fold");
      if (f) f.remove();
    }
  });
  // 复制
  const copyBtn = mkBtn("复制", (b) => {
    navigator.clipboard.writeText(src).then(() => {
      b.textContent = "已复制";
      setTimeout(() => { b.textContent = "复制"; }, 1000);
    });
  });
  head.append(foldBtn, copyBtn);
  // html / svg → 预览（工件）
  const isArtifact = ["html", "svg"].includes(lang) || (lang === "xml" && /<svg[\s>]/i.test(src));
  if (isArtifact && done) {
    head.appendChild(mkBtn("预览", () => aiArtifactOpen(src, lang)));
  }
  box.append(head, body);
  pre.replaceWith(box);
}

/* ---------- Mermaid：主题化渲染 + 平移缩放 + 导出 ---------- */
function aiInitMermaid() {
  if (aiMermaidReady || !window.mermaid) return;
  mermaid.initialize({
    startOnLoad: false,
    theme: matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "default",
    securityLevel: "loose",
    htmlLabels: false,
  });
  aiMermaidReady = true;
}
function aiSanitizeSvg(svg) {
  if (!window.DOMPurify) return svg;
  return DOMPurify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true },
    ADD_TAGS: ["style", "foreignObject", "use"],
    ADD_ATTR: [
      "class", "style", "id", "viewBox", "preserveAspectRatio", "markerWidth", "markerHeight",
      "markerUnits", "refX", "refY", "orient", "href", "xlink:href", "dominant-baseline",
      "text-anchor", "clipPathUnits", "filterUnits", "patternUnits", "patternContentUnits",
      "maskUnits", "role", "aria-label", "aria-hidden", "tabindex",
    ],
  });
}
async function aiBuildMermaid(pre, src, lang = "mermaid") {
  aiInitMermaid();
  // ```quadrantChart / ```xychart-beta 类 fence 的图类型声明在语言标记上，
  // 正文首行不是图类型——mermaid 需要它，缺失则补回（检测大小写敏感，用规范驼峰名）
  const CANON = { quadrantchart: "quadrantChart", xychart: "xychart-beta", "xychart-beta": "xychart-beta" };
  const canon = CANON[lang];
  if (canon && !new RegExp("^\\s*" + canon, "i").test(src)) {
    src = canon + "\n" + src;
  }
  const box = document.createElement("div");
  box.className = "ai-mermaid";
  const stage = document.createElement("div");
  stage.className = "ai-mermaid-stage";
  const inner = document.createElement("div");
  inner.style.transformOrigin = "center center";
  stage.appendChild(inner);
  const ctrls = document.createElement("div");
  ctrls.className = "ai-mermaid-ctrls";
  box.append(stage, ctrls);
  pre.replaceWith(box);
  // xychart 词法器不支持 CJK：标题/轴标签里的中日韩文本先替换为 ASCII 占位符，
  // 渲染完成后再把占位符替换回原文（SVG 文本节点逐个还原）
  let cjkMap = null;
  if (/^xychart/.test(src.trim())) {
    cjkMap = [];
    let i = 0;
    const put = (orig) => {
      const token = "zhT" + i++ + "Zh";
      cjkMap.push([token, orig]);
      return token;
    };
    // 先处理引号内整串，再处理剩余裸 CJK（轴标签 [1月, 2月] 等）
    src = src.replace(/"([^"]*[一-鿿][^"]*)"/g, (m, inner_) => '"' + put(inner_) + '"');
    src = src.replace(/[一-鿿][一-鿿\w]*/g, put);
  }
  let svgText = "";
  try {
    const r = await mermaid.render("mmd-" + Math.random().toString(36).slice(2), src);
    svgText = typeof r === "string" ? r : (r && r.svg) || "";
    if (!svgText) throw new Error("mermaid 返回空 SVG");
    if (cjkMap && cjkMap.length) {
      for (const [token, orig] of cjkMap) {
        svgText = svgText.split(token).join(orig);
      }
    }
    inner.innerHTML = aiSanitizeSvg(svgText);
  } catch (err) {
    const eb = document.createElement("div");
    eb.className = "ai-diagram-err";
    eb.innerHTML =
      `${AI_ICONS.info}<div>图表渲染失败：${esc(String(err && err.message || err))}</div>`;
    box.replaceWith(eb);
    const fb = document.createElement("div");
    fb.className = "ai-code";
    fb.innerHTML =
      `<div class="ai-code-head"><div class="ai-code-lang">mermaid</div></div>` +
      `<pre><code class="language-mermaid"></code></pre>`;
    fb.querySelector("code").textContent = src;
    eb.after(fb);
    return;
  }
  // 平移 / 缩放（Ctrl+滚轮缩放，拖拽平移）
  const view = { x: 0, y: 0, s: 1 };
  const apply = () => {
    inner.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.s})`;
  };
  stage.addEventListener("wheel", (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    view.s = Math.min(8, Math.max(0.2, view.s * Math.exp(-e.deltaY * 0.002)));
    apply();
  }, { passive: false });
  let pan = null;
  stage.addEventListener("pointerdown", (e) => {
    pan = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y };
    stage.setPointerCapture(e.pointerId);
    stage.classList.add("panning");
  });
  stage.addEventListener("pointermove", (e) => {
    if (!pan) return;
    view.x = pan.vx + (e.clientX - pan.x);
    view.y = pan.vy + (e.clientY - pan.y);
    apply();
  });
  stage.addEventListener("pointerup", () => { pan = null; stage.classList.remove("panning"); });
  // 「适应」：把图表视口高度调整到正好容纳当前缩放下的整张图（长图可跨页滚动查看）
  const svgNaturalH = () => {
    const svg = inner.querySelector("svg");
    if (!svg) return 0;
    return svg.getBoundingClientRect().height / view.s;
  };
  const fitStage = () => {
    const natH = svgNaturalH();
    if (!natH) return;
    const target = Math.min(Math.max(natH * view.s + 24, 120), window.innerHeight * 0.85);
    stage.style.height = target + "px";
    view.x = 0;
    view.y = 0;
    apply();
  };
  // 左上角：放大 / 缩小 / 适应
  const ctrlsL = document.createElement("div");
  ctrlsL.className = "ai-mermaid-ctrls-l";
  const mkCtrl = (tip, iconSvg, fn, host = ctrls) => {
    const b = document.createElement("button");
    b.className = "ai-act";
    b.title = tip;
    b.innerHTML = iconSvg;
    b.onclick = (e) => { e.stopPropagation(); fn(); };
    host.appendChild(b);
    return b;
  };
  mkCtrl("放大", '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>', () => { view.s = Math.min(8, view.s * 1.25); apply(); }, ctrlsL);
  mkCtrl("缩小", '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M5 12h14"/></svg>', () => { view.s = Math.max(0.2, view.s * 0.8); apply(); }, ctrlsL);
  mkCtrl("适应：视口高度正好容纳整张图", '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M16 3h3a2 2 0 0 1 2 2v3"/><path d="M8 21H5a2 2 0 0 1-2-2v-3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg>', fitStage, ctrlsL);
  // 首次渲染完成即自动适应：无需操作即可看到视口适中的图表
  fitStage();
  box.append(stage, ctrlsL, ctrls);
  // 右上角：下载 / 重置 / 复制
  mkCtrl("下载 SVG", AI_ICONS.dl, () => {
    aiDownload("diagram.svg", new Blob([svgText], { type: "image/svg+xml" }));
  });
  mkCtrl("重置视图",
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>',
    () => { view.x = view.y = 0; view.s = 1; apply(); });
  mkCtrl("复制源码", AI_ICONS.copy, () => aiCopy(src));
}

/* ---------- 预览面板（HTML/SVG 工件 + context 详情共用右侧 iframe） ---------- */
let aiArtifactSrc = "";
let aiArtifactMeta = { name: "artifact.html", mime: "text/html" };
function aiArtifactShow(name, mime, content, srcdocHtml) {
  aiArtifactSrc = content;
  aiArtifactMeta = { name, mime };
  $("aiArtifactName").textContent = name;
  $("aiEditorBox").classList.add("hidden");  // 编辑器模式与 iframe 预览互斥
  $("aiArtifactCopy").style.display = "";
  $("aiArtifactDownload").style.display = "";
  const fr = $("aiArtifactFrame");
  fr.style.display = "";
  fr.removeAttribute("srcdoc");
  fr.srcdoc = srcdocHtml;
  $("aiArtifact").classList.remove("hidden");
}
function aiArtifactOpen(src, lang) {
  const html = lang === "svg"
    ? `<!DOCTYPE html><html><body style="margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh">${src}</body></html>`
    : src;
  aiArtifactShow(
    lang === "svg" ? "SVG 预览" : "HTML 预览",
    lang === "svg" ? "image/svg+xml" : "text/html",
    src,
    html
  );
}
async function aiPreviewContext(ctxs) {
  let d;
  try {
    d = await api("/chats/context-preview", {
      method: "POST",
      body: JSON.stringify({ contexts: ctxs }),
    });
  } catch (e) {
    return flash("预览加载失败：" + e.message);
  }
  const blocks = d.blocks.filter((b) => (b.text && b.text.trim()) || (b.figures && b.figures.length));
  const body = blocks.length
    ? blocks
        .map((b) => {
          let h = b.text && b.text.trim() ? "<pre>" + esc(b.text) + "</pre>" : "";
          if (b.figures && b.figures.length) {
            h +=
              '<div class="ai-ctx-figs">' +
              b.figures
                .map((f) => `<figure><figcaption>第 ${f.page} 页（含图表）</figcaption><img src="${f.data}"></figure>`)
                .join("") +
              "</div>";
          }
          return h;
        })
        .join("<hr/>")
    : "<p>（该上下文内容为空或已不可用）</p>";
  const doc =
    '<!DOCTYPE html><html><head><meta charset="utf-8"><style>' +
    "body{margin:0;padding:16px 20px;font:13px/1.7 -apple-system,'PingFang SC','Segoe UI',sans-serif;background:#f6f7f9;color:#1c1e21}" +
    "@media (prefers-color-scheme: dark){body{background:#1d1f24;color:#e4e6eb}}" +
    "pre{white-space:pre-wrap;word-break:break-word;font-family:inherit;margin:0}" +
    ".ai-ctx-figs figure{margin:10px 0}.ai-ctx-figs figcaption{font-size:11px;color:#888;margin-bottom:4px}.ai-ctx-figs img{max-width:100%;border:1px solid rgba(128,128,128,.3);border-radius:6px}" +
    "hr{border:none;border-top:1px solid rgba(128,128,128,.25);margin:14px 0}" +
    "</style></head><body>" + body + "</body></html>";
  aiArtifactShow("上下文预览", "text/plain", d.blocks.map((b) => b.text).join("\n\n"), doc);
}
$("aiArtifactClose").onclick = () => {
  $("aiArtifact").classList.add("hidden");
  aiEditorClose();
  $("aiArtifactFrame").removeAttribute("srcdoc");
};
$("aiArtifactCopy").onclick = () => aiCopy(aiArtifactSrc);
$("aiArtifactDownload").onclick = () => aiDownload(aiArtifactMeta.name, aiArtifactSrc, aiArtifactMeta.mime);

/* ---------- 图片标注编辑器（未发送图片）---------- */
const aiEd = {
  open: false,
  idx: -1,
  tool: "brush",
  color: "#e74c3c",
  size: 6,
  img: null,
  hi: null,
  stroke: null,
  drawing: false,
};
function aiEditorOpen(idx) {
  const m = aiPendingMedia[idx];
  if (!m) return;
  aiEd.open = true;
  aiEd.idx = idx;
  const box = $("aiEditorBox");
  const frame = $("aiArtifactFrame");
  box.classList.remove("hidden");
  frame.style.display = "none";
  $("aiArtifactName").textContent = "图片标注";
  $("aiArtifactCopy").style.display = "none";
  $("aiArtifactDownload").style.display = "none";
  const img = $("aiEdImg");
  img.onload = () => {
    const w = img.clientWidth || 300;
    const h = Math.round((img.naturalHeight / img.naturalWidth) * w) || 200;
    for (const id of ["aiEdHi", "aiEdStroke"]) {
      const cv = $(id);
      cv.width = w;
      cv.height = h;
      cv.style.width = w + "px";
      cv.style.height = h + "px";
    }
    aiEd.hi = $("aiEdHi").getContext("2d");
    aiEd.stroke = $("aiEdStroke").getContext("2d");
  };
  img.src = m.url;
  $("aiArtifact").classList.remove("hidden");
}
function aiEditorClose() {
  aiEd.open = false;
  $("aiEditorBox").classList.add("hidden");
  $("aiArtifactFrame").style.display = "";
  $("aiArtifactCopy").style.display = "";
  $("aiArtifactDownload").style.display = "";
}
(async () => {
  // 预载一张默认笔触颜色校准图（无需网络）
})();
function aiEdPos(e) {
  const cv = $("aiEdStroke");
  const r = cv.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}
function aiEdBind() {
  const cv = $("aiEdStroke");
  cv.addEventListener("pointerdown", (e) => {
    if (!aiEd.open) return;
    try { cv.setPointerCapture(e.pointerId); } catch { /* 合成事件无活动指针 */ }
    aiEd.drawing = true;
    const { x, y } = aiEdPos(e);
    const ctx = aiEd.stroke;
    const hctx = aiEd.hi;
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    if (aiEd.tool === "eraser") {
      // 只擦笔迹（画笔层 + 高亮层），不碰原图
      ctx.globalCompositeOperation = "destination-out";
      ctx.lineWidth = Math.max(aiEd.size * 2, 14);
      ctx.strokeStyle = "rgba(0,0,0,1)";
      ctx.lineTo(x + 0.01, y);
      ctx.stroke();
      hctx.globalCompositeOperation = "destination-out";
      hctx.lineWidth = Math.max(aiEd.size * 2, 14);
      hctx.strokeStyle = "rgba(0,0,0,1)";
      hctx.beginPath();
      hctx.moveTo(x, y);
      hctx.lineTo(x + 0.01, y);
      hctx.stroke();
      hctx.globalCompositeOperation = "source-over";
      ctx.globalCompositeOperation = "source-over";
    } else if (aiEd.tool === "highlight") {
      // 高亮笔只画在 hi 层：CSS multiply 与原图混合 = 照亮效果
      hctx.beginPath();
      hctx.moveTo(x, y);
      hctx.lineCap = "round";
      hctx.lineJoin = "round";
      hctx.globalAlpha = 0.5;
      hctx.lineWidth = aiEd.size * 3;
      hctx.strokeStyle = aiEd.color;
      hctx.lineTo(x + 0.01, y);
      hctx.stroke();
      hctx.globalAlpha = 1;
    } else {
      ctx.strokeStyle = aiEd.color;
      ctx.lineWidth = aiEd.size;
      ctx.lineTo(x + 0.01, y);
      ctx.stroke();
    }
  });
  cv.addEventListener("pointermove", (e) => {
    if (!aiEd.drawing) return;
    const { x, y } = aiEdPos(e);
    if (aiEd.tool === "highlight") {
      const hctx = aiEd.hi;
      hctx.lineTo(x, y);
      hctx.stroke();
      return;
    }
    const ctx = aiEd.stroke;
    if (aiEd.tool === "eraser") {
      // 橡皮：两层都保持 destination-out（否则会画成黑线）
      ctx.globalCompositeOperation = "destination-out";
      ctx.lineTo(x, y);
      ctx.stroke();
      const hctx = aiEd.hi;
      hctx.globalCompositeOperation = "destination-out";
      hctx.lineTo(x, y);
      hctx.stroke();
      hctx.globalCompositeOperation = "source-over";
      ctx.globalCompositeOperation = "source-over";
      return;
    }
    ctx.lineTo(x, y);
    ctx.stroke();
  });
  const stop = () => {
    aiEd.drawing = false;
    const ctx = aiEd.stroke;
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
    if (aiEd.hi) {
      aiEd.hi.globalAlpha = 1;
      aiEd.hi.globalCompositeOperation = "source-over";
    }
  };
  cv.addEventListener("pointerup", stop);
  cv.addEventListener("pointercancel", stop);
}
aiEdBind();  // 画布指针监听（元素为静态节点，启动即绑定）

// 工具栏交互
$("aiEdColor").oninput = (e) => { aiEd.color = e.target.value; };
document.querySelectorAll(".ai-ed-size").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll(".ai-ed-size").forEach((x) => x.classList.remove("on"));
    b.classList.add("on");
    aiEd.size = Number(b.dataset.size);
  };
});
document.querySelectorAll(".ai-ed-tool").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll(".ai-ed-tool").forEach((x) => x.classList.remove("on"));
    b.classList.add("on");
    aiEd.tool = b.dataset.tool;
    $("aiEdStroke").style.cursor = aiEd.tool === "eraser" ? "cell" : "crosshair";
  };
});

// 保存：按原始分辨率合成（图片 × 高亮层 multiply × 笔迹层）并回传替换待发送图片
$("aiEdSave").onclick = async () => {
  const m = aiPendingMedia[aiEd.idx];
  if (!m) return;
  const img = $("aiEdImg");
  if (!img.naturalWidth) return flash("图片尚未加载完成");
  const natW = img.naturalWidth;
  const natH = img.naturalHeight;
  const out = document.createElement("canvas");
  out.width = natW;
  out.height = natH;
  const octx = out.getContext("2d");
  octx.drawImage(img, 0, 0);
  octx.globalCompositeOperation = "multiply";
  octx.drawImage($("aiEdHi"), 0, 0, natW, natH);
  octx.globalCompositeOperation = "source-over";
  octx.drawImage($("aiEdStroke"), 0, 0, natW, natH);
  const blob = await new Promise((res) => out.toBlob(res, "image/png"));
  try {
    const resp = await fetch("/api/ai-media/upload?filename=" + encodeURIComponent((m.name || "image") + "-标注.png"), {
      method: "POST",
      headers: { "Content-Type": "image/png" },
      body: await blob.arrayBuffer(),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const d = await resp.json();
    aiPendingMedia[aiEd.idx] = { url: d.url, name: d.name };
    aiRenderMediaChips();
    aiEditorClose();
    aiToast("标注已保存，将随消息发送");
  } catch (e) {
    flash("标注保存失败：" + (e.message || e));
  }
};

/* ---------- 灯箱 ---------- */
function aiLightboxOpen(src) {
  $("aiLightboxImg").src = src;
  $("aiLightbox").classList.remove("hidden");
}
$("aiLightboxClose").onclick = () => $("aiLightbox").classList.add("hidden");
$("aiLightbox").onclick = (e) => { if (e.target === $("aiLightbox")) $("aiLightbox").classList.add("hidden"); };
$("aiLightboxDownload").onclick = async () => {
  const src = $("aiLightboxImg").src;
  try {
    if (src.startsWith("data:")) {
      aiDownload("image.png", new Blob([await (await fetch(src)).blob()], { type: "image/png" }));
    } else {
      const b = await (await fetch(src)).blob();
      const ext = (b.type.split("/")[1] || "png").split("+")[0];
      aiDownload("image." + ext, b);
    }
  } catch {
    window.open(src, "_blank");
  }
};

/* ---------- 消息 DOM ---------- */
function aiCursorEl() {
  const s = document.createElement("span");
  s.className = "ai-cursor";
  return s;
}
function aiMsgErrBox(msg) {
  const d = document.createElement("div");
  d.className = "ai-errbox";
  d.innerHTML = `${AI_ICONS.info}<div>${esc(msg)}</div>`;
  return d;
}
function aiActsEl(msg, isLast) {
  const acts = document.createElement("div");
  acts.className = "ai-acts" + (isLast ? "" : " ai-reveal-wrap");
  return acts;
}
function aiActBtn(icon, tip, fn, cls = "") {
  const b = document.createElement("button");
  b.className = "ai-act " + cls;
  b.title = tip;
  b.innerHTML = icon;
  b.onclick = fn;
  return b;
}

function aiMsgCtxRow(contexts) {
  const row = document.createElement("div");
  row.className = "ai-msg-ctx";
  for (const c of contexts) {
    const b = document.createElement("button");
    b.className = "ai-ctx-tag";
    b.title = "点击预览内容";
    b.textContent = aiCtxChipLabel(c);
    b.onclick = () => aiPreviewContext([c]);
    row.appendChild(b);
  }
  return row;
}

const AI_CHEVRON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>';
function aiBuildCot(msg) {
  // 思维链折叠块：thinking=流式中（shimmer+计时）；否则显示"已深度思考（用时 N 秒）"
  const wrap = document.createElement("div");
  wrap.className = "ai-cot";
  const head = document.createElement("button");
  head.className = "ai-cot-h";
  const body = document.createElement("div");
  body.className = "ai-cot-b hidden";
  const setHead = (done, secs) => {
    head.innerHTML =
      (done ? AI_CHEVRON : '<span class="ai-cot-spin"></span>') +
      '<span class="ai-cot-t' + (done ? "" : " ai-shimmer") + '">' +
      (done ? "已深度思考（用时 " + (secs || 0) + " 秒）" : "思考中…") + "</span>" +
      (done ? AI_CHEVRON : "");
    const chev = head.querySelector("svg:last-child");
    if (chev) chev.style.transform = "";
  };
  let secs = msg.reasoning_secs || 0;
  let done = !msg._thinking;
  setHead(done, secs);
  head.onclick = () => {
    body.classList.toggle("hidden");
    const svgs = head.querySelectorAll("svg");
    if (svgs.length > 1) svgs[svgs.length - 1].style.transform = body.classList.contains("hidden") ? "" : "rotate(180deg)";
  };
  body.textContent = msg.reasoning || "";
  wrap.append(head, body);
  return {
    el: wrap,
    onReasoning(chunk) {
      msg.reasoning = (msg.reasoning || "") + chunk;
      body.textContent = msg.reasoning;
      if (!body.classList.contains("hidden")) body.scrollTop = body.scrollHeight;
    },
    finish(s) {
      secs = s || secs;
      done = true;
      setHead(true, secs);
      msg.reasoning_secs = secs;
      body.classList.remove("hidden");
      body.classList.add("hidden");
    },
  };
}

function aiBuildMsg(msg, isLast) {
  const root = document.createElement("div");
  root.className = "ai-msg " + (msg.role === "user" ? "user" : "assistant");
  root.dataset.id = msg.id || "";

  if (msg.role === "user") {
    if (msg.contexts && msg.contexts.length) {
      root.appendChild(aiMsgCtxRow(msg.contexts));
    }
    if (msg.media && msg.media.length) {
      const media = document.createElement("div");
      media.className = "ai-msg-user-media";
      for (const m of msg.media) {
        const img = document.createElement("img");
        img.src = m.url;
        img.alt = m.name || "";
        img.onclick = () => aiLightboxOpen(m.url);
        media.appendChild(img);
      }
      root.appendChild(media);
    }
    const bubble = document.createElement("div");
    bubble.className = "ai-bubble";
    if (aiMathish(msg.content) || /[#*`\[\]]|^\s*[-\d]+\.\s/m.test(msg.content)) {
      bubble.classList.add("ai-bubble-md");
      aiRenderMd(bubble, msg.content, { done: true });
    } else {
      bubble.textContent = msg.content;
    }
    root.appendChild(bubble);
    const acts = document.createElement("div");
    acts.className = "ai-acts" + (isLast ? "" : " ai-reveal");
    acts.appendChild(timeEl());
    acts.appendChild(aiActBtn(AI_ICONS.edit, "编辑", () => aiEditUser(msg)));
    acts.appendChild(aiActBtn(AI_ICONS.copy, "复制", () => aiCopy(msg.content)));
    acts.appendChild(aiActBtn(AI_ICONS.trash, "删除", () => aiDeleteMsg(msg, isLast)));
    root.appendChild(acts);
    return root;

    function timeEl() {
      const t = document.createElement("span");
      t.className = "ai-time";
      t.textContent = aiFmtTime(msg.created_at);
      return t;
    }
  }

  // assistant
  const ava = document.createElement("div");
  ava.className = "ai-ava";
  ava.textContent = "✦";
  const main = document.createElement("div");
  main.className = "ai-msg-main";
  const name = document.createElement("div");
  name.className = "ai-msg-name";
  name.textContent = aiState.model || "AI";
  const body = document.createElement("div");
  body.className = "ai-body";
  main.append(name, body);
  if (msg.reasoning) {
    main.insertBefore(aiBuildCot(msg).el, body);  // 思维链缀在正文前
  }
  root.append(ava, main);
  aiRenderMd(body, msg.content, { done: true });
  if (msg._error) {
    main.appendChild(aiMsgErrBox(msg._error));
  }
  if (!msg._streaming) {
    const acts = document.createElement("div");
    acts.className = "ai-acts" + (isLast ? "" : " ai-reveal");
    acts.appendChild(aiActBtn(AI_ICONS.copy, "复制", () => aiCopy(msg.content)));
    acts.appendChild(aiActBtn(AI_ICONS.edit, "编辑", () => aiEditAssistant(msg)));
    const ttsBtn = aiActBtn(AI_ICONS.tts, "朗读", () => aiTts(ttsBtn, msg.content));
    acts.appendChild(ttsBtn);
    const upBtn = aiActBtn(AI_ICONS.up, "好评", () => aiRate(msg, 1, upBtn, downBtn), msg.rating === 1 ? "on" : "");
    const downBtn = aiActBtn(AI_ICONS.down, "差评", () => aiRate(msg, -1, upBtn, downBtn), msg.rating === -1 ? "on" : "");
    acts.append(upBtn, downBtn);
    if (isLast) {
      acts.appendChild(aiActBtn(AI_ICONS.cont, "继续生成", () => aiContinue(msg)));
      acts.appendChild(aiActBtn(AI_ICONS.regen, "重新生成", () => aiRegenerate()));
    }
    acts.appendChild(aiActBtn(AI_ICONS.trash, "删除", () => aiDeleteMsg(msg, isLast)));
    const t = document.createElement("span");
    t.className = "ai-time ai-reveal";
    t.textContent = aiFmtTime(msg.created_at);
    acts.appendChild(t);
    main.appendChild(acts);
  }
  return root;
}

/* ---------- 消息操作 ---------- */
async function aiRate(msg, val, upBtn, downBtn) {
  const stored = msg.rating === val ? null : val;
  msg.rating = stored;
  upBtn.classList.toggle("on", stored === 1);
  downBtn.classList.toggle("on", stored === -1);
  await api(`/chats/${aiState.sessionId}/messages/${msg.id}/rating`, {
    method: "PUT",
    body: JSON.stringify({ rating: stored === null ? 0 : stored }),
  });
  aiToast("感谢你的反馈");
}
let aiTtsUtter = null;
function aiTts(btn, text) {
  if (!("speechSynthesis" in window)) return flash("浏览器不支持朗读");
  if (speechSynthesis.speaking) {
    speechSynthesis.cancel();
    btn.innerHTML = AI_ICONS.tts;
    return;
  }
  const plain = text.replace(/```[\s\S]*?```/g, " 代码块 ").replace(/[#*_>`~\[\]()]/g, "");
  aiTtsUtter = new SpeechSynthesisUtterance(plain.slice(0, 3000));
  aiTtsUtter.lang = "zh-CN";
  aiTtsUtter.onend = aiTtsUtter.onerror = () => { btn.innerHTML = AI_ICONS.tts; };
  btn.innerHTML = AI_ICONS.ttsStop;
  speechSynthesis.speak(aiTtsUtter);
}
async function aiDeleteMsg(msg, isLast) {
  const ok = await aiConfirm("删除消息？", "该消息将被删除。");
  if (!ok) return;
  const idx = aiState.messages.findIndex((m) => m.id === msg.id);
  await api(`/chats/${aiState.sessionId}/messages/${msg.id}`, { method: "DELETE" });
  // 用户消息连同其直接回复一并删除（Open WebUI 语义）
  if (msg.role === "user" && aiState.messages[idx + 1] && aiState.messages[idx + 1].role === "assistant") {
    await api(`/chats/${aiState.sessionId}/messages/${aiState.messages[idx + 1].id}`, { method: "DELETE" });
  }
  await aiReloadMessages();
}
function aiEditUser(msg) {
  const root = document.querySelector(`.ai-msg[data-id="${msg.id}"]`);
  if (!root) return;
  const orig = root.innerHTML;
  root.innerHTML = "";
  const box = document.createElement("div");
  box.className = "ai-edit";
  const ta = document.createElement("textarea");
  ta.value = msg.content;
  const btns = document.createElement("div");
  btns.className = "ai-edit-btns";
  const left = document.createElement("div");
  left.className = "ai-pill-row";
  const save = document.createElement("button");
  save.className = "ai-pill bordered";
  save.textContent = "保存";
  const right = document.createElement("div");
  right.className = "ai-pill-row";
  const cancel = document.createElement("button");
  cancel.className = "ai-pill ghosted";
  cancel.textContent = "取消";
  const send = document.createElement("button");
  send.className = "ai-pill primary";
  send.textContent = "发送";
  left.appendChild(save);
  right.append(cancel, send);
  btns.append(left, right);
  box.append(ta, btns);
  root.appendChild(box);
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(384, ta.scrollHeight) + "px"; };
  grow();
  ta.oninput = grow;
  const close = () => { root.innerHTML = orig; };
  cancel.onclick = close;
  ta.onkeydown = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") send.click();
  };
  save.onclick = async () => {
    const content = ta.value.trim();
    if (!content) return aiToast("请输入消息内容");
    msg.content = content;
    await api(`/chats/${aiState.sessionId}/messages/${msg.id}`, {
      method: "PATCH",
      body: JSON.stringify({ content }),
    });
    await aiReloadMessages();
  };
  send.onclick = async () => {
    const content = ta.value.trim();
    if (!content) return aiToast("请输入消息内容");
    if (aiState.busy) return;
    aiState.busy = true;
    aiAbort = new AbortController();
    aiSetSendStop(true);
    try {
      msg.content = content;
      await api(`/chats/${aiState.sessionId}/messages/${msg.id}`, {
        method: "PATCH",
        body: JSON.stringify({ content, truncate_after: true }),
      });
      const resp = await aiStreamRequest({ regenerate: true });
      await aiConsumeStream(resp, { regenerate: true });
      await aiReloadMessages();  // 关闭编辑框并刷新消息列
    } catch (err) {
      aiState.busy = false;
      aiAbort = null;
      aiSetSendStop(false);
      if (err.name !== "AbortError") flash("编辑后重发失败：" + err.message);
    }
  };
}
function aiEditAssistant(msg) {
  const root = document.querySelector(`.ai-msg[data-id="${msg.id}"]`);
  if (!root) return;
  const main = root.querySelector(".ai-msg-main");
  const orig = main.innerHTML;
  main.innerHTML = "";
  const box = document.createElement("div");
  box.className = "ai-edit";
  const ta = document.createElement("textarea");
  ta.value = msg.content;
  const btns = document.createElement("div");
  btns.className = "ai-edit-btns";
  const right = document.createElement("div");
  right.className = "ai-pill-row";
  const cancel = document.createElement("button");
  cancel.className = "ai-pill ghosted";
  cancel.textContent = "取消";
  const save = document.createElement("button");
  save.className = "ai-pill primary";
  save.textContent = "保存";
  right.append(cancel, save);
  btns.append(right);
  box.append(ta, btns);
  main.appendChild(box);
  ta.focus();
  const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(384, ta.scrollHeight) + "px"; };
  grow();
  ta.oninput = grow;
  const close = () => { main.innerHTML = orig; };
  cancel.onclick = close;
  ta.onkeydown = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") save.click();
  };
  save.onclick = async () => {
    const content = ta.value.trim();
    if (!content) return aiToast("请输入消息内容");
    msg.content = content;
    await api(`/chats/${aiState.sessionId}/messages/${msg.id}`, {
      method: "PATCH",
      body: JSON.stringify({ content }),
    });
    await aiReloadMessages();
  };
}

async function aiReloadMessages() {
  if (!aiState.sessionId) return;
  const d = await api(`/chats/${aiState.sessionId}/messages`);
  aiState.messages = d.messages;
  $("aiChatTitle").textContent = d.session.title || "新对话";
  aiRenderAll();
}

/* ---------- 渲染 / 占位 ---------- */
const AI_SUGGESTIONS = [
  { t: "总结当前列表", s: "把附加的列表上下文归纳成要点" },
  { t: "这个条目讲了什么？", s: "结合条目信息给出一段摘要" },
  { t: "提炼文档要点", s: "从存档文档中提取关键结论" },
  { t: "把内容整理成表格", s: "结构化输出便于比较" },
];
function aiRenderPlaceholder() {
  const ph = $("aiPlaceholder");
  if (aiState.messages.length) {
    ph.classList.add("hidden");
    return;
  }
  ph.classList.remove("hidden");
  ph.innerHTML =
    `<div class="ai-ph-hello">你好</div>` +
    `<div class="ai-ph-sub">有什么可以帮你阅读的？可先从右上角按钮附加上下文（当前列表 / 条目 / 存档文档）。</div>` +
    `<div class="ai-ph-sug"><div class="ai-ph-sug-h">${AI_ICONS.bolt}<span>建议</span></div></div>`;
  const sug = ph.querySelector(".ai-ph-sug");
  AI_SUGGESTIONS.forEach((s, i) => {
    const b = document.createElement("button");
    b.className = "ai-sug-row";
    b.style.animationDelay = i * 45 + "ms";
    b.innerHTML = `<div class="ai-sug-title">${esc(s.t)}</div><div class="ai-sug-sub">${esc(s.s)}</div>`;
    b.onclick = () => {
      $("aiInput").value = s.t;
      $("aiInput").focus();
      aiGrowInput();
      aiUpdateSendState();
      aiUpdateMathPreview();
    };
    sug.appendChild(b);
  });
}
function aiRenderAll() {
  const box = $("aiMessages");
  [...box.querySelectorAll(".ai-msg")].forEach((n) => n.remove());
  const n = aiState.messages.length;
  aiState.messages.forEach((m, i) => {
    try {
      box.appendChild(aiBuildMsg(m, i === n - 1));
    } catch (e) {
      console.warn("message render failed:", e);  // 单条失败不影响其余消息
    }
  });
  aiRenderPlaceholder();
  aiScrollBottomNow(false);
}

/* ---------- 滚动 ---------- */
function aiScrollBottomNow(smooth = true) {
  const box = $("aiMessages");
  box.scrollTo({ top: box.scrollHeight, behavior: smooth ? "smooth" : "auto" });
}
$("aiMessages").addEventListener("scroll", () => {
  const box = $("aiMessages");
  aiState.autoScroll = box.scrollHeight - box.scrollTop <= box.clientHeight + 5;
  $("aiScrollBottomWrap").classList.toggle("hidden", aiState.autoScroll);
});
$("aiScrollBottom").onclick = () => {
  aiState.autoScroll = true;
  $("aiScrollBottomWrap").classList.add("hidden");
  aiScrollBottomNow();
};

/* ---------- 发送 / 流式 ---------- */
function aiSetSendStop(stopping) {
  const btn = $("aiSend");
  if (stopping) {
    btn.classList.add("stop");
    btn.innerHTML = AI_ICONS.stop;
    btn.title = "停止";
    btn.disabled = false;
  } else {
    btn.classList.remove("stop");
    btn.innerHTML = AI_ICONS.send;
    btn.title = "发送";
    aiUpdateSendState();
  }
}
function aiUpdateSendState() {
  const has = $("aiInput").value.trim().length > 0 || aiPendingMedia.length > 0;
  $("aiSend").disabled = !has && !aiState.busy;
}
function aiGrowInput() {
  const ta = $("aiInput");
  ta.style.height = "auto";
  ta.style.height = Math.min(384, ta.scrollHeight) + "px";
}
async function aiStreamRequest(payload) {
  return fetch(`/api/chats/${aiState.sessionId}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: aiAbort.signal,
  });
}
function aiStartStreamingMsg() {
  const streamMsg = { id: null, role: "assistant", content: "", _streaming: true };
  aiState.messages.push(streamMsg);
  const box = $("aiMessages");
  const el = aiBuildMsg(streamMsg, true);
  box.appendChild(el);
  aiRenderPlaceholder();
  return { streamMsg, el };
}
function aiReplaceStreamEl(streamEl, streamMsg, isLast) {
  try {
    const el = aiBuildMsg(streamMsg, isLast);
    streamEl.replaceWith(el);
  } catch (e) {
    console.warn("final msg render failed:", e);
    const fb = document.createElement("div");
    fb.className = "ai-msg assistant";
    fb.innerHTML =
      '<div class="ai-ava">✦</div><div class="ai-msg-main"><div class="ai-msg-name">' +
      esc(aiState.model || "AI") +
      '</div><div class="ai-body"></div></div>';
    aiRenderMd(fb.querySelector(".ai-body"), streamMsg.content, { done: true });
    streamEl.replaceWith(fb);
  }
}
async function aiConsumeStream(resp, { regenerate = false, continuation = false } = {}) {
  const { streamMsg, el: streamEl } = aiStartStreamingMsg();
  const main = streamEl.querySelector(".ai-msg-main");
  const body = main.querySelector(".ai-body");
  let cot = null; // {el, onReasoning, finish}
  let rStart = 0;
  let rTimer = 0;
  const startThinking = () => {
    streamMsg._thinking = true;
    cot = aiBuildCot(streamMsg);
    main.insertBefore(cot.el, body);
    rStart = Date.now();
    rTimer = setInterval(() => {
      const h = cot.el.querySelector(".ai-cot-t");
      if (h) h.textContent = "思考中…（" + Math.round((Date.now() - rStart) / 1000) + " 秒）";
    }, 1000);
  };
  const stopThinking = (secs) => {
    clearInterval(rTimer);
    if (cot) {
      streamMsg._thinking = false;
      cot.finish(secs);
      streamMsg.reasoning_secs = secs || Math.round((Date.now() - rStart) / 1000);
    }
  };
  let lastPaint = 0;
  let full = "";
  let fullReasoning = "";
  const paint = (force = false) => {
    const now = Date.now();
    if (!force && now - lastPaint < 150) return;
    lastPaint = now;
    aiRenderMd(body, full, { done: false });
    body.appendChild(aiCursorEl());
    if (aiState.autoScroll) aiScrollBottomNow(false);
  };
  try {
    if (!resp.ok) {
      const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
      throw new Error(detail);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let got = false;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        if (!part.startsWith("data: ")) continue;
        const payload = part.slice(6);
        if (payload === "[DONE]") continue;
        let d;
        try { d = JSON.parse(payload); } catch { continue; }
        if (d.error) throw new Error(d.error);
        if (d.reasoning) {
          if (!cot) {
            startThinking();
            body.innerHTML = "";
          }
          fullReasoning += d.reasoning;
          cot.onReasoning(d.reasoning);
          if (aiState.autoScroll) aiScrollBottomNow(false);
        }
        if (d.reasoning_secs !== undefined) {
          stopThinking(d.reasoning_secs);
        }
        if (d.delta) {
          if (!got) {
            got = true;
            body.innerHTML = "";
            if (cot) stopThinking(d.reasoning_secs);
          }
          full += d.delta;
          paint();
        }
        if (d.message_id) streamMsg.id = d.message_id;
        if (d.title) {
          $("aiChatTitle").textContent = d.title;
          const s = aiState.sessions.find((x) => x.id === aiState.sessionId);
          if (s) { s.title = d.title; aiRenderSessions(); }
        }
      }
    }
    // 收尾
    stopThinking();
    streamMsg._streaming = false;
    streamMsg.content = full || streamMsg.content;
    streamMsg.reasoning = fullReasoning || streamMsg.reasoning;
    streamMsg.created_at = new Date().toISOString().slice(0, 19);
    const idx = aiState.messages.indexOf(streamMsg);
    aiReplaceStreamEl(streamEl, streamMsg, idx === aiState.messages.length - 1);
    if (aiState.autoScroll) aiScrollBottomNow();
    aiLoadSessions();
  } catch (err) {
    if (err.name === "AbortError") {
      if (full || fullReasoning) {
        streamMsg._streaming = false;
        streamMsg.content = full;
        streamMsg.reasoning = fullReasoning;
        await aiReloadMessages();
      } else {
        streamEl.remove();
        aiState.messages.pop();
        aiRenderPlaceholder();
      }
      aiToast("已停止生成");
    } else {
      stopThinking();
      streamMsg._streaming = false;
      streamMsg.content = full;
      streamMsg.reasoning = fullReasoning || streamMsg.reasoning;
      streamMsg._error = err.message;
      aiReplaceStreamEl(streamEl, streamMsg, true);
    }
  } finally {
    aiState.busy = false;
    aiAbort = null;
    aiSetSendStop(false);
  }
}
async function aiSend() {
  if (aiState.busy) return;
  const content = aiAutoFormula($("aiInput").value.trim());
  if (!content && !aiPendingMedia.length) return;
  aiState.busy = true;
  aiAbort = new AbortController();
  aiSetSendStop(true);
  // 无会话则自动创建
  if (!aiState.sessionId) {
    const d = await api("/chats", { method: "POST", body: JSON.stringify({}) });
    aiState.sessionId = d.id;
  }
  const userMedia = aiPendingMedia.map((m) => ({ url: m.url, name: m.name }));
  aiPendingMedia.length = 0;
  aiRenderMediaChips();
  // 保留展示用元数据（q/sources/title/name），剔除内部标记
  const contexts = aiCtx.map(({ via, ...rest }) => rest);
  const userMsg = { id: null, role: "user", content, media: userMedia, contexts, created_at: new Date().toISOString().slice(0, 19) };
  aiState.messages.push(userMsg);
  const box = $("aiMessages");
  box.appendChild(aiBuildMsg(userMsg, false));
  $("aiInput").value = "";
  aiGrowInput();
  aiUpdateSendState();
  aiUpdateMathPreview();
  if (aiState.autoScroll) aiScrollBottomNow();
  try {
    const resp = await aiStreamRequest({ content, contexts, media: userMedia });
    aiCtx.length = 0;  // context 一次性：随本条消息消费
    aiRenderCtxChips();
    await aiConsumeStream(resp);
  } catch (err) {
    aiState.busy = false;
    aiAbort = null;
    aiSetSendStop(false);
    if (err.name !== "AbortError") {
      userMsg._error = err.message;
      aiRenderAll();
    }
  }
}
async function aiRegenerate() {
  if (aiState.busy || !aiState.sessionId) return;
  const last = aiState.messages[aiState.messages.length - 1];
  if (!last || last.role !== "assistant") return;
  aiState.busy = true;
  aiAbort = new AbortController();
  aiSetSendStop(true);
  aiState.messages.pop();  // 本地先移除，服务端 regenerate 同步删除
  [...$("aiMessages").querySelectorAll(".ai-msg")].slice(-1).forEach((n) => n.remove());
  try {
    const resp = await aiStreamRequest({ regenerate: true });
    await aiConsumeStream(resp, { regenerate: true });
  } catch (err) {
    aiState.busy = false;
    aiAbort = null;
    aiSetSendStop(false);
    if (err.name !== "AbortError") flash("重新生成失败：" + err.message);
  }
}
async function aiContinue(msg) {
  if (aiState.busy || !aiState.sessionId) return;
  aiState.busy = true;
  aiAbort = new AbortController();
  aiSetSendStop(true);
  const msgEl = document.querySelector(`.ai-msg[data-id="${msg.id}"]`);
  const body = msgEl ? msgEl.querySelector(".ai-body") : null;
  let full = msg.content;
  let lastPaint = 0;
  try {
    const resp = await aiStreamRequest({ continuation: true });
    if (!resp.ok) {
      const detail = (await resp.json().catch(() => ({}))).detail || resp.statusText;
      throw new Error(detail);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        if (!part.startsWith("data: ")) continue;
        const payload = part.slice(6);
        if (payload === "[DONE]") continue;
        let d;
        try { d = JSON.parse(payload); } catch { continue; }
        if (d.error) throw new Error(d.error);
        if (d.delta) {
          full += d.delta;
          const now = Date.now();
          if (now - lastPaint > 150 && body) {
            lastPaint = now;
            aiRenderMd(body, full, { done: false });
            body.appendChild(aiCursorEl());
            if (aiState.autoScroll) aiScrollBottomNow(false);
          }
        }
      }
    }
    msg.content = full;
    await aiReloadMessages();
    if (aiState.autoScroll) aiScrollBottomNow();
  } catch (err) {
    if (err.name !== "AbortError") flash("继续生成失败：" + err.message);
    else aiToast("已停止生成");
  } finally {
    aiState.busy = false;
    aiAbort = null;
    aiSetSendStop(false);
  }
}

/* ---------- 输入区交互 ---------- */
let aiMathPreviewTm = 0;
function aiHideMathPreview() {
  clearTimeout(aiMathPreviewTm);
  $("aiMathPreview").classList.add("hidden");
}
// 定位光标所在的 LaTeX 公式（光标不在公式内则返回 null）
function aiFormulaAtCaret() {
  const ta = $("aiInput");
  const pos = ta.selectionStart;
  const t = ta.value;
  const ranges = [];
  const push = (re, display) => {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(t))) {
      if (m[1] !== undefined) ranges.push({ s: m.index, e: m.index + m[0].length, expr: m[1], display });
    }
  };
  push(/\$\$([\s\S]+?)\$\$/g, true);
  push(/(?<!\$)\$([^$\n]+?)\$(?!\$)/g, false);
  for (const r of ranges) {
    if (pos > r.s && pos < r.e) return r;
  }
  // 无定界符但整段是裸 LaTeX → 整段视为一个公式（与发送兜底一致）
  if (!t.includes("$") && /\[a-zA-Z]{2,}/.test(t) && /[_^]/.test(t)) {
    return { s: 0, e: t.length, expr: t, display: true };
  }
  return null;
}
function aiUpdateMathPreview() {
  const box = $("aiMathPreview");
  const hit = aiFormulaAtCaret();
  if (!hit) {
    box.classList.add("hidden");
    return;
  }
  clearTimeout(aiMathPreviewTm);
  aiMathPreviewTm = setTimeout(() => {
    const tex = hit.display ? "$$" + hit.expr + "$$" : "$" + hit.expr + "$";
    aiRenderMd(box.querySelector(".ai-math-preview-body"), tex, { done: true });
    box.classList.remove("hidden");
  }, 100);
}
$("aiInput").addEventListener("input", () => { aiGrowInput(); aiUpdateSendState(); aiUpdateMathPreview(); });
$("aiInput").addEventListener("keyup", (e) => {
  if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(e.key)) {
    aiUpdateMathPreview();
  }
});
$("aiInput").addEventListener("click", () => aiUpdateMathPreview());
$("aiInput").addEventListener("blur", () => aiHideMathPreview());
$("aiInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (aiState.busy) return;
    aiSend();
  } else if (e.key === "Escape") {
    if (aiState.busy && aiAbort) { aiAbort.abort(); }
  } else if (e.key === "ArrowUp") {
    const ta = $("aiInput");
    if (ta.value) return;
    const lastUser = [...aiState.messages].reverse().find((m) => m.role === "user");
    if (lastUser) { e.preventDefault(); aiEditUser(lastUser); }
  }
});
$("aiSend").onclick = () => {
  if (aiState.busy) { if (aiAbort) aiAbort.abort(); return; }
  aiSend();
};

/* ---------- 图片附件 ---------- */
function aiRenderMediaChips() {
  const box = $("aiMediaChips");
  box.innerHTML = aiPendingMedia
    .map(
      (m, i) =>
        `<div class="ai-chip-img"><img src="${esc(m.url)}" data-i="${i}" title="${esc(m.name || "")}（点击标注）" style="cursor:zoom-in">` +
        `<button class="ai-chip-x" data-i="${i}" title="移除"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="m6 6 12 12M18 6 6 18"/></svg></button></div>`
    )
    .join("");
  box.querySelectorAll(".ai-chip-x").forEach((x) => {
    x.onclick = () => {
      aiPendingMedia.splice(Number(x.dataset.i), 1);
      aiRenderMediaChips();
    };
  });
  box.querySelectorAll("img").forEach((img) => {
    img.onclick = () => aiEditorOpen(Number(img.dataset.i));
  });
}
$("aiImgBtn").onclick = () => $("aiImgInput").click();
$("aiImgInput").onchange = aiUploadImages;
async function aiUploadImages() {
  const files = [...$("aiImgInput").files];
  $("aiImgInput").value = "";
  for (const f of files) {
    if (!f.type.startsWith("image/")) { flash(`暂不支持非图片文件：${f.name}`); continue; }
    if (f.size > 20 * 1024 * 1024) { flash(`图片 ${f.name} 超过 20MB 上限`); continue; }
    try {
      // 后端为原始字节流端点（无 python-multipart），必须直发文件字节而非 FormData
      const resp = await fetch("/api/ai-media/upload?filename=" + encodeURIComponent(f.name), {
        method: "POST",
        headers: { "Content-Type": f.type || "application/octet-stream" },
        body: await f.arrayBuffer(),
      });
      if (!resp.ok) throw new Error(await resp.text());
      aiPendingMedia.push(await resp.json());
      aiRenderMediaChips();
    } catch (err) {
      flash(`图片上传失败：${err.message}`);
    }
  }
  aiUpdateSendState();
}
// 粘贴图片直达
$("aiInput").addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
  if (!files.length) return;
  e.preventDefault();
  const dt = new DataTransfer();
  files.forEach((f) => dt.items.add(f));
  $("aiImgInput").files = dt.files;
  aiUploadImages();
});
// 拖放图片
const aiChatCol = document.querySelector(".ai-chat-col");
let aiDragDepth = 0;
aiChatCol.addEventListener("dragenter", (e) => {
  if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
  aiDragDepth++;
  $("aiDropOverlay").classList.remove("hidden");
});
aiChatCol.addEventListener("dragleave", () => {
  if (--aiDragDepth <= 0) { aiDragDepth = 0; $("aiDropOverlay").classList.add("hidden"); }
});
aiChatCol.addEventListener("dragover", (e) => e.preventDefault());
aiChatCol.addEventListener("drop", async (e) => {
  e.preventDefault();
  aiDragDepth = 0;
  $("aiDropOverlay").classList.add("hidden");
  const files = [...(e.dataTransfer?.files || [])].filter((f) => f.type.startsWith("image/"));
  if (!files.length) return;
  const dt = new DataTransfer();
  files.forEach((f) => dt.items.add(f));
  $("aiImgInput").files = dt.files;
  await aiUploadImages();
});

/* ---------- 上下文 chips（输入框上方，一次性，随下一条消息发送） ---------- */
function aiCtxChipLabel(c) {
  if (c.type === "list") {
    const seg = [];
    if (c.q) seg.push("“" + c.q + "”");
    if (c.sources != null) seg.push(c.sources + " sources");
    if (c.count != null) seg.push(c.count + "条");
    return "列表：" + (seg.join(" | ") || "全部");
  }
  if (c.type === "item") {
    const t = c.title || "条目";
    return t.length > 24 ? t.slice(0, 24) + "…" : t;
  }
  return c.name || "文档";
}
function aiRenderCtxChips() {
  const box = $("aiCtxChips");
  box.innerHTML = "";
  for (const c of aiCtx) {
    const chip = document.createElement("div");
    chip.className = "ai-ctx-chip";
    chip.title = "点击预览内容，× 移除";
    const label = document.createElement("span");
    label.className = "ai-ctx-chip-label";
    label.textContent = aiCtxChipLabel(c);
    label.onclick = () => aiPreviewContext([c]);
    const x = document.createElement("button");
    x.className = "ai-chip-x";
    x.title = "移除";
    x.innerHTML =
      '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="m6 6 12 12M18 6 6 18"/></svg>';
    x.onclick = (e) => {
      e.stopPropagation();
      aiCtx = aiCtx.filter((y) => y !== c);
      aiRenderCtxChips();
    };
    chip.append(label, x);
    box.appendChild(chip);
  }
  $("aiAddListBtn").classList.toggle("on", aiCtx.some((c) => c.type === "list"));
  $("aiAddItemBtn").classList.toggle(
    "on",
    aiCtx.some((c) => c.type === "item" || c.type === "doc")
  );
}
function aiCtxAdd(entry) {
  if (entry.type === "list") {
    if (aiCtx.some((c) => c.type === "list")) return;
  } else if (aiCtx.some((c) => c.type === entry.type && String(c.id) === String(entry.id))) {
    return flash("该上下文已在列表中");
  }
  aiCtx.push(entry);
  aiRenderCtxChips();
}

$("aiAddListBtn").onclick = async () => {
  const f = state.feed;
  const params = new URLSearchParams();
  if (f.q) params.set("q", f.q);
  params.set("mode", f.mode || "semantic");
  if (f.sourceIds.length) params.set("source_ids", f.sourceIds.join(","));
  if (f.types.length) params.set("types", f.types.join(","));
  if (f.unread) params.set("unread", "true");
  if (f.starred) params.set("starred", "true");
  if (f.bookmarked) params.set("bookmarked", "true");
  let d;
  try {
    d = await api("/chats/list-context?" + params.toString());
  } catch (e) {
    return flash("列表上下文构建失败：" + e.message);
  }
  if (!d.count) return flash("当前列表为空");
  const old = aiCtx.find((c) => c.type === "list");
  if (old && old.text === d.text) return flash("列表上下文已附加");
  if (old) aiCtx = aiCtx.filter((c) => c.type !== "list");  // 同一列表的新快照替换旧快照
  aiCtxAdd({
    type: "list",
    count: d.count,
    text: d.text,
    q: d.q,
    sources: d.sources,
  });
};
$("aiAddItemBtn").onclick = () => {
  const d = $("detail");
  if (d.classList.contains("hidden") || !d.dataset.iid) return flash("请先在详情中打开条目");
  const docs = (aiDetailAssets || []).filter(
    (a) => ["pdf", "md", "html", "txt"].includes(a.kind) || (a.mime || "").startsWith("text/")
  );
  if (docs.length) {
    // 已归档：附加条目的全部可注入文档（跳过已附加的）
    const fresh = docs.filter(
      (a) => !aiCtx.some((c) => c.type === "doc" && String(c.id) === String(a.id))
    );
    if (!fresh.length) return flash("该条目的文档均已附加");
    for (const a of fresh) aiCtxAdd({ type: "doc", id: a.id, name: a.name || a.kind });
  } else {
    // 未归档：附加条目摘要
    aiCtxAdd({
      type: "item",
      id: Number(d.dataset.iid),
      title: (aiDetailItem && aiDetailItem.id === Number(d.dataset.iid) ? aiDetailItem.title : "") || "条目 " + d.dataset.iid,
    });
  }
};

/* ---------- 会话侧栏 ---------- */
async function aiLoadSessions() {
  const d = await api("/chats");
  aiState.sessions = d.sessions;
  if (d.model) aiState.model = d.model;
  aiRenderSessions();
}
function aiSessionMenu(s, anchor) {
  const items = [
    {
      label: s.pinned ? "取消置顶" : "置顶",
      icon: s.pinned ? AI_ICONS.pinOff : AI_ICONS.pin,
      fn: async () => {
        await api("/chats/" + s.id, { method: "PATCH", body: JSON.stringify({ pinned: !s.pinned }) });
        await aiLoadSessions();
      },
    },
    {
      label: "重命名",
      icon: AI_ICONS.pen,
      fn: () => aiRenameSession(s),
    },
    {
      label: s.archived ? "取消归档" : "归档",
      icon: AI_ICONS.archive,
      fn: async () => {
        await api("/chats/" + s.id, { method: "PATCH", body: JSON.stringify({ archived: !s.archived }) });
        await aiLoadSessions();
        aiToast(s.archived ? "已取消归档" : "会话已归档");
      },
    },
    {
      label: "克隆",
      icon: AI_ICONS.clone,
      fn: async () => {
        const d = await api(`/chats/${s.id}/clone`, { method: "POST" });
        await aiLoadSessions();
        aiToast("已创建副本");
      },
    },
    "-",
    {
      label: "删除",
      icon: AI_ICONS.trash,
      danger: true,
      fn: async () => {
        const ok = await aiConfirm("删除会话？", `这将删除「${s.title}」。`);
        if (!ok) return;
        await api("/chats/" + s.id, { method: "DELETE" });
        if (aiState.sessionId === s.id) {
          aiState.sessionId = null;
          aiState.messages = [];
          $("aiChatTitle").textContent = "新对话";
          aiRenderAll();
        }
        aiLoadSessions();
      },
    },
  ];
  aiPopMenu(items, anchor);
}
function aiRenameSession(s) {
  const row = document.querySelector(`.ai-sess-item[data-id="${s.id}"]`);
  if (!row) return;
  const orig = row.innerHTML;
  row.classList.add("menu-open");
  row.innerHTML = "";
  const input = document.createElement("input");
  input.className = "ai-sess-input";
  input.value = s.title;
  row.appendChild(input);
  input.focus();
  input.select();
  let doneFlag = false;
  const commit = async (save) => {
    if (doneFlag) return;
    doneFlag = true;
    const v = input.value.trim();
    if (save && v && v !== s.title) {
      await api("/chats/" + s.id, { method: "PATCH", body: JSON.stringify({ title: v }) });
      s.title = v;
      if (aiState.sessionId === s.id) $("aiChatTitle").textContent = v;
      await aiLoadSessions();
    } else {
      row.innerHTML = orig;
      row.classList.remove("menu-open");
      row._bind();
    }
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter") commit(true);
    else if (e.key === "Escape") commit(false);
    else if (e.isComposing || e.keyCode === 229) return;
  };
  input.onblur = () => commit(true);
}
function aiRenderSessions() {
  const list = $("aiSessionList");
  list.innerHTML = "";
  const q = aiState.searchQ.trim().toLowerCase();
  let sessions = aiState.sessions;
  if (q) sessions = sessions.filter((s) => (s.title || "").toLowerCase().includes(q));
  const active = sessions.filter((s) => !s.archived);
  const archived = sessions.filter((s) => s.archived);

  // 置顶组
  const pinned = active.filter((s) => s.pinned);
  if (pinned.length && !q) {
    const head = document.createElement("button");
    head.className = "ai-sess-pin-head" + (aiState.pinOpen ? "" : " closed");
    head.innerHTML =
      '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>' +
      "<span>置顶</span>";
    head.onclick = () => {
      aiState.pinOpen = !aiState.pinOpen;
      localStorage.setItem("da_ai_pin_open", aiState.pinOpen ? "1" : "0");
      aiRenderSessions();
    };
    list.appendChild(head);
    if (aiState.pinOpen) {
      const wrap = document.createElement("div");
      wrap.className = "ai-sess-pin-items";
      for (const s of pinned) wrap.appendChild(aiSessItem(s));
      list.appendChild(wrap);
    }
  }
  // 日期分组（时间倒序已是默认）
  let lastRange = null;
  for (const s of active) {
    if (s.pinned && !q) continue;
    const range = q ? null : aiTimeRange(s.updated_at);
    if (range && range !== lastRange) {
      const h = document.createElement("div");
      h.className = "ai-sess-group" + (lastRange ? " gap" : "");
      h.textContent = range;
      list.appendChild(h);
      lastRange = range;
    }
    list.appendChild(aiSessItem(s));
  }
  // 归档组
  if (archived.length) {
    const h = document.createElement("div");
    h.className = "ai-sess-group gap";
    h.textContent = "已归档";
    list.appendChild(h);
    for (const s of archived) list.appendChild(aiSessItem(s));
  }
  if (!list.children.length) {
    const d = document.createElement("div");
    d.className = "ai-sess-empty";
    d.textContent = q ? "没有匹配的会话" : "暂无会话";
    list.appendChild(d);
  }
}
function aiSessItem(s) {
  const d = document.createElement("div");
  d.className = "ai-sess-item" + (s.id === aiState.sessionId ? " active" : "");
  d.dataset.id = s.id;
  d.innerHTML =
    `<span class="ai-sess-title">${esc(s.title || "新对话")}</span>` +
    `<span class="ai-sess-ago">${aiFmtAgo(s.updated_at)}</span>` +
    `<span class="ai-sess-acts"><button class="ai-icon-btn" title="更多">${AI_ICONS.more}</button></span>`;
  d.onclick = () => aiLoadSession(s.id);
  d.ondblclick = () => aiRenameSession(s);
  d.querySelector(".ai-sess-acts .ai-icon-btn").onclick = (e) => {
    e.stopPropagation();
    aiSessionMenu(s, e.currentTarget);
  };
  d._bind = () => {
    d.querySelector(".ai-sess-acts .ai-icon-btn").onclick = (e) => {
      e.stopPropagation();
      aiSessionMenu(s, e.currentTarget);
    };
  };
  return d;
}

$("aiSearch").addEventListener("input", () => {
  aiState.searchQ = $("aiSearch").value;
  $("aiSearchClear").classList.toggle("hidden", !aiState.searchQ);
  aiRenderSessions();
});
$("aiSearchClear").onclick = () => {
  $("aiSearch").value = "";
  aiState.searchQ = "";
  $("aiSearchClear").classList.add("hidden");
  aiRenderSessions();
};

$("aiNewChat").onclick = async () => {
  const d = await api("/chats", { method: "POST", body: JSON.stringify({}) });
  aiState.sessionId = d.id;
  aiState.messages = [];
  $("aiChatTitle").textContent = "新对话";
  aiRenderAll();
  await aiLoadSessions();
  $("aiInput").focus();
};

/* ---------- 顶栏菜单（导出等） ---------- */
function aiChatAsText(fmt) {
  const lines = [`# ${$("aiChatTitle").textContent || "对话"}`, ""];
  for (const m of aiState.messages) {
    if (fmt === "txt") {
      lines.push(m.role === "user" ? "【用户】" : "【AI 助手】", m.content, "");
    } else {
      lines.push(m.role === "user" ? `**用户：**` : `**AI 助手：**`, "", m.content, "", "---", "");
    }
  }
  return lines.join("\n");
}
$("aiChatMenuBtn").onclick = (e) => {
  const s = aiState.sessions.find((x) => x.id === aiState.sessionId);
  const items = [
    { label: "复制", icon: AI_ICONS.copy, fn: () => aiCopy(aiChatAsText("md")) },
    "-",
    { label: "导出 JSON", icon: AI_ICONS.dl, fn: () => aiDownload("chat.json", JSON.stringify(aiState.messages, null, 2), "application/json") },
    { label: "导出 Markdown", icon: AI_ICONS.dl, fn: () => aiDownload("chat.md", aiChatAsText("md"), "text/markdown") },
    { label: "导出纯文本", icon: AI_ICONS.dl, fn: () => aiDownload("chat.txt", aiChatAsText("txt")) },
  ];
  if (s) {
    items.push(
      "-",
      {
        label: s.pinned ? "取消置顶" : "置顶",
        icon: s.pinned ? AI_ICONS.pinOff : AI_ICONS.pin,
        fn: async () => {
          await api("/chats/" + s.id, { method: "PATCH", body: JSON.stringify({ pinned: !s.pinned }) });
          aiLoadSessions();
        },
      },
      {
        label: s.archived ? "取消归档" : "归档",
        icon: AI_ICONS.archive,
        fn: async () => {
          await api("/chats/" + s.id, { method: "PATCH", body: JSON.stringify({ archived: !s.archived }) });
          if (!s.archived && aiState.sessionId === s.id) {
            aiState.sessionId = null;
            aiState.messages = [];
            aiRenderAll();
          }
          aiLoadSessions();
          aiToast(s.archived ? "已取消归档" : "会话已归档");
        },
      },
      {
        label: "删除会话",
        icon: AI_ICONS.trash,
        danger: true,
        fn: async () => {
          const ok = await aiConfirm("删除会话？", `这将删除「${s.title}」。`);
          if (!ok) return;
          await api("/chats/" + s.id, { method: "DELETE" });
          aiState.sessionId = null;
          aiState.messages = [];
          $("aiChatTitle").textContent = "新对话";
          aiRenderAll();
          aiLoadSessions();
        },
      }
    );
  }
  aiPopMenu(items, e.currentTarget);
};

/* ---------- 面板开合 / 拖拽调高 ---------- */
function aiSetH(h) {
  // 高度统一走 --ai-h 变量：AI 栈自身与上方 UI（详情面板可视高度）联动
  const stack = $("aiStack");
  stack.style.height = h;
  document.documentElement.style.setProperty("--ai-h", h);
  if (h !== "42px") localStorage.setItem("da_ai_h", h);
}
function aiOpenStack() {
  const stack = $("aiStack");
  stack.classList.remove("hidden");
  aiState.open = true;
  document.body.classList.add("ai-open");
  $("aiStackBtn").classList.add("on");
  const saved = parseFloat(localStorage.getItem("da_ai_h")) || 0;
  // 上方 UI 至少保留 240px 可视区
  const maxH = window.innerHeight - 240;
  aiSetH(saved > 200 && saved <= maxH ? saved + "px" : Math.min(window.innerHeight * 0.42, maxH) + "px");
  aiLoadSessions().then(() => {
    if (!aiState.sessionId && aiState.sessions.length) aiLoadSession(aiState.sessions[0].id, false);
  });
  $("aiInput").focus();
}
function aiCloseStack() {
  $("aiStack").classList.add("hidden");
  aiState.open = false;
  document.body.classList.remove("ai-open");
  $("aiStackBtn").classList.remove("on");
  if ("speechSynthesis" in window) speechSynthesis.cancel();
}
$("aiStackBtn").onclick = () => (aiState.open ? aiCloseStack() : aiOpenStack());
$("aiStackClose").onclick = aiCloseStack;
$("aiStackMin").onclick = () => {
  const stack = $("aiStack");
  const cur = stack.getBoundingClientRect().height;
  if (cur > 60) {
    stack.dataset.prevH = cur + "px";
    aiSetH("42px");
    stack.classList.add("minimized");
  } else {
    aiSetH(stack.dataset.prevH || "40vh");
    stack.classList.remove("minimized");
  }
};
$("aiSessionsToggle").onclick = () => {
  if (matchMedia("(max-width: 900px)").matches) {
    $("aiSessionsCol").classList.toggle("force-show");
  } else {
    const col = $("aiSessionsCol");
    col.style.display = col.style.display === "none" ? "" : "none";
  }
};
// 拖柄拖拽调高（200px ~ 80vh），双击收起/展开；高度存 localStorage
(() => {
  const handle = $("aiStackHandle");
  const stack = $("aiStack");
  let startY = 0, startH = 0, dragging = false;
  handle.addEventListener("mousedown", (e) => {
    dragging = true;
    startY = e.clientY;
    startH = stack.getBoundingClientRect().height;
    document.body.classList.add("ai-dragging");  // 拖拽期间屏蔽 iframe 吞事件（如 PDF 预览）
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const maxH = window.innerHeight - 240;  // 上方 UI 至少保留 240px
    const h = Math.max(200, Math.min(maxH, startH + (startY - e.clientY)));
    aiSetH(h + "px");
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove("ai-dragging");
  });
  handle.addEventListener("dblclick", () => {
    const cur = stack.getBoundingClientRect().height;
    if (cur > 60) {
      aiSetH("42px");
      stack.classList.add("minimized");
    } else {
      aiSetH(localStorage.getItem("da_ai_h") || "40vh");
      stack.classList.remove("minimized");
    }
  });
})();

/* ---------- 选中文本 → 🔍 定位 Feed 卡片 ---------- */
const selFindBtn = document.createElement("button");
selFindBtn.id = "selFindBtn";
selFindBtn.title = "在列表中查找匹配的条目";
selFindBtn.textContent = "🔍";
document.body.appendChild(selFindBtn);
let selFindText = "";
let selFindTm = 0;
function selFindHide() {
  clearTimeout(selFindTm);
  selFindBtn.style.display = "none";
}
document.addEventListener("selectionchange", () => {
  clearTimeout(selFindTm);
  selFindTm = setTimeout(() => {
    if (!aiState.open) return selFindHide();
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return selFindHide();
    const range = sel.getRangeAt(0);
    if (!range.commonAncestorContainer.parentElement?.closest("#aiMessages")) return selFindHide();
    const text = sel.toString().trim();
    if (text.length < 2) return selFindHide();
    selFindText = text;
    const r = range.getBoundingClientRect();
    selFindBtn.style.display = "flex";
    selFindBtn.style.left = Math.min(window.innerWidth - 44, r.right + 6) + "px";
    selFindBtn.style.top = Math.max(8, r.top - 36) + "px";
  }, 120);
});
$("aiMessages").addEventListener("scroll", selFindHide, { passive: true });

function normTitle(t) {
  return (t || "").replace(/[《》“”"'‘’\s]+/g, "").toLowerCase();
}
selFindBtn.onclick = () => {
  const target = normTitle(selFindText);
  selFindHide();
  window.getSelection()?.removeAllRanges();
  if (target.length < 2) return;
  let hit = null;
  for (const c of document.querySelectorAll("#itemList .item")) {
    const t1 = normTitle(c.querySelector(".item-title")?.textContent);
    const t2 = normTitle(c.querySelector(".item-title-trans")?.textContent);
    if (
      (t1 && (t1.includes(target) || target.includes(t1))) ||
      (t2 && (t2.includes(target) || target.includes(t2)))
    ) {
      hit = c;
      break;
    }
  }
  if (!hit) return flash("当前列表中没有找到匹配的条目");
  hit.scrollIntoView({ behavior: "smooth", block: "center" });
  // 平滑滚动约 600ms 落定后再播闪烁，避免动效在滚动途中就播完
  setTimeout(() => {
    hit.classList.remove("find-flash");
    void hit.offsetWidth;
    hit.classList.add("find-flash");
    setTimeout(() => hit.classList.remove("find-flash"), 1000);
  }, 650);
};

/* ---------- 会话加载 ---------- */
async function aiLoadSession(id) {
  aiState.sessionId = id;
  const d = await api(`/chats/${id}/messages`);
  aiState.messages = d.messages;
  $("aiChatTitle").textContent = d.session.title || "新对话";
  aiRenderAll();
  aiLoadSessions();
}

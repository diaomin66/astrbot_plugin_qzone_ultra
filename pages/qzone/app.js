const bridge = window.AstrBotPluginPage;
const BRIDGE_READY_TIMEOUT_MS = 5000;
const BRIDGE_REQUEST_TIMEOUT_MS = 60000;
const DETAIL_DRAWER_BREAKPOINT = 1200;

const state = {
  context: null,
  status: null,
  scope: "friends",
  targetUin: "",
  cursor: "",
  hasMore: false,
  posts: [],
  selected: null,
  media: [],
  loading: false,
  pendingLikes: new Set(),
  pendingDeletes: new Set(),
  pendingDeleteConfirms: new Map(),
  knownAuthors: new Map(),
  replyTarget: null,
  replyDrafts: new Map(),
  pendingReplies: new Set(),
  localVersions: new Map(),
  detailRequestSeq: 0,
  detailLoadingId: "",
};

let themeSyncBound = false;

function queryOne(selector) {
  return typeof document.querySelector === "function" ? document.querySelector(selector) : null;
}

function queryAll(selector) {
  return typeof document.querySelectorAll === "function" ? [...document.querySelectorAll(selector)] : [];
}

const el = {
  statusText: document.getElementById("statusText"),
  accountName: document.getElementById("accountName"),
  accountMeta: document.getElementById("accountMeta"),
  accountAvatar: queryOne(".account .avatar"),
  tabs: queryAll(".tab"),
  targetForm: document.getElementById("targetForm"),
  targetUin: document.getElementById("targetUin"),
  publishForm: document.getElementById("publishForm"),
  publishContent: document.getElementById("publishContent"),
  mediaInput: document.getElementById("mediaInput"),
  mediaStrip: document.getElementById("mediaStrip"),
  publishButton: document.getElementById("publishButton"),
  refreshButton: document.getElementById("refreshButton"),
  feedTitle: document.getElementById("feedTitle"),
  feedMeta: document.getElementById("feedMeta"),
  notice: document.getElementById("notice"),
  feed: document.getElementById("feed"),
  moreButton: document.getElementById("moreButton"),
  detailPane: document.getElementById("detailPane"),
  detailEmpty: document.getElementById("detailEmpty"),
  detailContent: document.getElementById("detailContent"),
};

function normalizeThemeHint(value) {
  if (typeof value === "boolean") return value ? "dark" : "light";
  const raw = text(value).trim().toLowerCase();
  if (!raw) return "";
  if (/(^|[\s_-])(dark|night|black)([\s_-]|$)/.test(raw) || raw === "dark" || raw.includes("dark")) return "dark";
  if (/(^|[\s_-])(light|day|white)([\s_-]|$)/.test(raw) || raw === "light" || raw.includes("light")) return "light";
  if (raw.includes("purpletheme")) return "light";
  return "";
}

function themeFromObject(value, depth = 0) {
  const direct = normalizeThemeHint(value);
  if (direct || !value || typeof value !== "object" || depth > 2) return direct;
  for (const key of ["theme", "themeMode", "uiTheme", "colorMode", "colorScheme", "appearance", "mode", "scheme", "name"]) {
    const found = normalizeThemeHint(value[key]);
    if (found) return found;
  }
  if (typeof value.dark === "boolean") return value.dark ? "dark" : "light";
  for (const key of ["context", "settings", "config", "ui", "page", "dashboard"]) {
    const found = themeFromObject(value[key], depth + 1);
    if (found) return found;
  }
  return "";
}

function applyTheme(theme) {
  const root = document.documentElement;
  if (!root || !theme) return;
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
  root.classList?.toggle("v-theme--dark", theme === "dark");
  root.classList?.toggle("v-theme--light", theme === "light");
  document.body?.classList?.toggle("v-theme--dark", theme === "dark");
  document.body?.classList?.toggle("v-theme--light", theme === "light");
}

let themeSyncQueued = false;
function queueThemeSync(hint = null) {
  if (themeSyncQueued) return;
  themeSyncQueued = true;
  const run = () => {
    themeSyncQueued = false;
    syncTheme(hint);
  };
  if (typeof window.requestAnimationFrame === "function") {
    window.requestAnimationFrame(run);
  } else {
    window.setTimeout(run, 0);
  }
}

function syncTheme(hint = null) {
  applyTheme(
    themeFromObject(hint)
    || themeFromObject(state.context)
    || themeFromObject(bridge?.getContext?.())
    || "light"
  );
}

function bindThemeSync() {
  if (themeSyncBound) return;
  themeSyncBound = true;
  syncTheme();
  try {
    const onContext = bridge?.onContext || bridge?.onContextChange;
    onContext?.((nextContext) => {
      state.context = { ...(state.context || {}), ...(nextContext || {}) };
      queueThemeSync(nextContext);
    });
  } catch (_) {
  }
  window.addEventListener?.("message", (event) => {
    const message = event?.data;
    if (!message || typeof message !== "object") return;
    const context = message.channel === "astrbot-plugin-page" && message.kind === "context"
      ? message.context
      : (themeFromObject(message) ? message : null);
    if (!context || typeof context !== "object") return;
    state.context = { ...(state.context || {}), ...(context || {}) };
    queueThemeSync(context);
  });
}

function text(value) {
  return String(value ?? "");
}

function getAvatarUrl(uin) {
  if (!uin) return "";
  return `https://q.qlogo.cn/headimg_dl?dst_uin=${encodeURIComponent(uin)}&spec=100`;
}

function getFallbackAvatarUrl(uin) {
  if (!uin) return "";
  return `https://q1.qlogo.cn/g?b=qq&nk=${encodeURIComponent(uin)}&s=100`;
}

function normalizeAvatarUrl(url) {
  const value = text(url).trim();
  if (!value) return "";
  if (value.startsWith("//")) return `https:${value}`;
  if (value.startsWith("http://")) return `https://${value.slice("http://".length)}`;
  return value;
}

function cleanDisplayText(value) {
  return text(value)
    .replace(/\r\n/g, "\n")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n[ \t]+/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function authorKey(uin) {
  const value = Number(uin || 0);
  if (!Number.isFinite(value) || value <= 0) return "";
  return String(Math.trunc(value));
}

function isGenericNickname(value, uin = 0) {
  const name = text(value).trim();
  const key = authorKey(uin);
  return !name
    || name === "我"
    || name === "用户"
    || name === "QQ空间用户"
    || name === "QQ 空间用户"
    || (key && name === key)
    || /^QQ\s*\d{5,}$/i.test(name)
    || /^\d{5,}$/.test(name);
}

function rememberAuthor(author) {
  const key = authorKey(author?.uin);
  if (!key) return;
  const nickname = text(author?.nickname).trim();
  if (isGenericNickname(nickname, key)) return;
  const remembered = { ...(state.knownAuthors.get(key) || {}), ...author, uin: Number(key), nickname };
  state.knownAuthors.set(key, remembered);

  const login = state.status?.login;
  if (login && authorKey(login.uin) === key && isGenericNickname(login.nickname, key)) {
    login.nickname = nickname;
  }
}

function rememberPostAuthors(post) {
  if (!post) return;
  rememberAuthor(post.author);
  for (const comment of post.comments || []) {
    rememberAuthor(comment.author);
  }
}

function rememberPosts(posts) {
  for (const post of posts || []) {
    rememberPostAuthors(post);
  }
}

function postKey(post) {
  const author = post?.author || {};
  return [
    text(post?.id),
    authorKey(author.uin || author.id),
    text(post?.created_at),
    text(post?.content),
  ].join("|");
}

function dedupePosts(posts) {
  const seen = new Set();
  const result = [];
  for (const post of posts || []) {
    const key = postKey(post);
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(post);
  }
  return result;
}

function mergeFeedPosts(currentPosts, incomingPosts) {
  const merged = dedupePosts(currentPosts);
  const seen = new Set(merged.map(postKey));
  for (const post of incomingPosts || []) {
    const key = postKey(post);
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(post);
  }
  return merged;
}

function loginDisplayName() {
  const login = state.status?.login || {};
  const key = authorKey(login.uin);
  const known = key ? state.knownAuthors.get(key) : null;
  if (!isGenericNickname(login.nickname, key)) return text(login.nickname).trim();
  if (known && !isGenericNickname(known.nickname, key)) return known.nickname;
  if (login.bound || key) return "我";
  return "未绑定";
}

function currentLoginAuthor() {
  const login = state.status?.login || {};
  return {
    uin: login.uin || 0,
    nickname: loginDisplayName(),
    avatar: login.avatar || "",
  };
}

function displayAuthorName(author, fallback = "QQ空间用户") {
  const key = authorKey(author?.uin);
  const known = key ? state.knownAuthors.get(key) : null;
  const login = state.status?.login || {};
  const loginKey = authorKey(login.uin);
  const name = text(author?.nickname).trim();

  if (key && loginKey && key === loginKey) {
    return loginDisplayName();
  }
  if (!isGenericNickname(name, key)) return name;
  if (known && !isGenericNickname(known.nickname, key)) return known.nickname;
  return fallback;
}

function mergeAuthor(base, patch) {
  const merged = { ...(base || {}), ...(patch || {}) };
  const key = authorKey(merged.uin || base?.uin || patch?.uin);
  if (isGenericNickname(merged.nickname, key) && !isGenericNickname(base?.nickname, key)) {
    merged.nickname = base.nickname;
  }
  if (!merged.avatar && base?.avatar) {
    merged.avatar = base.avatar;
  }
  return merged;
}

function commentKey(comment) {
  const id = text(comment?.id || comment?.commentid).trim();
  if (id) return `id:${id}`;
  return [
    "body",
    authorKey(comment?.author?.uin),
    text(comment?.content).trim(),
    text(comment?.created_at),
  ].join(":");
}

function mergeComment(base, patch) {
  const merged = { ...(base || {}), ...(patch || {}) };
  if (base?.author || patch?.author) {
    merged.author = mergeAuthor(base?.author, patch?.author);
  }
  if (!cleanDisplayText(merged.content) && cleanDisplayText(base?.content)) {
    merged.content = base.content;
  }
  return merged;
}

function mergeCommentsPreservingLocal(localComments = [], remoteComments = []) {
  const localByKey = new Map();
  for (const item of localComments || []) {
    localByKey.set(commentKey(item), item);
  }

  const merged = [];
  const seen = new Set();
  for (const remote of remoteComments || []) {
    const key = commentKey(remote);
    seen.add(key);
    merged.push(mergeComment(localByKey.get(key), remote));
  }
  for (const local of localComments || []) {
    const key = commentKey(local);
    if (!seen.has(key)) {
      merged.push(local);
    }
  }
  return merged;
}

function localVersion(id) {
  return Number(state.localVersions.get(id) || 0);
}

function markLocalChange(id) {
  if (!id) return;
  state.localVersions.set(id, localVersion(id) + 1);
}

function mergeRemotePost(id, remotePost, versionAtRequestStart) {
  const current = currentPostById(id || remotePost?.id);
  if (!current || localVersion(current.id) === versionAtRequestStart) {
    return remotePost;
  }

  const merged = mergePost(remotePost, {
    liked: current.liked,
    stats: current.stats,
    comments: mergeCommentsPreservingLocal(current.comments || [], remotePost.comments || []),
  });
  merged.id = remotePost.id;
  return merged;
}

function avatarUrlsFor(author) {
  const urls = [
    normalizeAvatarUrl(author?.avatar),
    getAvatarUrl(author?.uin),
    getFallbackAvatarUrl(author?.uin),
  ].filter(Boolean);
  return [...new Set(urls)];
}

function renderAvatar(target, author, fallbackName = "Q") {
  const initial = text(fallbackName || "Q").trim().slice(0, 1).toUpperCase() || "Q";
  const fallback = () => {
    target.classList.remove("has-image");
    target.replaceChildren();
    target.textContent = initial;
  };
  const urls = avatarUrlsFor(author);
  if (!urls.length) {
    fallback();
    return;
  }

  let index = 0;
  const img = document.createElement("img");
  img.alt = "";
  img.loading = "lazy";
  img.decoding = "async";
  img.onerror = () => {
    index += 1;
    if (index < urls.length) {
      img.src = urls[index];
      return;
    }
    fallback();
  };
  target.textContent = "";
  target.classList.add("has-image");
  target.replaceChildren(img);
  img.src = urls[index];
}

function mergePost(base, patch) {
  const merged = { ...(base || {}), ...(patch || {}) };
  if (base?.author || patch?.author) {
    merged.author = mergeAuthor(base?.author, patch?.author);
  }
  if (base?.stats || patch?.stats) {
    merged.stats = { ...(base?.stats || {}), ...(patch?.stats || {}) };
  }
  return merged;
}

function updatePost(id, patch) {
  let updated = null;
  state.posts = state.posts.map((item) => {
    if (item.id !== id) return item;
    updated = mergePost(item, patch);
    return updated;
  });
  if (state.selected?.id === id) {
    state.selected = mergePost(state.selected, patch);
    updated = state.selected;
  }
  return updated;
}

function currentPostById(id, fallback = null) {
  return state.posts.find((item) => item.id === id)
    || (state.selected?.id === id ? state.selected : null)
    || fallback;
}

function renderSelectedIfNeeded(id) {
  if (state.selected?.id === id) {
    renderDetail(state.selected);
  }
}


function showToast(message, tone = "info") {
  const container = document.getElementById("toastContainer");
  if (!message) return;
  if (!container) {
    if (el.notice) {
      el.notice.hidden = false;
      el.notice.textContent = message;
      el.notice.dataset.tone = tone;
    }
    return;
  }

  const toast = document.createElement("div");
  toast.className = `toast ${tone}`;
  toast.textContent = message;

  container.appendChild(toast);

  setTimeout(() => {
    toast.classList.add("fade-out");
    toast.addEventListener("animationend", () => {
      toast.remove();
    });
  }, 3000);
}

function setNotice(message, tone = "info") {
  if (message) {
    showToast(message, tone);
  }
}


async function withTimeout(promise, timeoutMs, message) {
  let timeoutId;
  const timeout = new Promise((_, reject) => {
    timeoutId = window.setTimeout(() => reject(new Error(message)), timeoutMs);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function normalizeBridgeResult(result, fallbackMessage) {
  if (result && typeof result === "object") {
    if (result.status === "error") {
      throw new Error(result.message || fallbackMessage);
    }
    if (Object.prototype.hasOwnProperty.call(result, "ok")) {
      if (!result.ok) {
        throw new Error(result.error?.message || result.message || fallbackMessage);
      }
      return result.data || {};
    }
  }
  return result || {};
}

async function apiGet(endpoint, params = {}) {
  const result = await withTimeout(
    bridge.apiGet(endpoint, params),
    BRIDGE_REQUEST_TIMEOUT_MS,
    "请求 AstrBot WebUI 超时，请刷新页面后重试。"
  );
  return normalizeBridgeResult(result, "请求失败");
}

async function apiPost(endpoint, body = {}) {
  const result = await withTimeout(
    bridge.apiPost(endpoint, body),
    BRIDGE_REQUEST_TIMEOUT_MS,
    "请求 AstrBot WebUI 超时，请刷新页面后重试。"
  );
  return normalizeBridgeResult(result, "请求失败");
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result || "")));
    reader.addEventListener("error", () => reject(new Error("读取媒体失败")));
    reader.readAsDataURL(file);
  });
}

async function uploadPageMedia(file) {
  if (typeof bridge.upload === "function") {
    try {
      const result = await withTimeout(
        bridge.upload("page/upload-media", file),
        BRIDGE_REQUEST_TIMEOUT_MS,
        "上传媒体超时，请刷新页面后重试。"
      );
      return normalizeBridgeResult(result, "上传失败");
    } catch (error) {
      console.warn("qzone page upload bridge failed, falling back to JSON upload", error);
    }
  }
  const dataUrl = await fileToDataUrl(file);
  return apiPost("page/upload-media", {
    name: file.name || "image.png",
    content_type: file.type || "",
    size: file.size || 0,
    data_url: dataUrl,
  });
}

function formatTime(value) {
  const timestamp = Number(value || 0);
  if (!timestamp) return "未知时间";
  const date = new Date(timestamp * 1000);
  if (Number.isNaN(date.getTime())) return "未知时间";
  return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function scopeTitle() {
  if (state.scope === "self") return "我的空间";
  if (state.scope === "profile") return state.targetUin ? `${state.targetUin} 的空间` : "指定 QQ";
  return "好友动态";
}

function mediaLayoutClass(count) {
  const safeCount = Math.max(1, Math.min(Number(count || 0), 9));
  return `media-layout-${safeCount}`;
}

function mediaDisplaySource(item) {
  const source = typeof item === "object" && item
    ? text(item.preview_url || item.previewUrl || item.source || item.data_url || item.url)
    : text(item);
  if (!source) return "";
  if (source.startsWith("base64://")) {
    const mimeType = typeof item === "object" && item ? text(item.mime_type || item.content_type || "image/jpeg") : "image/jpeg";
    return `data:${mimeType || "image/jpeg"};base64,${source.replace("base64://", "")}`;
  }
  return source;
}

function isVideoSource(url) {
  const value = text(url);
  return value.startsWith("data:video/") || /\.(mp4|m4v|mov|webm|ogg)(?:[?#].*)?$/i.test(value);
}

function isVideoMediaItem(item, source = "") {
  if (item && typeof item === "object") {
    const kind = text(item.kind || item.type || item.raw_type).toLowerCase();
    const mimeType = text(item.mime_type || item.content_type || item.mime).toLowerCase();
    if (kind === "video" || mimeType.startsWith("video/")) return true;
  }
  return isVideoSource(source || mediaDisplaySource(item));
}

function fileLooksLikeVideo(file) {
  return text(file?.type).toLowerCase().startsWith("video/") || isVideoSource(file?.name);
}

function queuedMediaHasVideo() {
  return (state.media || []).some((item) => isVideoMediaItem(item));
}

function createLocalPreviewUrl(file) {
  try {
    if (
      typeof URL !== "undefined"
      && typeof URL.createObjectURL === "function"
      && typeof Blob !== "undefined"
      && file instanceof Blob
    ) {
      return URL.createObjectURL(file);
    }
  } catch (_) {
  }
  return "";
}

function revokeLocalPreviewUrl(item) {
  const url = text(item?.preview_url || item?.previewUrl);
  if (!url.startsWith("blob:")) return;
  try {
    URL.revokeObjectURL?.(url);
  } catch (_) {
  }
}

function clearMediaQueue() {
  for (const item of state.media || []) {
    revokeLocalPreviewUrl(item);
  }
  state.media = [];
}

function uploadedMediaFromPayload(payload) {
  const candidates = [
    payload?.media,
    payload?.data?.media,
    payload?.payload?.media,
    payload,
  ];
  for (const candidate of candidates) {
    if (!candidate || typeof candidate !== "object") continue;
    if (candidate.media && typeof candidate.media === "object") return { ...candidate.media };
    if (candidate.upload_id || candidate.source || candidate.data_url || candidate.url || candidate.path || candidate.file) {
      return { ...candidate };
    }
  }
  return null;
}

function mediaForPublish(item) {
  const payload = { ...(item || {}) };
  delete payload.preview_url;
  delete payload.previewUrl;
  if (text(payload.source).startsWith("blob:")) {
    delete payload.source;
  }
  return payload;
}



function renderStatus() {
  const login = state.status?.login || {};
  rememberAuthor(login);
  el.accountName.textContent = loginDisplayName();
  el.accountMeta.textContent = login.uin ? `QQ ${login.uin}` : "需要先绑定 Cookie";
  if (el.statusText) {
    const daemon = state.status?.daemon || {};
    el.statusText.textContent = daemon.state === "ready" ? "daemon 已就绪" : `daemon ${daemon.state || "未知"}`;
  }
  
  if (login.uin && el.accountAvatar) {
    renderAvatar(el.accountAvatar, login, loginDisplayName());
  }
}

function renderTabs() {
  for (const tab of el.tabs) {
    tab.classList.toggle("active", tab.dataset.scope === state.scope);
  }
}


function renderMedia() {
  const container = document.getElementById("mediaStrip");
  container.className = "media-preview-grid";
  container.replaceChildren();

  for (const [index, item] of state.media.entries()) {
    const chip = document.createElement("div");
    chip.className = "media-preview-item";

    const source = mediaDisplaySource(item);
    if (source.startsWith("data:") || source.startsWith("blob:") || source.startsWith("http://") || source.startsWith("https://")) {
      if (isVideoMediaItem(item, source)) {
        const video = document.createElement("video");
        video.src = source;
        video.controls = true;
        video.muted = true;
        video.playsInline = true;
        chip.appendChild(video);
      } else {
        const img = document.createElement("img");
        img.src = source;
        chip.appendChild(img);
      }
    } else {
      const fallback = document.createElement("div");
      fallback.style.background = "var(--surface-hover)";
      fallback.style.width = "100%";
      fallback.style.height = "100%";
      fallback.style.display = "flex";
      fallback.style.alignItems = "center";
      fallback.style.justifyContent = "center";
      fallback.style.fontSize = "12px";
      fallback.textContent = (isVideoMediaItem(item, source) ? "视频 " : "媒体 ") + (index + 1);
      chip.appendChild(fallback);
    }

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "media-preview-remove";
    remove.textContent = "x";
    remove.title = "移除媒体";
    remove.setAttribute("aria-label", "移除媒体");
    remove.addEventListener("click", () => {
      revokeLocalPreviewUrl(state.media[index]);
      state.media.splice(index, 1);
      renderMedia();
    });

    chip.append(remove);
    container.append(chip);
  }
}
function renderSkeletonFeed() {
    el.feed.replaceChildren();
    for (let i = 0; i < 3; i++) {
        const card = document.createElement("article");
        card.className = "post skeleton-card";
        card.innerHTML = `
            <div class="skeleton-head">
                <div class="skeleton skeleton-avatar"></div>
                <div class="skeleton-meta">
                    <div class="skeleton skeleton-line short"></div>
                    <div class="skeleton skeleton-line medium"></div>
                </div>
            </div>
            <div class="skeleton skeleton-line long"></div>
            <div class="skeleton skeleton-line medium"></div>
        `;
        el.feed.append(card);
    }
}

function isDetailDrawerMode() {
  if (typeof window.matchMedia === "function") {
    return window.matchMedia(`(max-width: ${DETAIL_DRAWER_BREAKPOINT}px)`).matches;
  }
  return typeof window.innerWidth === "number" && window.innerWidth <= DETAIL_DRAWER_BREAKPOINT;
}

function clearDetailDrawerChrome() {
  const pane = document.getElementById("detailPane");
  const backdrop = document.getElementById("detailBackdrop");
  const closeBtn = document.getElementById("detailCloseBtn");
  pane?.classList.remove("visible");
  backdrop?.classList.remove("visible");
  if (closeBtn) closeBtn.hidden = true;
}

function renderFeed() {
  el.feedTitle.textContent = scopeTitle();
  el.feedMeta.textContent = state.loading ? "加载中..." : `${state.posts.length} 条`;
  el.moreButton.hidden = !state.hasMore;
  el.feed.replaceChildren();

  if (!state.posts.length && !state.loading) {
    const empty = document.createElement("div");
    empty.className = "empty-feed";
    empty.textContent = "还没有读取到说说。";
    el.feed.append(empty);
    return;
  }

  for (const post of state.posts) {
    el.feed.append(renderPostCard(post));
  }
}


function openLightbox(url) {
  let lightbox = document.getElementById("lightbox");
  if (!lightbox) {
    lightbox = document.createElement("div");
    lightbox.id = "lightbox";
    lightbox.className = "lightbox";
    
    document.body.appendChild(lightbox);
  }
  
  const cleanup = () => {
    lightbox.classList.remove("visible");
    const video = lightbox.querySelector("video");
    if (video) {
        video.pause();
        video.src = "";
        video.removeAttribute("src");
    }
    setTimeout(() => {
        lightbox.innerHTML = "";
    }, 300);
  };

  lightbox.onclick = (e) => {
    if (e.target === lightbox) {
      cleanup();
    }
  };

  lightbox.innerHTML = "";

  const closeBtn = document.createElement("button");
  closeBtn.className = "lightbox-close";
  closeBtn.textContent = "x";
  closeBtn.setAttribute("aria-label", "关闭预览");
  closeBtn.onclick = cleanup;
  
  let mediaElement;
  if (isVideoSource(url)) {
    mediaElement = document.createElement("video");
    mediaElement.src = url;
    mediaElement.controls = true;
    mediaElement.autoplay = true;
  } else {
    mediaElement = document.createElement("img");
    mediaElement.src = url;
  }
  
  lightbox.appendChild(closeBtn);
  lightbox.appendChild(mediaElement);
  
  void lightbox.offsetWidth; // Force reflow
  lightbox.classList.add("visible");
}

function renderMediaGrid(items, className = "post-media") {
  const entries = (items || [])
    .map((item) => ({ item, source: mediaDisplaySource(item) }))
    .filter((entry) => entry.source)
    .slice(0, 9);
  if (!entries.length) return null;

  const media = document.createElement("div");
  media.className = `${className} media-grid ${mediaLayoutClass(entries.length)}`;
  for (const { item, source: url } of entries) {
    if (isVideoMediaItem(item, url)) {
      const video = document.createElement("video");
      video.src = url;
      video.className = "preview-video";
      video.muted = true;
      video.loop = true;
      video.playsInline = true;
      video.addEventListener("mouseenter", () => video.play().catch(() => {}));
      video.addEventListener("mouseleave", () => {
        video.pause();
        video.currentTime = 0;
      });
      video.addEventListener("click", (event) => {
        event.stopPropagation();
        openLightbox(url);
      });
      media.append(video);
    } else {
      const image = document.createElement("img");
      image.loading = "lazy";
      image.alt = "说说图片";
      image.src = url;
      image.addEventListener("click", (event) => {
        event.stopPropagation();
        openLightbox(url);
      });
      media.append(image);
    }
  }
  return media;
}

function renderPostCard(post) {
  rememberPostAuthors(post);
  const card = document.createElement("article");
  card.className = "post";
  card.dataset.id = post.id;

  const head = document.createElement("button");
  head.className = "post-header";
  head.type = "button";
  head.addEventListener("click", () => openDetail(post.id));

  const avatar = document.createElement("span");
  avatar.className = "avatar";
  const authorName = displayAuthorName(post.author);
  renderAvatar(avatar, post.author, authorName);

  const meta = document.createElement("span");
  meta.className = "post-meta";
  const name = document.createElement("strong");
  name.textContent = authorName;
  const time = document.createElement("span");
  time.textContent = formatTime(post.created_at);
  meta.append(name, time);
  head.append(avatar, meta);

  card.append(head);

  const contentText = cleanDisplayText(post.content);
  if (contentText) {
    const body = document.createElement("p");
    body.className = "post-content";
    body.textContent = contentText;
    card.append(body);
  }

  const images = renderMediaGrid(post.images, "post-media");
  if (images) {
    card.append(images);
  }

  const actions = document.createElement("div");
  actions.className = "post-actions";
  const like = document.createElement("button");
  like.type = "button";
  like.className = `metric-action${post.liked ? " liked" : ""}`;
  like.disabled = state.pendingLikes.has(post.id);
  like.setAttribute?.("aria-busy", state.pendingLikes.has(post.id) ? "true" : "false");
  
  const likeIcon = document.createElement("span");
  likeIcon.innerHTML = post.liked ? "♥" : "♡";
  likeIcon.className = "action-icon";
  if (post.liked) likeIcon.style.color = "var(--danger)";
  
  const likeText = document.createElement("span");
  likeText.textContent = ` ${post.stats?.likes ?? 0}`;
  
  like.append(likeIcon, likeText);
  like.addEventListener("click", () => toggleLike(post));

  const comment = document.createElement("button");
  comment.type = "button";
  comment.className = "metric-action";
  
  const commentIcon = document.createElement("span");
  commentIcon.innerHTML = "💬";
  commentIcon.className = "action-icon";
  
  const commentText = document.createElement("span");
  commentText.textContent = ` ${post.stats?.comments ?? 0}`;
  
  comment.append(commentIcon, commentText);
  comment.addEventListener("click", () => openDetail(post.id, true));

  const detail = document.createElement("button");
  detail.type = "button";
  detail.className = "detail-action";
  detail.textContent = "查看详情";
  detail.addEventListener("click", () => openDetail(post.id));
  
  actions.append(like, comment, detail);
  card.append(actions);
  
  return card;
}

function renderDetail(post, options = {}) {
  state.selected = mergePost(state.selected?.id === post.id ? state.selected : {}, post);
  post = state.selected;
  rememberPostAuthors(post);
  const isLoading = Boolean(options.loading || state.detailLoadingId === post.id);
  el.detailEmpty.hidden = true;
  el.detailContent.hidden = false;
  el.detailPane.classList.add("has-selection");
  el.detailContent.setAttribute?.("aria-busy", isLoading ? "true" : "false");
  el.detailContent.replaceChildren();

  const title = document.createElement("div");
  title.className = "detail-title";
  
  const titleHead = document.createElement("div");
  titleHead.className = "detail-header";
  
  const titleAvatar = document.createElement("span");
  titleAvatar.className = "avatar";
  const authorName = displayAuthorName(post.author);
  renderAvatar(titleAvatar, post.author, authorName);
  
  const titleMeta = document.createElement("div");
  titleMeta.className = "detail-meta";
  const name = document.createElement("strong");
  name.textContent = authorName;
  const time = document.createElement("span");
  time.textContent = formatTime(post.created_at);
  titleMeta.append(name, time);
  
  titleHead.append(titleAvatar, titleMeta);
  title.append(titleHead);

  el.detailContent.append(title);

  const contentText = cleanDisplayText(post.content);
  if (contentText) {
    const content = document.createElement("p");
    content.className = "post-content detail-text";
    content.textContent = contentText;
    el.detailContent.append(content);
  }

  const detailMedia = renderMediaGrid(post.images, "detail-media");
  if (detailMedia) {
    el.detailContent.append(detailMedia);
  }

  const actions = document.createElement("div");
  actions.className = "detail-actions";
  titleHead.append(actions);
  const like = document.createElement("button");
  like.type = "button";
  like.textContent = post.liked ? "取消点赞" : "点赞";
  like.disabled = state.pendingLikes.has(post.id);
  like.setAttribute?.("aria-busy", state.pendingLikes.has(post.id) ? "true" : "false");
  like.addEventListener("click", () => toggleLike(post));
  actions.append(like);
  if (post.can_delete) {
    const deleting = state.pendingDeletes.has(post.id);
    const confirming = Number(state.pendingDeleteConfirms.get(post.id) || 0) > Date.now();
    const del = document.createElement("button");
    del.type = "button";
    del.className = "danger";
    del.textContent = deleting ? "删除中..." : confirming ? "再次点击确认删除" : "删除";
    del.disabled = deleting;
    del.setAttribute?.("aria-busy", deleting ? "true" : "false");
    del.addEventListener("click", () => deletePost(post));
    actions.append(del);
  }

  const comments = document.createElement("div");
  comments.className = "comments";
  const commentsTitle = document.createElement("h2");
  commentsTitle.textContent = "全部评论";
  comments.append(commentsTitle);
  if (isLoading) {
    const loading = document.createElement("div");
    loading.className = "detail-loading";
    loading.textContent = "正在同步详情...";
    comments.append(loading);
  }
  for (const item of post.comments || []) {
    comments.append(renderComment(post, item));
  }
  if (!post.comments?.length && !isLoading) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "还没有人评论，快来抢沙发。";
    comments.append(empty);
  }

  const form = document.createElement("form");
  form.className = "comment-form";
  const input = document.createElement("textarea");
  input.rows = 3;
  input.placeholder = "写下你的评论...";
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = "发送";
  form.append(input, submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    await sendComment(post, input.value);
    input.value = "";
  });

  el.detailContent.append(comments, form);
}

function renderComment(post, comment) {
  rememberAuthor(comment.author);
  const row = document.createElement("div");
  row.className = "comment";
  const targetKey = `${post.id}:${text(comment.id)}`;
  
  const avatar = document.createElement("span");
  avatar.className = "avatar";
  const authorName = displayAuthorName(comment.author);
  renderAvatar(avatar, comment.author, authorName);
  
  const body = document.createElement("div");
  body.className = "comment-body";
  
  const name = document.createElement("strong");
  name.textContent = authorName;
  const content = document.createElement("p");
  content.textContent = cleanDisplayText(comment.content);
  body.append(name, content);
  row.append(avatar, body);
  if (comment.can_reply) {
    const reply = document.createElement("button");
    reply.type = "button";
    reply.className = "reply-btn";
    reply.textContent = "回复";
    reply.addEventListener("click", () => {
      const commentId = text(comment.id);
      const isSameTarget = state.replyTarget?.postId === post.id
        && state.replyTarget?.commentId === commentId;
      state.replyTarget = isSameTarget ? null : { postId: post.id, commentId };
      renderSelectedIfNeeded(post.id);
      if (!isSameTarget) {
        setTimeout(() => {
          el.detailContent.querySelector(".reply-form textarea")?.focus();
        }, 0);
      }
    });
    row.append(reply);
  }
  if (state.replyTarget?.postId === post.id && state.replyTarget?.commentId === text(comment.id)) {
    const form = document.createElement("form");
    form.className = "reply-form";
    const input = document.createElement("textarea");
    input.rows = 2;
    input.placeholder = `回复 ${authorName}`;
    input.value = state.replyDrafts.get(targetKey) || "";
    input.addEventListener("input", () => {
      state.replyDrafts.set(targetKey, input.value);
    });
    const actions = document.createElement("div");
    actions.className = "reply-form-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "ghost";
    cancel.textContent = "取消";
    cancel.addEventListener("click", () => {
      state.replyDrafts.delete(targetKey);
      state.replyTarget = null;
      renderSelectedIfNeeded(post.id);
    });
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "primary";
    submit.textContent = "发送回复";
    actions.append(cancel, submit);
    form.append(input, actions);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      const sent = await sendReply(post, comment, input.value);
      if (sent) {
        state.replyDrafts.delete(targetKey);
      } else {
        submit.disabled = false;
      }
    });
    row.append(form);
  }
  return row;
}

async function loadStatus() {
  const data = await apiGet("page/status");
  state.status = data;
  renderStatus();
}

function feedParams(next = false) {
  const params = { limit: 10 };
  if (next && state.cursor) params.cursor = state.cursor;
  if (state.scope === "friends") params.scope = "friends";
  if (state.scope === "self") params.scope = "self";
  if (state.scope === "profile") {
    params.scope = "profile";
    params.hostuin = state.targetUin;
  }
  return params;
}

async function loadFeed({ append = false } = {}) {
  if (append && state.loading) return;
  if (state.scope === "profile" && !state.targetUin) {
    state.posts = [];
    state.cursor = "";
    state.hasMore = false;
    setNotice("输入 QQ 号后查看指定空间。", "warn");
    renderFeed();
    return;
  }
  
  // Only show loading if we're completely empty, otherwise let the spinner handle it gracefully
  state.loading = true;
  if (!append) {
    renderSkeletonFeed();
  } else {
    el.moreButton.textContent = "加载中...";
    el.moreButton.disabled = true;
  }
  
  try {
    const data = await apiGet("page/feed", feedParams(append));
    const incomingPosts = dedupePosts(data.items || []);
    const previousCount = state.posts.length;
    state.cursor = data.cursor || "";
    state.hasMore = Boolean(data.has_more);
    state.posts = append ? mergeFeedPosts(state.posts, incomingPosts) : incomingPosts;
    if (append && incomingPosts.length && state.posts.length === previousCount) {
      state.hasMore = false;
      state.cursor = "";
    }
    rememberPosts(state.posts);
    renderStatus();
    setNotice("");
  } catch (error) {
    setNotice(error.message || "动态加载失败", "error");
  } finally {
    state.loading = false;
    if (append) {
      el.moreButton.textContent = "加载更多";
      el.moreButton.disabled = false;
    }
    renderFeed();
  }
}

function toggleDetailDrawer(visible) {
    const pane = document.getElementById("detailPane");
    const backdrop = document.getElementById("detailBackdrop");
    const closeBtn = document.getElementById("detailCloseBtn");
    if (!pane) return;
    if (isDetailDrawerMode()) {
        if (visible) {
            pane.classList.add("visible");
            if (closeBtn) closeBtn.hidden = false;
            if (backdrop) backdrop.classList.add("visible");
        } else {
            pane.classList.remove("visible");
            if (closeBtn) closeBtn.hidden = true;
            if (backdrop) backdrop.classList.remove("visible");
            state.selected = null;
        }
    } else {
        clearDetailDrawerChrome();
        // Desktop just scroll into view if needed
        if (visible) {
            pane.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
        }
    }
}

window.addEventListener?.("resize", () => {
  if (!isDetailDrawerMode()) {
    clearDetailDrawerChrome();
  }
});

async function openDetail(id, focusComment = false) {
  const cached = currentPostById(id);
  const requestSeq = ++state.detailRequestSeq;
  const versionAtRequestStart = localVersion(id);
  if (state.selected?.id !== id) {
    state.replyTarget = null;
  }
  state.detailLoadingId = id;
  toggleDetailDrawer(true);
  if (cached) {
    renderDetail(cached, { loading: true });
    renderFeed();
    if (focusComment) {
      setTimeout(() => {
        el.detailContent.querySelector(".comment-form textarea")?.focus();
      }, 0);
    }
  }
  try {
    const data = await apiGet("page/detail", { id });
    if (requestSeq !== state.detailRequestSeq) return;
    state.detailLoadingId = "";
    const incoming = mergeRemotePost(data.post.id, data.post, versionAtRequestStart);
    const merged = updatePost(incoming.id, incoming) || incoming;
    rememberPostAuthors(merged);
    renderDetail(merged);
    renderFeed();
    if (data.partial) {
      setNotice(data.message || "详情接口响应较慢，已先显示缓存内容。", "warn");
    }
    if (focusComment) {
      setTimeout(() => {
        el.detailContent.querySelector(".comment-form textarea")?.focus();
      }, 0);
    }
  } catch (error) {
    if (requestSeq !== state.detailRequestSeq) return;
    state.detailLoadingId = "";
    if (cached) {
      renderDetail(currentPostById(id, cached));
    }
    setNotice(error.message || "详情加载失败", "error");
  }
}

async function toggleLike(post) {
  if (state.pendingLikes.has(post.id)) return;
  state.pendingLikes.add(post.id);
  const current = state.posts.find((item) => item.id === post.id) || state.selected || post;
  const oldLiked = Boolean(current.liked);
  const oldStats = { ...(current.stats || { likes: 0, comments: 0 }) };
  const nextLiked = !oldLiked;
  const nextLikes = Math.max(0, Number(oldStats.likes || 0) + (nextLiked ? 1 : -1));
  updatePost(post.id, {
    liked: nextLiked,
    stats: { ...oldStats, likes: nextLikes },
  });
  markLocalChange(post.id);
  renderFeed();
  renderSelectedIfNeeded(post.id);
  try {
    const data = await apiPost("page/like", { id: post.id, unlike: oldLiked });
    const finalLiked = Boolean(data.liked);
    const finalLikes = finalLiked === nextLiked
      ? nextLikes
      : Math.max(0, nextLikes + (finalLiked ? 1 : -1));
    updatePost(post.id, {
      liked: finalLiked,
      stats: { ...oldStats, likes: finalLikes },
    });
    markLocalChange(post.id);
    if (data.verified === false || data.operation_status === "accepted_pending_verification") {
      setNotice("QQ空间已接受操作，读回状态可能稍后同步。", "warn");
    } else {
      setNotice("");
    }
  } catch (error) {
    updatePost(post.id, { liked: oldLiked, stats: oldStats });
    markLocalChange(post.id);
    setNotice(error.message || "点赞失败", "error");
  } finally {
    state.pendingLikes.delete(post.id);
    renderFeed();
    renderSelectedIfNeeded(post.id);
  }
}

async function sendComment(post, content) {
  const textValue = text(content).trim();
  if (!textValue) return;
  const postId = post.id;
  const current = currentPostById(postId, post);
  const comments = [...(current.comments || [])];
  const tempId = `pending_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  const optimisticComment = {
    id: tempId,
    author: currentLoginAuthor(),
    content: textValue,
    created_at: Math.floor(Date.now() / 1000),
    can_reply: false,
  };
  const oldStats = { ...(current.stats || { likes: 0, comments: 0 }) };
  updatePost(postId, {
    comments: [...comments, optimisticComment],
    stats: { ...oldStats, comments: Number(oldStats.comments || 0) + 1 },
  });
  markLocalChange(postId);
  renderFeed();
  renderSelectedIfNeeded(postId);
  try {
    const data = await apiPost("page/comment", { id: postId, content: textValue });
    const serverComment = data.comment || {};
    const author = mergeAuthor(optimisticComment.author, serverComment.author);
    rememberAuthor(author);
    const target = currentPostById(postId, post);
    const nextComments = [...(target?.comments || [])].map((item) => (
      item.id === tempId
        ? {
          ...item,
          id: serverComment.id || tempId,
          author,
          content: serverComment.content || item.content,
          can_reply: Boolean(serverComment.id),
        }
        : item
    ));
    updatePost(postId, { comments: nextComments });
    markLocalChange(postId);
    setNotice("评论已发送。", "success");
  } catch (error) {
    const target = currentPostById(postId, post);
    const currentComments = [...(target?.comments || [])];
    const hadTemp = currentComments.some((item) => item.id === tempId);
    const currentStats = { ...(target?.stats || oldStats) };
    updatePost(postId, {
      comments: currentComments.filter((item) => item.id !== tempId),
      stats: hadTemp
        ? { ...currentStats, comments: Math.max(0, Number(currentStats.comments || 0) - 1) }
        : currentStats,
    });
    markLocalChange(postId);
    setNotice(error.message || "评论失败", "error");
  } finally {
    renderFeed();
    renderSelectedIfNeeded(postId);
  }
}

async function sendReply(post, comment, content) {
  const replyText = text(content).trim();
  if (!replyText) return false;
  const postId = post.id;
  const pendingKey = `${postId}:${text(comment.id)}`;
  if (state.pendingReplies.has(pendingKey)) return false;
  state.pendingReplies.add(pendingKey);
  const current = currentPostById(postId, post);
  const comments = [...(current.comments || [])];
  const oldStats = { ...(current.stats || { likes: 0, comments: 0 }) };
  const tempId = `pending_reply_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  const targetName = displayAuthorName(comment.author, "评论");
  const previousReplyTarget = state.replyTarget;
  const optimisticReply = {
    id: tempId,
    author: currentLoginAuthor(),
    content: `回复 ${targetName}：${replyText}`,
    created_at: Math.floor(Date.now() / 1000),
    can_reply: false,
  };
  updatePost(postId, {
    comments: [...comments, optimisticReply],
    stats: { ...oldStats, comments: Number(oldStats.comments || 0) + 1 },
  });
  markLocalChange(postId);
  if (state.replyTarget?.postId === postId && state.replyTarget?.commentId === text(comment.id)) {
    state.replyTarget = null;
  }
  renderFeed();
  renderSelectedIfNeeded(postId);
  try {
    const data = await apiPost("page/reply", {
      id: postId,
      commentid: comment.id,
      comment_uin: comment.author?.uin,
      content: replyText,
    });
    const serverReply = data.reply || {};
    const author = mergeAuthor(optimisticReply.author, serverReply.author);
    rememberAuthor(author);
    const target = currentPostById(postId, post);
    const nextComments = [...(target?.comments || [])].map((item) => (
      item.id === tempId
        ? {
          ...item,
          id: serverReply.id || tempId,
          author,
          content: serverReply.content ? `回复 ${targetName}：${serverReply.content}` : item.content,
        }
        : item
    ));
    updatePost(postId, { comments: nextComments });
    markLocalChange(postId);
    setNotice("回复已发送。", "success");
    return true;
  } catch (error) {
    const target = currentPostById(postId, post);
    const currentComments = [...(target?.comments || [])];
    const hadTemp = currentComments.some((item) => item.id === tempId);
    const currentStats = { ...(target?.stats || oldStats) };
    updatePost(postId, {
      comments: currentComments.filter((item) => item.id !== tempId),
      stats: hadTemp
        ? { ...currentStats, comments: Math.max(0, Number(currentStats.comments || 0) - 1) }
        : currentStats,
    });
    markLocalChange(postId);
    state.replyTarget = previousReplyTarget;
    setNotice(error.message || "回复失败", "error");
  } finally {
    state.pendingReplies.delete(pendingKey);
    renderFeed();
    renderSelectedIfNeeded(postId);
  }
  return false;
}

async function deletePost(post) {
  if (state.pendingDeletes.has(post.id)) return;
  const now = Date.now();
  const confirmUntil = Number(state.pendingDeleteConfirms.get(post.id) || 0);
  if (confirmUntil < now) {
    const expiresAt = now + 6000;
    state.pendingDeleteConfirms.set(post.id, expiresAt);
    setNotice("再次点击删除按钮确认删除这条说说。", "warn");
    renderSelectedIfNeeded(post.id);
    window.setTimeout(() => {
      if (Number(state.pendingDeleteConfirms.get(post.id) || 0) <= Date.now()) {
        state.pendingDeleteConfirms.delete(post.id);
        renderSelectedIfNeeded(post.id);
      }
    }, 6100);
    return;
  }
  state.pendingDeleteConfirms.delete(post.id);
  state.pendingDeletes.add(post.id);
  setNotice("正在删除说说...");
  renderSelectedIfNeeded(post.id);
  try {
    await apiPost("page/delete", { id: post.id });
    state.posts = state.posts.filter((item) => item.id !== post.id);
    state.selected = null;
    state.replyTarget = null;
    state.localVersions.delete(post.id);
    state.pendingDeleteConfirms.delete(post.id);
    el.detailContent.hidden = true;
    el.detailEmpty.hidden = false;
    el.detailPane.classList.remove("has-selection");
    clearDetailDrawerChrome();
    setNotice("说说已删除。", "success");
  } catch (error) {
    setNotice(error.message || "删除失败", "error");
  } finally {
    state.pendingDeletes.delete(post.id);
    renderFeed();
    renderSelectedIfNeeded(post.id);
  }
}

async function publish(event) {
  event.preventDefault();
  const content = el.publishContent.value;
  if (!content.trim() && !state.media.length) {
    setNotice("写点文字或添加图片/视频再发布。", "warn");
    return;
  }
  el.publishButton.disabled = true;
  const originalText = el.publishButton.textContent;
  el.publishButton.textContent = "发布中...";
  try {
    const media = state.media.map(mediaForPublish);
    const data = await apiPost("page/publish", { content, media });
    el.publishContent.value = "";
    clearMediaQueue();
    renderMedia();
    if (data.post) {
      data.post.author = mergeAuthor(currentLoginAuthor(), data.post.author);
      rememberPostAuthors(data.post);
      state.posts = [data.post, ...state.posts];
      renderFeed();
    } else {
      await loadFeed();
    }
    setNotice("说说已发布。", "success");
  } catch (error) {
    setNotice(error.message || "发布失败", "error");
  } finally {
    el.publishButton.disabled = false;
    el.publishButton.textContent = originalText;
  }
}

async function uploadFiles(files) {
  const maxMedia = state.status?.limits?.images || 9;
  for (const file of files) {
    const incomingVideo = fileLooksLikeVideo(file);
    if ((incomingVideo && state.media.length) || (!incomingVideo && queuedMediaHasVideo())) {
      setNotice("视频说说请只添加一个视频，不要和图片混发。", "warn");
      break;
    }
    if (state.media.length >= maxMedia) {
      setNotice(`最多只能添加 ${maxMedia} 个图片/视频。`, "warn");
      break;
    }
    try {
      const data = await uploadPageMedia(file);
      const media = uploadedMediaFromPayload(data);
      if (!media) throw new Error("上传失败");
      if (!media.source && media.data_url) {
        media.source = media.data_url;
      }
      const previewUrl = createLocalPreviewUrl(file);
      if (previewUrl) {
        media.preview_url = previewUrl;
      }
      state.media.push(media);
    } catch (error) {
      setNotice(error.message || "媒体上传失败", "error");
    }
  }
  renderMedia();
}

function bindEvents() {
  for (const tab of el.tabs) {
    tab.addEventListener("click", async () => {
      state.scope = tab.dataset.scope;
      state.cursor = "";
      state.posts = [];
      renderTabs();
      await loadFeed();
    });
  }
  el.targetForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    state.targetUin = el.targetUin.value.trim();
    state.scope = "profile";
    state.cursor = "";
    state.posts = [];
    renderTabs();
    await loadFeed();
  });

  const closeBtn = document.getElementById("detailCloseBtn");
  if (closeBtn) closeBtn.addEventListener("click", () => toggleDetailDrawer(false));
  const backdrop = document.getElementById("detailBackdrop");
  if (backdrop) backdrop.addEventListener("click", () => toggleDetailDrawer(false));

  const contentInput = document.getElementById("publishContent");
  const charCounter = document.getElementById("charCounter");
  if (contentInput && charCounter) {
      contentInput.addEventListener("input", () => {
          const len = contentInput.value.length;
          charCounter.textContent = `${len}/1200`;
          if (len > 1100) charCounter.classList.add("warning");
          else charCounter.classList.remove("warning");
      });
  }

  el.refreshButton.addEventListener("click", () => {
    state.cursor = "";
    state.posts = [];
    loadFeed();
  });
  el.moreButton.addEventListener("click", () => loadFeed({ append: true }));
  el.publishForm.addEventListener("submit", publish);
  el.mediaInput.addEventListener("change", async () => {
    await uploadFiles(el.mediaInput.files || []);
    el.mediaInput.value = "";
  });
}

async function init() {
  bindThemeSync();
  if (!bridge) {
    setNotice("没有检测到 AstrBot Pages bridge，请从 AstrBot WebUI 插件页面进入。", "error");
    return;
  }
  
  try {
    state.context = await withTimeout(
      bridge.ready(),
      BRIDGE_READY_TIMEOUT_MS,
      "初始化 AstrBot WebUI 桥接超时，请刷新页面后重试。"
    );
    syncTheme(state.context);
  } catch (error) {
    setNotice(error.message || "桥接初始化失败", "error");
    return;
  }

  bindEvents();
  renderTabs();
  
  try {
    await loadStatus();
    await loadFeed();
  } catch (error) {
    setNotice(error.message || "初始化失败", "error");
  }
}

init();

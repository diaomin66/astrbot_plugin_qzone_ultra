const bridge = window.AstrBotPluginPage;
const BRIDGE_READY_TIMEOUT_MS = 5000;
const BRIDGE_REQUEST_TIMEOUT_MS = 60000;
const DETAIL_DRAWER_BREAKPOINT = 1200;
const DETAIL_CACHE_TTL_MS = 45000;

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
  pendingComments: new Set(),
  pendingDeletes: new Set(),
  pendingDeleteConfirms: new Map(),
  knownAuthors: new Map(),
  replyTarget: null,
  replyDrafts: new Map(),
  commentDrafts: new Map(),
  pendingReplies: new Set(),
  localVersions: new Map(),
  detailCache: new Map(),
  detailInFlight: new Map(),
  detailRequestSeq: 0,
  detailLoadingId: "",
  lifeJob: null,
  lifePollTimer: null,
  lifeRequestPending: false,
  lifeRequestId: "",
  lifeAssociation: null,
  lifeAssociationInvalid: "",
  lifeAssociationPreviewUnavailable: false,
  lifeRecovery: null,
  lifeRecoveryExpired: false,
  lifeLoadPending: false,
  publishPending: false,
  pendingUploads: 0,
  composerRevision: 0,
  confirmationToken: null,
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
  generateLifeDraft: document.getElementById("generateLifeDraft"),
  lifeDraftStatus: document.getElementById("lifeDraftStatus"),
  lifeDraftResult: document.getElementById("lifeDraftResult"),
  lifeDraftDiagnostics: document.getElementById("lifeDraftDiagnostics"),
  lifeDraftPreview: document.getElementById("lifeDraftPreview"),
  lifeDraftLongContent: document.getElementById("lifeDraftLongContent"),
  loadLifeDraft: document.getElementById("loadLifeDraft"),
  discardLifeDraft: document.getElementById("discardLifeDraft"),
  actionConfirmDialog: document.getElementById("actionConfirmDialog"),
  actionConfirmTitle: document.getElementById("actionConfirmTitle"),
  actionConfirmMessage: document.getElementById("actionConfirmMessage"),
  actionConfirmFeedback: document.getElementById("actionConfirmFeedback"),
  actionConfirmCancel: document.getElementById("actionConfirmCancel"),
  actionConfirmAccept: document.getElementById("actionConfirmAccept"),
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
  if (base?.images || patch?.images) {
    const nextImages = patch?.images?.length ? patch.images : base?.images || [];
    merged.images = dedupeMediaEntries(nextImages).map((entry) => entry.item);
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

function cacheDetailPost(post) {
  if (!post?.id) return;
  state.detailCache.set(post.id, {
    post: mergePost({}, post),
    expiresAt: Date.now() + DETAIL_CACHE_TTL_MS,
  });
}

function cachedDetailPost(id) {
  const cached = state.detailCache.get(id);
  if (!cached) return null;
  if (cached.expiresAt <= Date.now()) {
    state.detailCache.delete(id);
    return null;
  }
  return mergePost(currentPostById(id, {}), cached.post);
}

function selectedPostId() {
  return text(state.selected?.id);
}

function syncSelectedFeedCard(id = selectedPostId()) {
  if (!el.feed) return;
  for (const card of el.feed.querySelectorAll?.(".post") || []) {
    const selected = text(card.dataset?.id) === text(id);
    card.classList.toggle("selected", selected);
    if (selected) {
      card.setAttribute?.("aria-current", "true");
    } else {
      card.removeAttribute?.("aria-current");
    }
  }
}

function detailScrollTop() {
  return Number(el.detailPane?.scrollTop || 0);
}

function restoreDetailScroll(value) {
  if (!el.detailPane || !Number.isFinite(value)) return;
  requestAnimationFrameSafe(() => {
    el.detailPane.scrollTop = value;
  });
}

function requestAnimationFrameSafe(callback) {
  if (typeof window.requestAnimationFrame === "function") {
    window.requestAnimationFrame(callback);
  } else {
    window.setTimeout(callback, 0);
  }
}

async function fetchDetailPost(id) {
  if (state.detailInFlight.has(id)) {
    return state.detailInFlight.get(id);
  }
  const request = apiGet("page/detail", { id })
    .finally(() => {
      state.detailInFlight.delete(id);
    });
  state.detailInFlight.set(id, request);
  return request;
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

let actionConfirmationSequence = 0;

function actionBusyFeedback() {
  const message = "已有操作正在等待确认或完成，请稍候再试。";
  if (state.confirmationToken && el.actionConfirmFeedback) el.actionConfirmFeedback.textContent = message;
  setNotice(message, "warn");
}

function beginActionConfirmation({ title, message, confirmText = "确认", cancelText = "取消" }) {
  if (state.confirmationToken) {
    actionBusyFeedback();
    return null;
  }
  const dialog = el.actionConfirmDialog;
  if (!dialog || typeof dialog.showModal !== "function" || typeof dialog.close !== "function" || typeof dialog.addEventListener !== "function" || typeof dialog.removeEventListener !== "function" || !el.actionConfirmAccept || !el.actionConfirmCancel) {
    setNotice("当前页面无法显示安全的确认窗口，操作已停止。请刷新页面后重试。", "error");
    return null;
  }
  let resolvePromise;
  const promise = new Promise(resolve => { resolvePromise = resolve; });
  const token = {
    id: ++actionConfirmationSequence,
    resolve: resolvePromise,
    settled: false,
    opener: document.activeElement || null,
    onCancel: null,
    onClose: null,
    onKeydown: null,
  };
  state.confirmationToken = token;
  renderLifeDraft();
  if (el.actionConfirmTitle) el.actionConfirmTitle.textContent = text(title || "请确认操作");
  if (el.actionConfirmMessage) el.actionConfirmMessage.textContent = text(message || "请确认是否继续。");
  if (el.actionConfirmFeedback) el.actionConfirmFeedback.textContent = "";
  el.actionConfirmAccept.textContent = text(confirmText || "确认");
  el.actionConfirmCancel.textContent = text(cancelText || "取消");
  token.onCancel = event => {
    event.preventDefault();
    settleActionConfirmation(token, false);
  };
  token.onClose = () => {
    if (dialog.open) return;
    if (!token.settled) settleActionConfirmation(token, false);
  };
  token.onKeydown = event => {
    if (event.key !== "Tab") return;
    const active = document.activeElement;
    if (event.shiftKey && active === el.actionConfirmCancel) {
      event.preventDefault();
      el.actionConfirmAccept.focus?.();
    } else if (!event.shiftKey && active === el.actionConfirmAccept) {
      event.preventDefault();
      el.actionConfirmCancel.focus?.();
    }
  };
  dialog.addEventListener("cancel", token.onCancel);
  dialog.addEventListener("close", token.onClose);
  dialog.addEventListener("keydown", token.onKeydown);
  try {
    dialog.showModal();
    if (!dialog.open) throw new Error("dialog did not open");
    el.actionConfirmCancel.focus?.();
  } catch (_) {
    dialog.removeEventListener?.("cancel", token.onCancel);
    dialog.removeEventListener?.("close", token.onClose);
    dialog.removeEventListener?.("keydown", token.onKeydown);
    token.settled = true;
    token.resolve(false);
    state.confirmationToken = null;
    renderLifeDraft();
    setNotice("确认窗口未能打开，操作已停止。请刷新页面后重试。", "error");
    return null;
  }
  return { token, promise };
}

function settleActionConfirmation(token, accepted) {
  if (!token || token !== state.confirmationToken || token.settled) return;
  token.settled = true;
  el.actionConfirmDialog?.removeEventListener?.("cancel", token.onCancel);
  el.actionConfirmDialog?.removeEventListener?.("close", token.onClose);
  el.actionConfirmDialog?.removeEventListener?.("keydown", token.onKeydown);
  let closeFailed = false;
  try {
    if (el.actionConfirmDialog?.open) el.actionConfirmDialog.close();
  } catch (_) {
    closeFailed = true;
  }
  if (el.actionConfirmDialog?.open) closeFailed = true;
  if (closeFailed) setNotice("确认窗口未能关闭，操作已停止。请刷新页面后重试。", "error");
  token.resolve(Boolean(accepted) && !closeFailed);
}

function releaseActionConfirmation(token) {
  if (!token || state.confirmationToken !== token) return;
  if (!token.settled) settleActionConfirmation(token, false);
  state.confirmationToken = null;
  if (el.actionConfirmFeedback) el.actionConfirmFeedback.textContent = "";
  renderLifeDraft();
  const opener = token.opener;
  queueMicrotask(() => {
    if (!document.hidden && opener && opener.isConnected !== false && !opener.disabled && !opener.hidden) opener.focus?.();
  });
}

function cancelActiveActionConfirmation() {
  const token = state.confirmationToken;
  if (!token) return;
  settleActionConfirmation(token, false);
  releaseActionConfirmation(token);
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
      const error = new Error(result.message || fallbackMessage);
      error.code = result.code || result.error?.code || "PAGE_ERROR";
      throw error;
    }
    if (Object.prototype.hasOwnProperty.call(result, "ok")) {
      if (!result.ok) {
        const error = new Error(result.error?.message || result.message || fallbackMessage);
        error.code = result.error?.code || result.code || "PAGE_ERROR";
        throw error;
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
    ? text(item.source || item.data_url || item.url || item.preview_url || item.previewUrl)
    : text(item);
  if (!source) return "";
  if (source.startsWith("base64://")) {
    const mimeType = typeof item === "object" && item ? text(item.mime_type || item.content_type || "image/jpeg") : "image/jpeg";
    return `data:${mimeType || "image/jpeg"};base64,${source.replace("base64://", "")}`;
  }
  return source;
}

function renderRichText(target, fallback, segments, emojiClass = "qzone-emoji") {
  const entries = Array.isArray(segments) ? segments : [];
  if (!entries.length) {
    target.textContent = cleanDisplayText(fallback);
    return;
  }
  target.replaceChildren();
  for (const segment of entries) {
    if (segment?.type === "emoji" && segment.source) {
      const image = document.createElement("img");
      image.className = emojiClass;
      image.src = text(segment.source);
      image.alt = text(segment.alt || "[表情]");
      image.loading = "lazy";
      target.append(image);
      continue;
    }
    const value = cleanDisplayText(segment?.text);
    if (value) {
      const span = document.createElement("span");
      span.textContent = value;
      target.append(span);
    }
  }
}

function mediaDedupeKey(source) {
  let value = text(source).trim().replace(/&amp;/g, "&").replace(/\\\//g, "/");
  if (!value) return "";
  if (value.startsWith("//")) value = `https:${value}`;
  if (!/^https?:\/\//i.test(value)) return value;
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    const path = decodeURIComponent(url.pathname || "");
    let query = decodeURIComponent(url.search || "")
      .replace(/([?&])(?:w|h)=\d+(?=&|$)/gi, "$1")
      .replace(/\?&/g, "?")
      .replace(/[?&]$/g, "");
    if (path.toLowerCase() === "/psc" && query.startsWith("?/")) {
      const pscPayload = query
        .slice(1)
        .replace(/\/(?:a|m|s)(?=&|$)/gi, "/b")
        .replace(/&(?:bo|w|h)=[^&]*/gi, "")
        .replace(/[!&?]+$/g, "");
      if (pscPayload) return `qzone-psc:${pscPayload}`;
    }
    return `url:${host}${path}${query}`;
  } catch (_) {
    return value.toLowerCase();
  }
}

function dedupeMediaEntries(items) {
  const seen = new Set();
  const result = [];
  for (const item of items || []) {
    const source = mediaDisplaySource(item);
    if (!source) continue;
    const key = mediaDedupeKey(source) || source;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push({ item, source });
  }
  return result;
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

function mediaKind(item, source = "") {
  if (isVideoMediaItem(item, source)) return "video";
  if (item && typeof item === "object") {
    const kind = text(item.kind || item.type || item.raw_type).toLowerCase();
    const mimeType = text(item.mime_type || item.content_type || item.mime).toLowerCase();
    if (kind === "audio" || kind === "music" || kind === "record" || mimeType.startsWith("audio/")) return "audio";
    if (kind === "file" || kind === "attachment") return "file";
    if (kind === "image" || mimeType.startsWith("image/")) return "image";
  }
  return /\.(mp3|m4a|aac|wav|ogg|flac)(?:[?#].*)?$/i.test(source) ? "audio" : "image";
}

function mediaDownloadSource(item, source = "") {
  if (item && typeof item === "object") {
    return text(item.download_url || item.downloadUrl || item.original_url || item.originalUrl || source);
  }
  return text(source || item);
}

function mediaName(item, kind) {
  const name = item && typeof item === "object"
    ? text(item.name || item.filename || item.file_name || item.title).trim()
    : "";
  return name || ({ image: "QQ空间原图", video: "QQ空间视频", audio: "QQ空间音频", file: "QQ空间附件" }[kind] || "QQ空间文件");
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
      state.composerRevision += 1;
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
  pane?.setAttribute("aria-hidden", String(isDetailDrawerMode()));
  document.body?.classList?.remove("detail-drawer-open");
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


function openLightbox(item) {
  const url = mediaDisplaySource(item);
  const kind = mediaKind(item, url);
  let lightbox = document.getElementById("lightbox");
  if (!lightbox) {
    lightbox = document.createElement("div");
    lightbox.id = "lightbox";
    lightbox.className = "lightbox";
    
    document.body.appendChild(lightbox);
  }
  
  const cleanup = () => {
    lightbox.classList.remove("visible");
    const playable = lightbox.querySelector("video") || lightbox.querySelector("audio");
    if (playable) {
        playable.pause();
        playable.src = "";
        playable.removeAttribute("src");
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
  if (kind === "video") {
    mediaElement = document.createElement("video");
    mediaElement.src = url;
    mediaElement.controls = true;
    mediaElement.autoplay = true;
    mediaElement.playsInline = true;
  } else if (kind === "audio") {
    mediaElement = document.createElement("audio");
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
  const entries = dedupeMediaEntries(items);
  if (!entries.length) return null;

  const media = document.createElement("div");
  media.className = `${className} media-grid ${mediaLayoutClass(entries.length)}`;
  for (const { item, source: url } of entries) {
    const kind = mediaKind(item, url);
    const tile = document.createElement("div");
    tile.className = `media-tile media-${kind}`;
    if (kind === "video") {
      const video = document.createElement("video");
      video.src = url;
      video.className = "preview-video";
      video.controls = true;
      video.preload = "metadata";
      video.playsInline = true;
      video.referrerPolicy = "no-referrer";
      video.poster = text(item?.preview_url || item?.previewUrl);
      tile.append(video);
    } else if (kind === "audio") {
      const audio = document.createElement("audio");
      audio.src = url;
      audio.controls = true;
      audio.preload = "metadata";
      tile.append(audio);
    } else if (kind === "file") {
      const file = document.createElement("div");
      file.className = "file-preview";
      file.textContent = mediaName(item, kind);
      tile.append(file);
    } else {
      const image = document.createElement("img");
      image.loading = "lazy";
      image.alt = "说说图片";
      image.src = url;
      image.referrerPolicy = "no-referrer";
      image.addEventListener("click", (event) => {
        event.stopPropagation();
        openLightbox(item);
      });
      tile.append(image);
    }
    const download = document.createElement("button");
    download.type = "button";
    download.className = "media-download";
    download.textContent = "下载原文件";
    download.addEventListener("click", async (event) => {
      event.stopPropagation();
      download.disabled = true;
      try {
        if (item?.media_id && typeof bridge?.download === "function") {
          await bridge.download("page/media", { id: item.media_id, download: 1 }, mediaName(item, kind));
        } else {
          window.open(mediaDownloadSource(item, url), "_blank", "noopener,noreferrer");
        }
      } catch (error) {
        showToast(error?.message || "下载失败，请刷新后重试。", "error");
      } finally {
        download.disabled = false;
      }
    });
    tile.append(download);
    media.append(tile);
  }
  return media;
}

function renderPostCard(post) {
  rememberPostAuthors(post);
  const card = document.createElement("article");
  card.className = `post${state.selected?.id === post.id ? " selected" : ""}`;
  card.dataset.id = post.id;
  if (state.selected?.id === post.id) {
    card.setAttribute?.("aria-current", "true");
  }

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
  renderRichText(name, authorName, post.author?.segments, "qzone-emoji nickname-emoji");
  const time = document.createElement("span");
  time.textContent = formatTime(post.created_at);
  meta.append(name, time);
  head.append(avatar, meta);

  card.append(head);

  const contentText = cleanDisplayText(post.content);
  if (contentText) {
    const body = document.createElement("p");
    body.className = "post-content";
    renderRichText(body, contentText, post.content_segments);
    card.append(body);
  }

  const images = renderMediaGrid(post.media?.length ? post.media : post.images, "post-media");
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
  const previousId = state.selected?.id;
  const scrollBefore = previousId === post.id ? detailScrollTop() : 0;
  state.selected = mergePost(state.selected?.id === post.id ? state.selected : {}, post);
  post = state.selected;
  rememberPostAuthors(post);
  if (!options.loading) {
    cacheDetailPost(post);
  }
  const isLoading = Boolean(options.loading || state.detailLoadingId === post.id);
  el.detailEmpty.hidden = true;
  el.detailContent.hidden = false;
  el.detailPane.classList.add("has-selection");
  el.detailPane.dataset.postId = post.id;
  el.detailContent.setAttribute?.("aria-busy", isLoading ? "true" : "false");
  el.detailContent.replaceChildren();
  syncSelectedFeedCard(post.id);

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
  renderRichText(name, authorName, post.author?.segments, "qzone-emoji nickname-emoji");
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
    renderRichText(content, contentText, post.content_segments);
    el.detailContent.append(content);
  }

  const detailMedia = renderMediaGrid(post.media?.length ? post.media : post.images, "detail-media");
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
  input.value = state.commentDrafts.get(post.id) || "";
  input.addEventListener("input", () => {
    state.commentDrafts.set(post.id, input.value);
  });
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = "发送";
  submit.disabled = state.pendingComments.has(post.id);
  const aiButton = document.createElement("button");
  aiButton.type = "button";
  aiButton.className = "ghost ai-comment-draft";
  aiButton.textContent = "AI 生成评论";
  aiButton.setAttribute("aria-label", "AI 生成评论");
  const aiStatus = document.createElement("span");
  aiStatus.className = "muted ai-draft-status";
  aiStatus.setAttribute("role", "status");
  aiButton.addEventListener("click", async () => {
    if (aiButton.disabled) return;
    const targetId = post.id;
    const originalValue = input.value;
    aiButton.disabled = true;
    aiButton.textContent = "正在生成…";
    aiStatus.textContent = "正在生成评论草稿";
    try {
      const data = await apiPost("page/ai-comment", { id: targetId });
      if (!input.isConnected || selectedPostId() !== targetId || input.value !== originalValue) return;
      input.value = text(data.content || "");
      state.commentDrafts.set(targetId, input.value);
      aiStatus.textContent = "草稿已填入，可编辑后手动发送";
    } catch (error) {
      if (input.isConnected && selectedPostId() === targetId) aiStatus.textContent = error.message || "生成评论草稿失败";
    } finally {
      aiButton.disabled = false;
      aiButton.textContent = "AI 生成评论";
    }
  });
  form.append(input, aiButton, submit, aiStatus);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const postId = post.id;
    if (state.pendingComments.has(postId) || !input.value.trim()) return;
    state.commentDrafts.set(postId, input.value);
    state.pendingComments.add(postId);
    renderSelectedIfNeeded(postId);
    try {
      if (await sendComment(post, input.value)) state.commentDrafts.delete(postId);
    } finally {
      state.pendingComments.delete(postId);
      renderSelectedIfNeeded(postId);
    }
  });

  el.detailContent.append(comments, form);
  if (previousId === post.id && options.preserveScroll !== false) {
    restoreDetailScroll(scrollBefore);
  } else if (previousId !== post.id) {
    el.detailPane.scrollTop = 0;
  }
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
    const aiButton = document.createElement("button");
    aiButton.type = "button";
    aiButton.className = "ghost ai-reply-draft";
    aiButton.textContent = "AI 生成回复";
    aiButton.setAttribute("aria-label", "AI 生成回复");
    const aiStatus = document.createElement("span");
    aiStatus.className = "muted ai-draft-status";
    aiStatus.setAttribute("role", "status");
    aiButton.addEventListener("click", async () => {
      if (aiButton.disabled) return;
      const postId = post.id;
      const commentId = text(comment.id);
      const commentUin = Number(comment.author?.uin || comment.uin || 0);
      const originalValue = input.value;
      aiButton.disabled = true;
      aiButton.textContent = "正在生成…";
      aiStatus.textContent = "正在生成回复草稿";
      try {
        const data = await apiPost("page/ai-reply", { id: postId, commentid: commentId, comment_uin: commentUin });
        if (!input.isConnected || state.replyTarget?.postId !== postId || state.replyTarget?.commentId !== commentId || input.value !== originalValue) return;
        input.value = text(data.content || "");
        state.replyDrafts.set(targetKey, input.value);
        aiStatus.textContent = "草稿已填入，可编辑后手动发送";
      } catch (error) {
        if (input.isConnected && state.replyTarget?.postId === postId && state.replyTarget?.commentId === commentId) {
          aiStatus.textContent = error.message || "生成回复草稿失败";
        }
      } finally {
        aiButton.disabled = false;
        aiButton.textContent = "AI 生成回复";
      }
    });
    actions.append(cancel, aiButton, submit, aiStatus);
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
  const previousUin = String(state.status?.login?.uin || "");
  const data = await apiGet("page/status");
  state.status = data;
  const currentUin = String(data?.login?.uin || "");
  if (state.lifeAssociation && currentUin !== String(state.lifeAssociation.accountUin)) {
    invalidateLifeAssociation("登录账号已变化，日常草稿关联已锁定；清空后才能作为普通内容重新编辑。");
  } else if (previousUin && previousUin !== currentUin && state.lifeJob?.account_uin !== Number(currentUin)) {
    state.lifeJob = null;
  }
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
            pane.setAttribute("aria-hidden", "false");
            if (closeBtn) closeBtn.hidden = false;
            if (backdrop) backdrop.classList.add("visible");
            document.body?.classList?.add("detail-drawer-open");
        } else {
            pane.classList.remove("visible");
            pane.setAttribute("aria-hidden", "true");
            if (closeBtn) closeBtn.hidden = true;
            if (backdrop) backdrop.classList.remove("visible");
            document.body?.classList?.remove("detail-drawer-open");
            state.selected = null;
            state.detailLoadingId = "";
            syncSelectedFeedCard("");
        }
    } else {
        clearDetailDrawerChrome();
        // Desktop detail stays pinned in its grid column; do not scroll the
        // page when opening a photo detail, otherwise image loads can nudge
        // the viewport and make the layout look crooked.
    }
}

window.addEventListener?.("resize", () => {
  if (!isDetailDrawerMode()) {
    clearDetailDrawerChrome();
  }
});

async function openDetail(id, focusComment = false) {
  const cached = cachedDetailPost(id) || currentPostById(id);
  const requestSeq = ++state.detailRequestSeq;
  const versionAtRequestStart = localVersion(id);
  if (state.selected?.id !== id) {
    state.replyTarget = null;
  }
  toggleDetailDrawer(true);
  const hasFreshDetail = Boolean(cachedDetailPost(id));
  state.detailLoadingId = hasFreshDetail ? "" : id;
  if (cached) {
    renderDetail(cached, { loading: !hasFreshDetail, preserveScroll: state.selected?.id === id });
    if (focusComment) {
      setTimeout(() => {
        el.detailContent.querySelector(".comment-form textarea")?.focus();
      }, 0);
    }
  }
  if (hasFreshDetail) {
    return;
  }
  try {
    const data = await fetchDetailPost(id);
    if (requestSeq !== state.detailRequestSeq) return;
    state.detailLoadingId = "";
    const incoming = mergeRemotePost(data.post.id, data.post, versionAtRequestStart);
    const merged = updatePost(incoming.id, incoming) || incoming;
    rememberPostAuthors(merged);
    renderDetail(merged, { preserveScroll: true });
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
      renderDetail(currentPostById(id, cached), { preserveScroll: true });
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
    setNotice(error.message || "评论失败", "error");
    return false;
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

function setLifeDraftStatus(message, tone = "info") {
  if (!el.lifeDraftStatus) return;
  el.lifeDraftStatus.textContent = text(message || "");
  el.lifeDraftStatus.dataset.tone = tone;
}

const LIFE_RECOVERY_SESSION_KEY = "qzoneLifeDraftRecovery";

function invalidateLifeAssociation(message) {
  if (state.lifeAssociation) state.lifeAssociationInvalid = message || "该日常草稿已失效。";
  if (message) setLifeDraftStatus(message, "warn");
  renderLifeDraft();
}

function saveLifeRecovery() {
  try {
    if (state.lifeRecovery) sessionStorage.setItem(LIFE_RECOVERY_SESSION_KEY, JSON.stringify(state.lifeRecovery));
    else sessionStorage.removeItem(LIFE_RECOVERY_SESSION_KEY);
  } catch (_) {}
}

function restoreLifeRecovery() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(LIFE_RECOVERY_SESSION_KEY) || "null");
    if (saved && Number.isFinite(Number(saved.startedAt))) state.lifeRecovery = saved;
  } catch (_) {}
}

function clearLifePoll() {
  if (state.lifePollTimer) window.clearTimeout(state.lifePollTimer);
  state.lifePollTimer = null;
}

function scheduleLifePoll(jobId, delay = 1400) {
  clearLifePoll();
  if (document.hidden || (!jobId && !state.lifeRecovery)) return;
  state.lifePollTimer = window.setTimeout(() => {
    state.lifePollTimer = null;
    refreshLifeDraft(jobId || "", { poll: true, recovery: !jobId });
  }, delay);
}

const lifeDiagnosticMessages = {
  INTERFACE_ERROR: "调用接口时发生错误", REQUEST_ERROR: "请求处理失败",
  SOURCE_OR_FORMAT_ERROR: "媒体来源或格式未通过处理", AUTH_REQUIRED: "登录状态需要重新确认",
  DAEMON_UNAVAILABLE: "后台服务暂不可用", TIMEOUT: "操作等待超时",
  CANCELLED: "操作被取消，结果按未确认保留", INTERNAL_ERROR: "内部处理发生错误",
  SOURCE_TOO_LARGE: "图片来源超过大小限制", ENCODED_SOURCE_TOO_LARGE: "图片编码内容超过大小限制",
  SOURCE_MISSING: "图片来源缺失", SOURCE_UNTRUSTED: "图片来源未通过本地校验",
  PIXEL_LIMIT: "图片尺寸超过处理限制", RASTER_UNSUPPORTED: "图片格式不受支持",
  RASTER_DECODE_FAILED: "图片解码失败", OUTPUT_TOO_LARGE: "预览图片超过输出限制",
  REMOTE_POLICY_FAILED: "图片来源校验未通过", PREVIEW_TIMEOUT: "预览读取或处理超时",
};
const lifeDiagnosticPhases = new Set(["controller_call", "result_processing", "bookkeeping", "preview_read", "preview_raster", "preview_wait"]);

function acceptedLifeDiagnostic(value, allowedPhases) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (!Object.hasOwn(lifeDiagnosticMessages, value.code) || !lifeDiagnosticPhases.has(value.phase)
      || !allowedPhases.has(value.phase) || !/^[0-9a-f]{12}$/.test(value.id || "")) return null;
  return { id: value.id, code: value.code, phase: value.phase };
}

function renderLifeDiagnostics(job) {
  if (!el.lifeDraftDiagnostics) return;
  const rows = [];
  const publish = acceptedLifeDiagnostic(job?.publish_diagnostic, new Set(["controller_call", "result_processing", "bookkeeping"]));
  const preview = acceptedLifeDiagnostic(job?.preview_diagnostic, new Set(["preview_read", "preview_raster", "preview_wait"]));
  if (publish) rows.push(`发布结果仍按“${job?.publish_state === "unknown" ? "未确认" : "已记录状态"}”处理；诊断：${publish.code}：${lifeDiagnosticMessages[publish.code]}（${publish.phase}，编号 ${publish.id}）。`);
  if (preview) rows.push(`仅图片预览诊断（不说明发布原因）：${preview.code}：${lifeDiagnosticMessages[preview.code]}（${preview.phase}，编号 ${preview.id}）；原草稿媒体仍保留。`);
  const previewReference = job?.preview_diagnostic_reference;
  if (!preview && previewReference && Object.hasOwn(lifeDiagnosticMessages, previewReference.code) && /^[0-9a-f]{12}$/.test(previewReference.id || "")) {
    rows.push(`仅图片预览诊断：${previewReference.code}：${lifeDiagnosticMessages[previewReference.code]}（阶段待任务状态查询确认，编号 ${previewReference.id}）；原草稿媒体仍保留。`);
  }
  el.lifeDraftDiagnostics.textContent = rows.join(" ");
  el.lifeDraftDiagnostics.hidden = rows.length === 0;
}

function renderLifeDraft() {
  const job = state.lifeJob;
  if (!el.generateLifeDraft || !el.lifeDraftResult) return;
  if (el.mediaInput) {
    const mediaDisabled = Boolean(state.confirmationToken || state.lifeLoadPending || state.publishPending);
    el.mediaInput.disabled = mediaDisabled;
    el.mediaInput.parentNode?.classList?.toggle("is-disabled", mediaDisabled);
    if (mediaDisabled) el.mediaInput.parentNode?.setAttribute?.("aria-disabled", "true");
    else el.mediaInput.parentNode?.removeAttribute?.("aria-disabled");
  }
  const recovering = Boolean(state.lifeRecovery);
  el.generateLifeDraft.textContent = recovering ? "查询已有任务" : "生成日常草稿";
  el.generateLifeDraft.disabled = state.lifeRequestPending || job?.state === "running" || !state.status?.login?.uin;
  el.generateLifeDraft.setAttribute("aria-busy", String(Boolean(state.lifeRequestPending || job?.state === "running")));
  el.lifeDraftResult.hidden = job?.state !== "succeeded" && !state.lifeAssociation;
  renderLifeDiagnostics(job);
  if (el.discardLifeDraft) {
    el.discardLifeDraft.hidden = !state.lifeAssociation;
    el.discardLifeDraft.disabled = state.publishPending || state.lifeLoadPending || state.pendingUploads > 0;
  }
  if (el.loadLifeDraft) el.loadLifeDraft.disabled = state.lifeLoadPending || state.publishPending || state.pendingUploads > 0 || Boolean(state.lifeAssociation) || (job?.state === "succeeded" && job.publish_state !== "ready");
  if (el.lifeDraftLongContent) {
    const longText = job?.state === "succeeded" && text(job.content).length > 1200 ? text(job.content) : "";
    el.lifeDraftLongContent.hidden = !longText;
    el.lifeDraftLongContent.value = longText;
  }
  if (state.lifeRecoveryExpired) {
    setLifeDraftStatus("任务查询超过保留时间；确认后可重新生成。", "warn");
    return;
  }
  if (recovering) {
    const age = Date.now() - Number(state.lifeRecovery.startedAt || Date.now());
    setLifeDraftStatus(age >= 15 * 60 * 1000
      ? "任务查询超过保留时间；确认后可重新生成。"
      : "生成请求结果待确认；点击“查询已有任务”仅查询状态，不会再次提交生成。", "warn");
    return;
  }
  if (state.lifeAssociationInvalid) {
    setLifeDraftStatus(state.lifeAssociationInvalid, "warn");
    return;
  }
  if (state.lifeAssociation) {
    if (state.lifeAssociation.publishState === "published" || job?.publish_state === "published") {
      setLifeDraftStatus("该日常草稿已确认发布；不会再次发送。需要新内容请先明确清空日常稿。", "success");
    } else if (state.lifeAssociation.uncertain || ["sending", "unknown"].includes(job?.publish_state)) {
      setLifeDraftStatus("该日常草稿的发布结果尚未确认；仅查询状态，不会重发。", "warn");
    } else {
      setLifeDraftStatus(`已载入草稿 ${job?.draft_id || "—"}。${state.lifeAssociationPreviewUnavailable ? "图片预览暂不可用，原媒体仍保留。" : ""}编辑后点击“发布说说”并确认；不会自动发布。`, state.lifeAssociationPreviewUnavailable ? "warn" : "success");
    }
    if (job?.state === "succeeded") renderLifeDraftPreviews(job);
    return;
  }
  if (!job || job.state === "idle") {
    const cap = job?.capability || {};
    const status = [cap.life_scheduler ? `Life Scheduler：${cap.life_scheduler}` : "", cap.omnidraw ? `OmniDraw：${cap.omnidraw}` : "", cap.provider ? `模型：${cap.provider}` : ""].filter(Boolean).join("；");
    setLifeDraftStatus(`${cap.message || "可按需生成一份可编辑的日常草稿。"}${status ? `（${status}）` : ""}`, cap.available === false ? "warn" : "info");
  } else if (job.state === "running") {
    setLifeDraftStatus(job.message || "正在生成日常草稿…");
  } else if (job.state === "succeeded") {
    if (job.publish_state === "published") {
      setLifeDraftStatus("该日常草稿已确认发布；不会再次发送。需要新内容请先明确清空日常稿。", "success");
    } else if (["sending", "unknown"].includes(job.publish_state)) {
      setLifeDraftStatus("该日常草稿的发布结果尚未确认；仅查询状态，不会重发。", "warn");
    } else if (job.publish_state !== "ready") {
      setLifeDraftStatus("该日常草稿的发布状态无法确认；载入已停止，请重新查询状态。", "warn");
    } else {
      setLifeDraftStatus(`${job.message || "日常草稿已保存。"} 草稿编号 ${job.draft_id || "—"}，目标 QQ ${job.account_uin}。`, "success");
    }
    renderLifeDraftPreviews(job);
  } else {
    setLifeDraftStatus(job.message || "日常草稿任务已结束。", job.state === "failed" ? "error" : "info");
  }
}

async function renderLifeDraftPreviews(job) {
  if (!el.lifeDraftPreview || job !== state.lifeJob) return;
  el.lifeDraftPreview.replaceChildren();
  const media = Array.isArray(job.media) ? job.media : [];
  if (!media.length) {
    const label = document.createElement("span");
    label.className = "life-draft-no-media";
    label.textContent = job.has_media ? "图片预览暂不可用，原媒体仍保留。" : "纯文字草稿";
    el.lifeDraftPreview.append(label);
    return;
  }
  for (const item of media.slice(0, 9)) {
    const figure = document.createElement("figure");
    figure.className = "life-draft-thumb";
    const image = document.createElement("img");
    image.alt = text(item.name || "日常草稿图片");
    image.dataset.mediaId = text(item.media_id);
    const caption = document.createElement("figcaption");
    caption.textContent = text(item.name || "日常图片");
    figure.append(image, caption);
    el.lifeDraftPreview.append(figure);
    try {
      const preview = await apiGet("page/life-draft-preview", { job_id: job.id, media_id: item.media_id });
      if (state.lifeJob === job && state.lifeJob.state === "succeeded" && preview.data_url) image.src = preview.data_url;
      else image.alt = "预览暂不可用";
    } catch (error) {
      image.alt = "预览暂不可用";
      figure.classList.add("is-unavailable");
      const diagnosticMatch = /([A-Z_]+) ([0-9a-f]{12})$/.exec(String(error?.message || ""));
      if (diagnosticMatch && Object.hasOwn(lifeDiagnosticMessages, diagnosticMatch[1])
          && state.lifeJob === job && Number(state.status?.login?.uin || 0) === Number(job.account_uin)) {
        job.preview_diagnostic_reference = { code: diagnosticMatch[1], id: diagnosticMatch[2] };
        renderLifeDiagnostics(job);
      }
    }
  }
}

async function refreshLifeDraft(jobId = "", { poll = false, recovery = false } = {}) {
  try {
    const job = await apiGet("page/life-draft", jobId ? { id: jobId } : {});
    if (recovery && state.lifeRecovery) {
      const elapsed = Date.now() - Number(state.lifeRecovery.startedAt || Date.now());
      if (elapsed >= 15 * 60 * 1000) {
        state.lifeRecovery = null;
        state.lifeRecoveryExpired = true;
        saveLifeRecovery();
      } else if (job?.state === "idle") {
        setLifeDraftStatus("未找到已接收任务，正在仅查询最新状态；不会自动重新生成。", "warn");
      } else if (job?.state === "failed" || job?.state === "cancelled") {
        state.lifeRecovery = null;
        state.lifeRecoveryExpired = false;
        saveLifeRecovery();
      } else {
        state.lifeRecovery = null;
        state.lifeRecoveryExpired = false;
        saveLifeRecovery();
      }
    }
    state.lifeJob = job || { state: "idle" };
    if (state.lifeAssociation && (Number(job?.account_uin || 0) !== Number(state.lifeAssociation.accountUin) || String(job?.id || "") !== String(state.lifeAssociation.jobId))) {
      invalidateLifeAssociation("原日常草稿无法在当前账号/查询结果中验证；不会作为普通内容发送。请明确清空后重新编辑。");
    } else if (state.lifeAssociation && job?.publish_state === "published") {
      state.lifeAssociation.publishState = "published";
      state.lifeAssociation.uncertain = false;
      state.lifeAssociationInvalid = "";
    } else if (state.lifeAssociation && ["sending", "unknown"].includes(job?.publish_state)) {
      state.lifeAssociation.publishState = job.publish_state;
      state.lifeAssociation.uncertain = true;
    }
    renderLifeDraft();
    if (job?.state === "running") scheduleLifePoll(job.id, poll ? 1800 : 800);
    else if (state.lifeRecovery) scheduleLifePoll("", 3000);
    else clearLifePoll();
    return job;
  } catch (error) {
    if (jobId && ["LIFE_EXPIRED", "LIFE_ACCOUNT_CHANGED"].includes(error.code)) {
      invalidateLifeAssociation(`${error.message || "该日常草稿已失效。"} 清空草稿前不会按普通内容发送。`);
    }
    if (poll) {
      if (state.lifeRecovery) setLifeDraftStatus(`任务接收状态待确认：${error.message || "查询暂不可用"}。可点“查询已有任务”重试；不会重新生成。`, "warn");
      else setLifeDraftStatus(`状态查询失败：${error.message || "网络暂不可用"}；可稍后重新查询，不会重新开始生成。`, "warn");
      scheduleLifePoll(jobId, 3000);
    } else {
      setLifeDraftStatus(error.message || "日常草稿状态暂不可用。", "warn");
    }
    return null;
  }
}

async function startLifeDraft() {
  if (state.lifeRecovery) {
    await refreshLifeDraft("", { poll: true, recovery: true });
    return;
  }
  if (state.lifeRequestPending || state.lifeJob?.state === "running") return;
  state.lifeRequestPending = true;
  state.lifeRequestId = (globalThis.crypto?.randomUUID?.() || `life_${Date.now()}_${Math.random().toString(36).slice(2)}`).replace(/[^A-Za-z0-9_-]/g, "").slice(0, 64);
  renderLifeDraft();
  setLifeDraftStatus("正在提交草稿生成任务…");
  const requestId = state.lifeRequestId;
  try {
    const job = await apiPost("page/life-draft", { action: "generate_draft", request_id: requestId });
    state.lifeRecovery = null;
    state.lifeRecoveryExpired = false;
    saveLifeRecovery();
    state.lifeJob = job;
    renderLifeDraft();
    if (job?.state === "running") scheduleLifePoll(job.id);
  } catch (error) {
    state.lifeRecovery = { requestId, startedAt: Date.now() };
    state.lifeRecoveryExpired = false;
    saveLifeRecovery();
    setLifeDraftStatus(`${error.message || "请求结果暂不明确"} 正在查询已有任务，不会自动重发生成请求。`, "warn");
    await refreshLifeDraft("", { poll: true, recovery: true });
  } finally {
    state.lifeRequestPending = false;
    renderLifeDraft();
  }
}

async function loadLifeDraftIntoComposer() {
  const job = state.lifeJob;
  if (state.confirmationToken) {
    actionBusyFeedback();
    return;
  }
  if (!job || job.state !== "succeeded" || job.publish_state !== "ready") {
    setLifeDraftStatus("当前没有处于可载入状态的已完成日常草稿；请重新查询后再试。", "warn");
    return;
  }
  if (state.lifeLoadPending || state.publishPending || state.pendingUploads > 0 || state.lifeAssociation) {
    setNotice("编辑器当前有操作或已载入日常稿，请完成后再载入。", "warn");
    return;
  }
  if (Number(state.status?.login?.uin || 0) !== Number(job.account_uin)) {
    invalidateLifeAssociation("登录账号已变化；请清空当前日常稿或切回原账号重新查询。");
    return;
  }
  const accountUin = Number(state.status?.login?.uin || 0);
  const jobId = String(job.id || "");
  const revision = state.composerRevision;
  const originalContent = text(el.publishContent.value);
  const originalMediaJson = JSON.stringify(state.media.map(mediaForPublish));
  const originalMediaIds = JSON.stringify((job.media || []).map(item => text(item.media_id)));
  const baseIntentCurrent = () => state.composerRevision === revision
    && (!actionToken || state.confirmationToken === actionToken)
    && text(el.publishContent.value) === originalContent
    && JSON.stringify(state.media.map(mediaForPublish)) === originalMediaJson
    && Number(state.status?.login?.uin || 0) === accountUin
    && state.lifeJob?.id === jobId
    && Number(state.lifeJob?.account_uin || 0) === accountUin
    && state.lifeJob?.state === "succeeded"
    && state.lifeJob?.publish_state === "ready"
    && state.pendingUploads === 0
    && !state.publishPending
    && !state.lifeAssociation;
  let actionToken = null;
  state.lifeLoadPending = true;
  renderLifeDraft();
  try {
    const dirty = Boolean(originalContent || state.media.length);
    if (dirty) {
      const action = beginActionConfirmation({
        title: "替换当前编辑内容？",
        message: "载入日常草稿会替换当前文字和媒体。是否继续？",
        confirmText: "载入草稿",
      });
      if (!action) return;
      actionToken = action.token;
      if (!await action.promise) {
        setNotice("已取消载入；当前编辑内容和媒体保持不变。", "info");
        return;
      }
      if (!baseIntentCurrent()) {
        setNotice("确认期间编辑器、账号或草稿状态已变化；当前内容保持不变，请重新检查后载入。", "warn");
        return;
      }
    }
    const previews = [];
    let loadInvalidated = false;
    for (const item of job.media || []) {
      let previewUrl = "";
      try {
        const preview = await apiGet("page/life-draft-preview", { job_id: jobId, media_id: item.media_id });
        previewUrl = preview.data_url || "";
      } catch (error) {
        if (["LIFE_EXPIRED", "LIFE_ACCOUNT_CHANGED"].includes(error.code)) {
          loadInvalidated = true;
          setLifeDraftStatus(`${error.message} 草稿载入已停止。`, "warn");
        }
      }
      previews.push({ ...item, preview_url: previewUrl });
    }
    if (loadInvalidated || !baseIntentCurrent()) {
      setNotice("载入期间账号、草稿或编辑器发生变化；已保留当前内容，请重新查询后再载入。", "warn");
      return;
    }
    let verifiedJob = null;
    try {
      verifiedJob = await apiGet("page/life-draft", { id: jobId });
    } catch (error) {
      setNotice(`${error.message || "无法重新验证日常草稿"} 草稿载入已停止。`, "warn");
      return;
    }
    if (!verifiedJob || String(verifiedJob.id || "") !== jobId || Number(verifiedJob.account_uin || 0) !== accountUin || verifiedJob.state !== "succeeded" || !baseIntentCurrent()) {
      setNotice("载入期间账号、草稿或编辑器发生变化；已保留当前内容，请重新查询后再载入。", "warn");
      return;
    }
    const cachedState = state.lifeJob?.publish_state;
    if (cachedState !== "ready" || verifiedJob.publish_state !== "ready") {
      if (verifiedJob.publish_state === "published" || verifiedJob.publish_state === "unknown" || verifiedJob.publish_state === "sending") state.lifeJob = verifiedJob;
      renderLifeDraft();
      setNotice(verifiedJob.publish_state === "published"
        ? "该日常草稿已确认发布；载入已停止，不会再次发送。"
        : verifiedJob.publish_state === "sending" || verifiedJob.publish_state === "unknown"
          ? "该日常草稿的发布结果尚未确认；载入已停止，请查询状态。"
          : "该日常草稿的发布状态无法确认；载入已停止，请重新查询。", "warn");
      return;
    }
    if (JSON.stringify((verifiedJob.media || []).map(item => text(item.media_id))) !== originalMediaIds) {
      setNotice("日常草稿媒体在载入期间发生变化；已保留当前内容，请重新查询。", "warn");
      return;
    }
    state.lifeJob = verifiedJob;
    if (text(verifiedJob.content).length > 1200) {
      renderLifeDraft();
      setNotice("草稿超过编辑器 1200 字限制；下方只读原文可复制，请自行精简后再发布。", "warn");
      return;
    }
    clearMediaQueue();
    el.publishContent.value = text(verifiedJob.content);
    state.media = previews;
    state.lifeAssociation = { jobId: verifiedJob.id, accountUin: Number(verifiedJob.account_uin), uncertain: false };
    state.lifeAssociationInvalid = "";
    state.composerRevision += 1;
    const counter = document.getElementById("charCounter");
    if (counter) counter.textContent = `${el.publishContent.value.length}/1200`;
    renderMedia();
    const unavailablePreview = Boolean(verifiedJob.has_media && previews.some(item => !item.preview_url));
    state.lifeAssociationPreviewUnavailable = unavailablePreview;
    setLifeDraftStatus(`已载入草稿 ${verifiedJob.draft_id || "—"}。${unavailablePreview ? "图片预览暂不可用，原媒体仍保留。" : ""}编辑后点击“发布说说”并确认；不会自动发布。`, unavailablePreview ? "warn" : "success");
    renderLifeDraft();
  } finally {
    state.lifeLoadPending = false;
    renderLifeDraft();
    releaseActionConfirmation(actionToken);
  }
}

async function discardLifeDraftAssociation() {
  const association = state.lifeAssociation;
  if (state.confirmationToken) {
    actionBusyFeedback();
    return;
  }
  if (!association) {
    setLifeDraftStatus("当前没有已载入的日常稿可清空。", "info");
    return;
  }
  const revision = state.composerRevision;
  const currentLoginUin = Number(state.status?.login?.uin || 0);
  const cachedJobSnapshot = {
    id: String(state.lifeJob?.id || ""),
    accountUin: Number(state.lifeJob?.account_uin || 0),
    state: text(state.lifeJob?.state),
    publishState: text(state.lifeJob?.publish_state),
  };
  const associationSnapshot = {
    jobId: String(association.jobId || ""),
    accountUin: Number(association.accountUin || 0),
    invalid: text(state.lifeAssociationInvalid),
    publishState: text(association.publishState),
    uncertain: Boolean(association.uncertain),
  };
  const contentSnapshot = text(el.publishContent.value);
  const mediaSnapshot = JSON.stringify(state.media.map(mediaForPublish));
  const action = beginActionConfirmation({
    title: "清空当前日常稿？",
    message: "这会清除当前编辑器中的日常稿文字和媒体；已保存的服务器草稿会保留。",
    confirmText: "清空日常稿",
  });
  if (!action) return;
  try {
    if (!await action.promise) {
      setNotice("已取消清空；当前日常稿保持不变。", "info");
      return;
    }
    if (state.lifeAssociation !== association || state.composerRevision !== revision
      || text(el.publishContent.value) !== contentSnapshot
      || JSON.stringify(state.media.map(mediaForPublish)) !== mediaSnapshot
      || String(association.jobId || "") !== associationSnapshot.jobId
      || Number(association.accountUin || 0) !== associationSnapshot.accountUin
      || text(state.lifeAssociationInvalid) !== associationSnapshot.invalid
      || text(association.publishState) !== associationSnapshot.publishState
      || Boolean(association.uncertain) !== associationSnapshot.uncertain
      || String(state.lifeJob?.id || "") !== cachedJobSnapshot.id
      || Number(state.lifeJob?.account_uin || 0) !== cachedJobSnapshot.accountUin
      || text(state.lifeJob?.state) !== cachedJobSnapshot.state
      || text(state.lifeJob?.publish_state) !== cachedJobSnapshot.publishState
      || Number(state.status?.login?.uin || 0) !== currentLoginUin
      || state.confirmationToken !== action.token
      || state.lifeLoadPending || state.publishPending || state.pendingUploads > 0) {
      setNotice("确认期间日常稿、账号或编辑器已变化；未清空，请重新检查后操作。", "warn");
      return;
    }
    el.publishContent.value = "";
    clearMediaQueue();
    state.lifeAssociation = null;
    state.lifeAssociationInvalid = "";
    state.lifeAssociationPreviewUnavailable = false;
    state.composerRevision += 1;
    renderMedia();
    const counter = document.getElementById("charCounter");
    if (counter) counter.textContent = "0/1200";
    setLifeDraftStatus("当前日常稿已清空；已保存的原草稿仍保留。", "info");
    renderLifeDraft();
  } finally {
    releaseActionConfirmation(action.token);
  }
}

async function publish(event) {
  event.preventDefault();
  if (state.confirmationToken) {
    actionBusyFeedback();
    return;
  }
  if (state.publishPending || state.lifeLoadPending || state.pendingUploads > 0) {
    setNotice("编辑器操作尚未完成，请稍候再发布。", "warn");
    return;
  }
  const content = text(el.publishContent.value);
  const mediaPayload = state.media.map(mediaForPublish);
  const mediaSnapshot = JSON.stringify(mediaPayload);
  if (!content.trim() && !state.media.length) {
    setNotice("写点文字或添加图片/视频再发布。", "warn");
    return;
  }
  const lifeAssociation = state.lifeAssociation;
  let actionToken = null;
  if (lifeAssociation) {
    if (state.lifeAssociationInvalid) {
      setLifeDraftStatus(`${state.lifeAssociationInvalid} 请用“清空日常稿，重新编辑”明确解除关联。`, "error");
      return;
    }
    if (lifeAssociation.publishState === "published" || state.lifeJob?.publish_state === "published") {
      setLifeDraftStatus("该日常草稿已确认发布，不会再次发送。请明确清空后作为新内容编辑。", "warn");
      return;
    }
    if (Number(state.status?.login?.uin || 0) !== Number(lifeAssociation.accountUin)) {
      invalidateLifeAssociation("登录账号已变化，日常草稿关联已锁定；清空后才能作为普通内容重新编辑。");
      return;
    }
    if (lifeAssociation.uncertain || ["sending", "unknown"].includes(state.lifeJob?.publish_state)) {
      await refreshLifeDraft(lifeAssociation.jobId);
      if (state.lifeJob?.publish_state === "published") setLifeDraftStatus("该日常草稿已确认发布，不会重发。", "success");
      else setLifeDraftStatus("发布结果待确认；已查询任务状态，不会重发此日常草稿。", "warn");
      return;
    }
    const composerRevision = state.composerRevision;
    const originalAssociation = lifeAssociation;
    const originalSelectedJob = state.lifeJob;
    const jobId = String(lifeAssociation.jobId || "");
    const accountUin = Number(lifeAssociation.accountUin || 0);
    const verifiedPayload = { content, media: mediaPayload.map(item => ({ ...item })), life_job_id: jobId };
    let verifiedJob = null;
    const lifeIntentCurrent = () => state.composerRevision === composerRevision
      && state.confirmationToken === actionToken
      && text(el.publishContent.value) === content
      && JSON.stringify(state.media.map(mediaForPublish)) === mediaSnapshot
      && state.lifeAssociation === originalAssociation
      && !state.lifeAssociationInvalid
      && String(state.lifeAssociation?.jobId || "") === jobId
      && Number(state.lifeAssociation?.accountUin || 0) === accountUin
      && Number(state.status?.login?.uin || 0) === accountUin
      && state.lifeJob?.id === jobId
      && (state.lifeJob === originalSelectedJob || (verifiedJob && state.lifeJob === verifiedJob))
      && Number(state.lifeJob?.account_uin || 0) === accountUin
      && state.lifeJob?.state === "succeeded"
      && state.lifeJob?.publish_state === "ready"
      && !state.publishPending
      && !state.lifeLoadPending
      && state.pendingUploads === 0;
    const action = beginActionConfirmation({
      title: "确认发布日常草稿？",
      message: `即将以 QQ ${accountUin} 发布这份已审核的日常草稿。确认后将先重新验证账号和草稿状态，再按当前已审核内容发送一次。`,
      confirmText: "确认发布",
    });
    if (!action) return;
    actionToken = action.token;
    try {
      if (!await action.promise) {
        setNotice("已取消发布；日常草稿和编辑内容保持不变。", "info");
        return;
      }
      if (!lifeIntentCurrent()) {
        setNotice("确认后编辑内容、账号或日常草稿状态已变化；未发送，请重新检查并确认。", "warn");
        return;
      }
      try {
        verifiedJob = await apiGet("page/life-draft", { id: jobId });
      } catch (error) {
        setNotice(`${error.message || "无法重新验证日常草稿"} 未发送，请重新查询后再确认。`, "warn");
        return;
      }
      if (!verifiedJob || String(verifiedJob.id || "") !== jobId || Number(verifiedJob.account_uin || 0) !== accountUin || verifiedJob.state !== "succeeded") {
        setNotice("账号或日常草稿身份无法重新验证；未发送，请重新查询。", "warn");
        return;
      }
      if (!lifeIntentCurrent()) {
        setNotice("重新验证期间编辑器、账号或选中的日常草稿已变化；未发送，请重新检查并确认。", "warn");
        return;
      }
      const currentCachedState = state.lifeJob?.publish_state;
      if (currentCachedState !== "ready") {
        renderLifeDraft();
        setNotice(currentCachedState === "published"
          ? "该日常草稿已确认发布；本次未发送。"
          : "该日常草稿状态已变化；本次未发送，请重新查询。", "warn");
        return;
      }
      state.lifeJob = verifiedJob;
      if (verifiedJob.publish_state !== "ready") {
        renderLifeDraft();
        setNotice(verifiedJob.publish_state === "published"
          ? "该日常草稿已确认发布；本次未发送。"
          : verifiedJob.publish_state === "sending" || verifiedJob.publish_state === "unknown"
            ? "该日常草稿发布结果尚未确认；本次未发送，请查询状态。"
            : "该日常草稿发布状态无法确认；本次未发送，请重新查询。", "warn");
        return;
      }
      if (!lifeIntentCurrent()) {
        setNotice("重新验证后编辑内容、账号或草稿状态已变化；未发送，请重新检查并确认。", "warn");
        return;
      }
      state.publishPending = true;
      renderLifeDraft();
      await publishCapturedPayload(verifiedPayload, { lifeAssociation: originalAssociation, composerRevision });
    } finally {
      releaseActionConfirmation(actionToken);
    }
    return;
  }
  const composerRevision = state.composerRevision;
  await publishCapturedPayload({ content, media: mediaPayload.map(item => ({ ...item })) }, { composerRevision });
}

async function publishCapturedPayload(body, { lifeAssociation = null, composerRevision }) {
  state.publishPending = true;
  el.publishButton.disabled = true;
  const originalText = el.publishButton.textContent;
  el.publishButton.textContent = "发布中...";
  try {
    const data = await apiPost("page/publish", body);
    if (lifeAssociation) {
      lifeAssociation.publishState = "published";
      lifeAssociation.uncertain = false;
      if (state.lifeJob?.id === lifeAssociation.jobId) state.lifeJob.publish_state = "published";
    }
    if (composerRevision === state.composerRevision) {
      el.publishContent.value = "";
      clearMediaQueue();
      state.composerRevision += 1;
      renderMedia();
    }
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
    if (lifeAssociation) {
      lifeAssociation.uncertain = true;
      lifeAssociation.publishState = "unknown";
      if (state.lifeJob?.id === lifeAssociation.jobId) state.lifeJob.publish_state = "unknown";
      await refreshLifeDraft(lifeAssociation.jobId);
    }
    setNotice(error.message || "发布失败", "error");
  } finally {
    state.publishPending = false;
    el.publishButton.disabled = false;
    el.publishButton.textContent = originalText;
    renderLifeDraft();
  }
}

async function uploadFiles(files) {
  if (state.publishPending) return;
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
    state.pendingUploads += 1;
    state.composerRevision += 1;
    renderLifeDraft();
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
      state.composerRevision += 1;
    } catch (error) {
      setNotice(error.message || "媒体上传失败", "error");
    } finally {
      state.pendingUploads = Math.max(0, state.pendingUploads - 1);
      renderLifeDraft();
    }
  }
  renderMedia();
}

function bindEvents() {
  el.actionConfirmAccept?.addEventListener("click", () => settleActionConfirmation(state.confirmationToken, true));
  el.actionConfirmCancel?.addEventListener("click", () => settleActionConfirmation(state.confirmationToken, false));
  window.addEventListener("pagehide", cancelActiveActionConfirmation);
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
          state.composerRevision += 1;
          const len = contentInput.value.length;
          charCounter.textContent = `${len}/1200`;
          if (len > 1100) charCounter.classList.add("warning");
          else charCounter.classList.remove("warning");
      });
  }

  el.refreshButton.addEventListener("click", () => {
    state.cursor = "";
    state.posts = [];
    loadStatus().then(() => refreshLifeDraft()).catch(() => {});
    loadFeed();
  });
  el.moreButton.addEventListener("click", () => loadFeed({ append: true }));
  el.publishForm.addEventListener("submit", publish);
  el.generateLifeDraft?.addEventListener("click", startLifeDraft);
  el.loadLifeDraft?.addEventListener("click", loadLifeDraftIntoComposer);
  el.discardLifeDraft?.addEventListener("click", discardLifeDraftAssociation);
  window.addEventListener("pagehide", clearLifePoll);
  document.addEventListener?.("visibilitychange", () => {
    if (!document.hidden && state.lifeRecovery) scheduleLifePoll("", 0);
    else if (!document.hidden && state.lifeJob?.state === "running") scheduleLifePoll(state.lifeJob.id, 0);
    else if (!document.hidden && state.lifeAssociation?.uncertain) scheduleLifePoll(state.lifeAssociation.jobId, 0);
    else if (document.hidden) clearLifePoll();
  });
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
  restoreLifeRecovery();
  
  try {
    await loadStatus();
    await refreshLifeDraft("", { poll: Boolean(state.lifeRecovery), recovery: Boolean(state.lifeRecovery) });
    await loadFeed();
  } catch (error) {
    setNotice(error.message || "初始化失败", "error");
  }
}

init();

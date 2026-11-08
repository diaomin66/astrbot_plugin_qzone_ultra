from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_page_header_removes_brand_block() -> None:
    html = (ROOT / "pages" / "qzone" / "index.html").read_text(encoding="utf-8")

    assert 'class="brand"' not in html
    assert 'id="statusText"' not in html
    assert 'id="accountName"' in html
    assert 'id="accountMeta"' in html
    assert '<script src="/api/plugin/page/bridge-sdk.js"></script>' in html


def test_life_confirmation_uses_accessible_in_page_dialog() -> None:
    html = (ROOT / "pages" / "qzone" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "pages" / "qzone" / "app.js").read_text(encoding="utf-8-sig")
    css = (ROOT / "pages" / "qzone" / "style.css").read_text(encoding="utf-8")

    assert '<dialog id="actionConfirmDialog"' in html
    assert 'aria-labelledby="actionConfirmTitle"' in html
    assert 'aria-describedby="actionConfirmMessage"' in html
    assert 'id="actionConfirmCancel"' in html and 'id="actionConfirmAccept"' in html
    assert 'window.confirm(' not in app
    assert "beginActionConfirmation" in app and "settleActionConfirmation" in app
    assert 'apiGet("page/life-draft", { id: jobId })' in app
    assert "actionToken" in app and "composerRevision" in app
    assert 'window.addEventListener("pagehide", cancelActiveActionConfirmation)' in app
    assert "state.confirmationToken === actionToken" in app
    assert 'dialog.addEventListener("keydown", token.onKeydown)' in app
    assert 'el.actionConfirmDialog?.removeEventListener?.("keydown", token.onKeydown)' in app
    assert ".action-confirm-dialog::backdrop" in css
    assert "max-height: calc(100dvh - 32px)" in css
    assert "@media (max-width: 560px)" in css


def test_life_diagnostics_are_persistent_safe_status_text() -> None:
    html = (ROOT / "pages" / "qzone" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "pages" / "qzone" / "app.js").read_text(encoding="utf-8-sig")
    assert 'id="lifeDraftDiagnostics"' in html and 'aria-live="polite"' in html
    assert "renderLifeDiagnostics(job)" in app and "el.lifeDraftDiagnostics.textContent" in app
    assert "publish_diagnostic" in app and "preview_diagnostic" in app
    assert "不说明发布原因" in app
    assert "el.lifeDraftDiagnostics.innerHTML" not in app


def test_page_theme_uses_astrbot_context_not_system_fallback() -> None:
    app = (ROOT / "pages" / "qzone" / "app.js").read_text(encoding="utf-8-sig")
    css = (ROOT / "pages" / "qzone" / "style.css").read_text(encoding="utf-8")

    assert "prefers-color-scheme" not in css
    assert "prefers-color-scheme" not in app
    assert "themeFromDocument" not in app
    assert "themeFromParentStorage" not in app
    assert '"uiTheme"' in app
    assert "v-theme--dark" in app
    assert "::root" not in css
    assert ':root[data-theme="dark"]' in css
    assert '|| "light"' in app


def test_page_upload_preview_accepts_local_blob_urls() -> None:
    app = (ROOT / "pages" / "qzone" / "app.js").read_text(encoding="utf-8-sig")
    html = (ROOT / "pages" / "qzone" / "index.html").read_text(encoding="utf-8")

    assert 'accept="image/*,video/*"' in html
    assert 'source.startsWith("blob:")' in app
    assert "isVideoMediaItem" in app
    assert "document.createElement(\"video\")" in app
    assert "delete payload.preview_url" in app


def test_page_detail_logic_avoids_viewport_jump_and_reuses_cache() -> None:
    app = (ROOT / "pages" / "qzone" / "app.js").read_text(encoding="utf-8-sig")
    css = (ROOT / "pages" / "qzone" / "style.css").read_text(encoding="utf-8")

    assert "scrollIntoView" not in app
    assert "detailCache" in app
    assert "detailInFlight" in app
    assert "syncSelectedFeedCard" in app
    assert "body.detail-drawer-open" in css
    assert "scrollbar-gutter: stable" in css


def test_page_feed_renders_normalized_media_and_source_downloads() -> None:
    app = (ROOT / "pages" / "qzone" / "app.js").read_text(encoding="utf-8-sig")

    assert "post.media?.length ? post.media : post.images" in app
    assert 'kind === "video"' in app
    assert 'kind === "audio"' in app
    assert 'kind === "file"' in app
    assert 'video.preload = "metadata"' in app
    assert 'audio.preload = "metadata"' in app
    assert "item.source || item.data_url || item.url || item.preview_url" in app
    assert 'bridge.download("page/media"' in app
    assert 'video.referrerPolicy = "no-referrer"' in app
    assert 'download.textContent = "下载原文件"' in app
    assert "renderRichText(body, contentText, post.content_segments)" in app
    assert '"qzone-emoji nickname-emoji"' in app


def test_ai_draft_buttons_are_accessible_and_inline_reply_actions_remain_tappable() -> None:
    app = (ROOT / "pages" / "qzone" / "app.js").read_text(encoding="utf-8-sig")
    css = (ROOT / "pages" / "qzone" / "style.css").read_text(encoding="utf-8")

    assert 'setAttribute("aria-label", "AI 生成评论")' in app
    assert 'setAttribute("aria-label", "AI 生成回复")' in app
    assert 'apiPost("page/ai-comment"' in app
    assert 'apiPost("page/ai-reply"' in app
    assert ".reply-form-actions {\n  display: flex;\n  flex-wrap: wrap;" in css
    assert ".reply-form-actions button {\n  flex: 0 0 auto;\n  white-space: nowrap;" in css
    assert ".reply-form-actions .ai-draft-status {\n  flex: 1 0 100%;" in css
    assert "grid-column: 1 / -1;" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the frontend smoke test")
def test_page_frontend_reply_uses_inline_form_and_cached_detail(tmp_path: Path) -> None:
    harness = tmp_path / "qzone_page_harness.mjs"
    app_source = (ROOT / "pages" / "qzone" / "app.js").read_text(encoding="utf-8-sig")
    instrumented_app = tmp_path / "qzone_app_instrumented.mjs"
    instrumented_app.write_text(app_source.replace("\ninit();", "\nglobalThis.__qzoneTest = { state, el, publish, discardLifeDraftAssociation, renderLifeDraft, renderLifeDraftPreviews };\ninit();"), encoding="utf-8")
    harness.write_text(
        r'''
import { pathToFileURL } from "node:url";

class Element {
  constructor(id = "") {
    this.id = id;
    this.hidden = false;
    this.textContent = "";
    this.innerHTML = "";
    this.value = "";
    this.disabled = false;
    this.files = [];
    this.children = [];
    this.dataset = {};
    this.className = "";
    this.type = "";
    this.rows = 0;
    this.placeholder = "";
    this.maxlength = 0;
    this.inputmode = "";
    this.autocomplete = "";
    this.loading = "";
    this.alt = "";
    this.src = "";
    this.style = {};
    this.handlers = {};
    this.tagName = "";
    this.parentNode = null;
    this.classList = {
      add: (name) => {
        if (!this.className.split(/\s+/).includes(name)) {
          this.className = `${this.className} ${name}`.trim();
        }
      },
      remove: (name) => {
        this.className = this.className.split(/\s+/).filter((item) => item && item !== name).join(" ");
      },
      toggle: (name, enabled) => {
        this[`class:${name}`] = Boolean(enabled);
        if (enabled) this.classList.add(name);
        else this.classList.remove(name);
      },
      contains: (name) => this.className.split(/\s+/).includes(name),
    };
  }

  append(...nodes) {
    for (const node of nodes) {
      if (node && typeof node === "object") node.parentNode = this;
      this.children.push(node);
    }
  }

  appendChild(node) {
    this.append(node);
    return node;
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((item) => item !== this);
  }

  addEventListener(name, handler) {
    this.handlers[name] = handler;
  }

  removeEventListener(name, handler) {
    if (this.handlers[name] === handler) delete this.handlers[name];
  }

  showModal() {
    this.showModalCalls = (this.showModalCalls || 0) + 1;
    this.open = true;
  }

  close() {
    this.open = false;
    this.handlers.close?.();
  }

  setAttribute(name, value) {
    this[name] = String(value);
  }

  removeAttribute(name) {
    delete this[name];
  }

  focus() {
    this.focused = true;
    if (globalThis.document) globalThis.document.activeElement = this;
  }

  querySelector(selector) {
    return find(this, selector);
  }

  querySelectorAll(selector) {
    return findAll(this, selector);
  }
}

function matches(element, selector) {
  if (!(element instanceof Element)) return false;
  if (selector.startsWith(".")) {
    return element.className.split(/\s+/).includes(selector.slice(1));
  }
  return element.tagName.toLowerCase() === selector.toLowerCase();
}

function find(root, selector) {
  if (selector.includes(" ")) {
    const [head, ...tail] = selector.split(/\s+/);
    const parent = find(root, head);
    return parent ? find(parent, tail.join(" ")) : null;
  }
  for (const child of root.children) {
    if (matches(child, selector)) return child;
    const nested = child instanceof Element ? find(child, selector) : null;
    if (nested) return nested;
  }
  return null;
}

function findAll(root, selector) {
  if (selector.includes(" ")) {
    const [head, ...tail] = selector.split(/\s+/);
    return findAll(root, head).flatMap((parent) => findAll(parent, tail.join(" ")));
  }
  const result = [];
  for (const child of root.children) {
    if (matches(child, selector)) result.push(child);
    if (child instanceof Element) result.push(...findAll(child, selector));
  }
  return result;
}

function byText(root, value) {
  for (const child of root.children) {
    if (child instanceof Element && child.textContent === value) return child;
    const nested = child instanceof Element ? byText(child, value) : null;
    if (nested) return nested;
  }
  return null;
}

const elements = new Map();
for (const id of [
  "accountName",
  "accountMeta",
  "targetForm",
  "targetUin",
  "publishForm",
  "publishContent",
  "mediaInput",
  "mediaStrip",
  "publishButton",
  "refreshButton",
  "feedTitle",
  "feedMeta",
  "notice",
  "feed",
  "moreButton",
  "detailPane",
  "detailEmpty",
  "detailContent",
  "generateLifeDraft",
  "lifeDraftStatus",
  "lifeDraftResult",
  "lifeDraftDiagnostics",
  "lifeDraftPreview",
  "lifeDraftLongContent",
  "loadLifeDraft",
  "discardLifeDraft",
  "actionConfirmDialog",
  "actionConfirmTitle",
  "actionConfirmMessage",
  "actionConfirmFeedback",
  "actionConfirmCancel",
  "actionConfirmAccept",
  "charCounter",
]) {
  elements.set(id, new Element(id));
}
elements.get("actionConfirmCancel").textContent = "取消";
elements.get("actionConfirmAccept").textContent = "确认发布";

const account = new Element("account");
account.className = "account";
const accountAvatar = new Element("account-avatar");
accountAvatar.className = "avatar";
account.append(accountAvatar);
const documentRoot = new Element("document-root");
documentRoot.dataset = {};
documentRoot.style = {};

const tabs = ["friends", "self", "profile"].map((scope) => {
  const tab = new Element(`tab-${scope}`);
  tab.dataset.scope = scope;
  return tab;
});

globalThis.document = {
  activeElement: null,
  hidden: false,
  documentElement: documentRoot,
  body: new Element("body"),
  getElementById(id) {
    return elements.get(id) || null;
  },
  querySelector(selector) {
    return selector === ".account .avatar" ? accountAvatar : null;
  },
  querySelectorAll(selector) {
    return selector === ".tab" ? tabs : [];
  },
  createElement(tag) {
    const element = new Element();
    element.tagName = tag.toUpperCase();
    return element;
  },
};

const windowHandlers = new Map();
let promptCalls = 0;
let confirmCalls = 0;
let replyPayload = null;
let commentPayload = null;
let rejectComment;
let deletePayload = null;
let publishPayload = null;
let lifePublishPosts = 0;
let deferLifeGet = false;
let releaseLifeGet;
let freshLifeGetStarted;
let rejectLifePreview = false;
const lifeJob = { id: "life-confirm-1", draft_id: "draft-confirm-1", account_uin: 10001, state: "succeeded", publish_state: "ready", content: "review this life draft", media: [], has_media: false };
let detailResolve;
let detailApiCalls = 0;
const detailPromise = new Promise((resolve) => {
  detailResolve = resolve;
});

const feedPost = {
  id: "post_1",
  author: { uin: 10001, nickname: "Tester" },
  content: "hello from qzone",
  created_at: 1710000000,
  stats: { likes: 3, comments: 1 },
  liked: false,
  images: ["https://qzone.example.test/photo.jpg"],
  can_delete: true,
  comments: [
    {
      id: "comment-1",
      author: { uin: 20002, nickname: "Friend" },
      content: "nice",
      created_at: 1710000100,
      can_reply: true,
    },
  ],
};

globalThis.window = {
  setTimeout,
  clearTimeout,
  requestAnimationFrame(handler) {
    return setTimeout(handler, 0);
  },
  addEventListener(name, handler) {
    windowHandlers.set(name, [...(windowHandlers.get(name) || []), handler]);
  },
  prompt() {
    promptCalls += 1;
    return "should not be used";
  },
  confirm() {
    confirmCalls += 1;
    return true;
  },
    AstrBotPluginPage: {
    ready() {
      return Promise.resolve({ pluginName: "astrbot_plugin_qzone_ultra", pageName: "qzone", uiTheme: "PurpleThemeDark" });
    },
    apiGet(endpoint, params = {}) {
      if (endpoint === "page/status") {
        return Promise.resolve({
          daemon: { state: "ready", version: "0.4.3" },
          login: { bound: true, uin: 10001, nickname: "" },
          limits: { images: 9 },
        });
      }
      if (endpoint === "page/feed") {
        return Promise.resolve({
          items: [feedPost],
          cursor: "",
          has_more: false,
        });
      }
      if (endpoint === "page/detail") {
        detailApiCalls += 1;
        return detailPromise;
      }
      if (endpoint === "page/life-draft") {
        if (!params.id) return Promise.resolve({ ...lifeJob });
        freshLifeGetStarted?.();
        if (deferLifeGet) return new Promise(resolve => { releaseLifeGet = resolve; });
        return Promise.resolve({ ...lifeJob });
      }
      if (endpoint === "page/life-draft-preview") return rejectLifePreview
        ? Promise.reject(new Error("图片预览暂不可用。REMOTE_POLICY_FAILED 0123456789ab"))
        : Promise.resolve({ data_url: "data:image/png;base64,AA==" });
      throw new Error(`unexpected GET ${endpoint}`);
    },
    apiPost(endpoint, body) {
      if (endpoint === "page/reply") {
        replyPayload = body;
        return Promise.resolve({
          reply: {
            id: "reply-new",
            content: body.content,
            author: { uin: 10001, nickname: "" },
          },
        });
      }
      if (endpoint === "page/comment") {
        commentPayload = body;
        return new Promise((resolve, reject) => { rejectComment = reject; });
      }
      if (endpoint === "page/delete") {
        deletePayload = body;
        return Promise.resolve({ message: "ok" });
      }
      if (endpoint === "page/publish") {
        publishPayload = body;
        if (body.life_job_id) lifePublishPosts += 1;
        return Promise.resolve({
          post: {
            ...feedPost,
            id: "post_new",
            content: body.content,
            images: [],
            comments: [],
          },
        });
      }
      return Promise.resolve({});
    },
    upload(endpoint, file) {
      if (endpoint !== "page/upload-media") {
        throw new Error(`unexpected upload ${endpoint}`);
      }
      return Promise.resolve({
        media: {
          upload_id: "upload-token-1",
          name: file.name,
          mime_type: "image/png",
          size: 1234,
        },
      });
    },
  },
};

await import(pathToFileURL(process.argv[2]).href);
await new Promise((resolve) => setTimeout(resolve, 30));

if (documentRoot.dataset.theme !== "dark" || documentRoot.style.colorScheme !== "dark" || !documentRoot.classList.contains("v-theme--dark")) {
  throw new Error(`theme did not follow bridge context: ${documentRoot.dataset.theme}/${documentRoot.style.colorScheme}`);
}
windowHandlers.get("message")?.forEach(handler => handler({
  data: {
    channel: "astrbot-plugin-page",
    kind: "context",
    context: { uiTheme: "PurpleTheme" },
  },
}));
await new Promise((resolve) => setTimeout(resolve, 30));
if (documentRoot.dataset.theme !== "light" || documentRoot.style.colorScheme !== "light" || !documentRoot.classList.contains("v-theme--light")) {
  throw new Error(`theme did not follow AstrBot theme update: ${documentRoot.dataset.theme}/${documentRoot.style.colorScheme}`);
}

if (elements.get("accountName").textContent !== "Tester") {
  throw new Error(`account nickname fallback failed: ${elements.get("accountName").textContent}`);
}
if (elements.get("accountMeta").textContent !== "QQ 10001") {
  throw new Error(`account QQ meta failed: ${elements.get("accountMeta").textContent}`);
}
const accountImg = accountAvatar.children.find((child) => child.tagName === "IMG");
if (!accountImg) {
  throw new Error("account avatar image was not rendered");
}
if (!String(accountImg.src).startsWith("https://")) {
  throw new Error(`account avatar must use https: ${accountImg.src}`);
}
if (accountImg.alt === "Avatar") {
  throw new Error("account avatar leaked the placeholder alt text");
}
accountImg.onerror();
accountImg.onerror();
if (accountAvatar.textContent !== "T") {
  throw new Error(`account avatar fallback was wrong: ${accountAvatar.textContent}`);
}
if (elements.get("feedMeta").textContent !== "1 条") {
  throw new Error(`feed was not rendered: ${elements.get("feedMeta").textContent}`);
}

const detailButton = byText(elements.get("feed"), "查看详情");
if (!detailButton) throw new Error("detail button was not rendered");
detailButton.handlers.click();
detailButton.handlers.click();

if (elements.get("detailContent").hidden) {
  throw new Error("cached detail was not rendered immediately");
}
if (detailApiCalls !== 1) {
  throw new Error(`duplicate detail requests were not coalesced: ${detailApiCalls}`);
}
const detailMedia = elements.get("detailContent").querySelector(".detail-media");
if (!detailMedia || !detailMedia.querySelector("img")) {
  throw new Error("detail image media was not rendered");
}
if (!byText(elements.get("detailContent"), "回复")) {
  throw new Error("cached comments were not rendered before network detail finished");
}

const replyButton = byText(elements.get("detailContent"), "回复");
replyButton.handlers.click();
if (promptCalls !== 0) {
  throw new Error("reply still used window.prompt");
}

const replyForm = elements.get("detailContent").querySelector(".reply-form");
const textarea = replyForm?.querySelector("textarea");
if (!replyForm || !textarea) {
  throw new Error("inline reply form was not rendered");
}
textarea.value = "谢谢";
await replyForm.handlers.submit({ preventDefault() {} });
await new Promise((resolve) => setTimeout(resolve, 0));

if (!replyPayload || replyPayload.commentid !== "comment-1" || replyPayload.content !== "谢谢") {
  throw new Error(`reply payload was wrong: ${JSON.stringify(replyPayload)}`);
}
if (!byText(elements.get("detailContent"), "Tester")) {
  throw new Error("self reply did not reuse the current nickname");
}

let commentForm = elements.get("detailContent").querySelector(".comment-form");
let commentInput = commentForm?.querySelector("textarea");
let commentSubmit = byText(commentForm, "发送");
if (!commentForm || !commentInput || !commentSubmit) throw new Error("comment form was not rendered");
commentInput.value = "draft kept after failure";
commentInput.handlers.input?.();
const pendingComment = commentForm.handlers.submit({ preventDefault() {} });
commentForm = elements.get("detailContent").querySelector(".comment-form");
commentInput = commentForm?.querySelector("textarea");
commentSubmit = byText(commentForm, "发送");
if (!commentSubmit?.disabled || commentInput?.value !== "draft kept after failure") {
  throw new Error("comment submit busy state/draft did not survive optimistic DOM rebuild");
}
if (!commentPayload || commentPayload.content !== "draft kept after failure") {
  throw new Error(`comment request payload was wrong: ${JSON.stringify(commentPayload)}`);
}
rejectComment(new Error("fixture comment send failure"));
await pendingComment;
commentForm = elements.get("detailContent").querySelector(".comment-form");
commentInput = commentForm?.querySelector("textarea");
commentSubmit = byText(commentForm, "发送");
if (commentSubmit?.disabled || commentInput?.value !== "draft kept after failure") {
  throw new Error("comment failure lost draft or kept form busy after DOM rebuild");
}

detailResolve({
  post: {
    ...feedPost,
    stats: { likes: 3, comments: 1 },
    images: [
      "http://qzone.example.test/photo.jpg?w=120",
      "https://qzone.example.test/photo.jpg?h=240",
    ],
    comments: [...feedPost.comments],
  },
});
await new Promise((resolve) => setTimeout(resolve, 30));

if (!byText(elements.get("detailContent"), "回复 Friend：谢谢")) {
  throw new Error("stale detail response overwrote the local reply");
}
const dedupedDetailImages = elements.get("detailContent").querySelectorAll(".detail-media img");
if (dedupedDetailImages.length !== 1) {
  throw new Error(`detail image variants were rendered more than once: ${dedupedDetailImages.length}`);
}

const mediaInput = elements.get("mediaInput");
mediaInput.files = [{ name: "photo.png" }];
await mediaInput.handlers.change();
await new Promise((resolve) => setTimeout(resolve, 30));

if (elements.get("mediaStrip").children.length !== 1) {
  throw new Error("unwrapped upload payload was not accepted");
}
elements.get("publishContent").value = "with image";
await elements.get("publishForm").handlers.submit({ preventDefault() {} });
await new Promise((resolve) => setTimeout(resolve, 30));
if (!publishPayload || publishPayload.content !== "with image") {
  throw new Error(`publish payload was missing: ${JSON.stringify(publishPayload)}`);
}
if (publishPayload.media?.[0]?.upload_id !== "upload-token-1") {
  throw new Error(`upload token was not preserved for publish: ${JSON.stringify(publishPayload)}`);
}
if ("preview_url" in publishPayload.media[0] || "previewUrl" in publishPayload.media[0]) {
  throw new Error(`preview url leaked into publish payload: ${JSON.stringify(publishPayload)}`);
}
if (confirmCalls !== 0) {
  throw new Error("ordinary publish unexpectedly requested a confirmation");
}

const deleteButton = byText(elements.get("detailContent"), "删除");
if (!deleteButton) throw new Error("delete button was not rendered");
deleteButton.handlers.click();
if (confirmCalls !== 0) {
  throw new Error("delete still used native confirm");
}
if (deletePayload) {
  throw new Error("delete should require a second in-page confirmation click");
}
const confirmDeleteButton = byText(elements.get("detailContent"), "再次点击确认删除");
if (!confirmDeleteButton) {
  throw new Error("second-click delete confirmation was not rendered");
}
confirmDeleteButton.handlers.click();
await new Promise((resolve) => setTimeout(resolve, 0));
if (!deletePayload || deletePayload.id !== "post_1") {
  throw new Error(`delete payload was wrong: ${JSON.stringify(deletePayload)}`);
}

const qz = globalThis.__qzoneTest;
qz.state.status = { login: { uin: 10001 } };
qz.state.lifeJob = { ...lifeJob };
qz.state.lifeAssociation = { jobId: lifeJob.id, accountUin: 10001, uncertain: false };
elements.get("publishContent").value = lifeJob.content;
const dialog = elements.get("actionConfirmDialog");
const submitLife = () => qz.publish({ preventDefault() {} });
const cancelAction = elements.get("actionConfirmCancel");
const acceptAction = elements.get("actionConfirmAccept");

const cancelledLife = submitLife();
await new Promise(resolve => setTimeout(resolve, 0));
if (!dialog.open) throw new Error("Life publish did not open the in-page confirmation");
cancelAction.handlers.click();
await cancelledLife;
if (lifePublishPosts !== 0) throw new Error("cancelled Life publish dispatched a request");

// Model the close event from the previous modal arriving after a new one opens.
const nativeClose = dialog.close.bind(dialog);
dialog.close = () => { nativeClose(); setTimeout(() => dialog.handlers.close?.(), 15); };
const staleCloseFirst = submitLife();
await new Promise(resolve => setTimeout(resolve, 0));
cancelAction.handlers.click();
await staleCloseFirst;
const staleCloseSecond = submitLife();
await new Promise(resolve => setTimeout(resolve, 0));
const modalCount = dialog.showModalCalls;
await new Promise(resolve => setTimeout(resolve, 25));
if (!dialog.open || dialog.showModalCalls !== modalCount) throw new Error("queued stale close cancelled a later confirmation");
cancelAction.handlers.click();
await staleCloseSecond;
dialog.close = nativeClose;

deferLifeGet = true;
let announceJobSwitchGet;
const jobSwitchGetStarted = new Promise(resolve => { announceJobSwitchGet = resolve; });
freshLifeGetStarted = announceJobSwitchGet;
const jobSwitchPublish = submitLife();
await new Promise(resolve => setTimeout(resolve, 0));
acceptAction.handlers.click();
await jobSwitchGetStarted;
const replacementJob = { id: "life-confirm-replacement", draft_id: "draft-replacement", account_uin: 10001, state: "succeeded", publish_state: "ready", content: "replacement", media: [], has_media: false };
qz.state.lifeJob = replacementJob;
releaseLifeGet({ ...lifeJob });
await jobSwitchPublish;
if (lifePublishPosts !== 0 || qz.state.lifeJob !== replacementJob) {
  throw new Error("fresh GET overwrote a replacement selected job or published the old job");
}
qz.state.lifeJob = { ...lifeJob };

// The captured action token remains live until its fresh GET completes.
let announceFreshGet;
const freshGetStarted = new Promise(resolve => { announceFreshGet = resolve; });
freshLifeGetStarted = announceFreshGet;
const pagehideLife = submitLife();
await new Promise(resolve => setTimeout(resolve, 0));
const openCount = dialog.showModalCalls;
const duplicateLife = submitLife();
await duplicateLife;
if (dialog.showModalCalls !== openCount) throw new Error("duplicate Life submit opened another confirmation");
acceptAction.handlers.click();
await freshGetStarted;
windowHandlers.get("pagehide")?.forEach(handler => handler());
releaseLifeGet({ ...lifeJob });
await pagehideLife;
if (lifePublishPosts !== 0) throw new Error("pagehide during fresh GET allowed a Life publish");

// A completed approval posts one exact immutable request.
deferLifeGet = false;
freshLifeGetStarted = null;
const acceptedLife = submitLife();
await new Promise(resolve => setTimeout(resolve, 0));
acceptAction.handlers.click();
await acceptedLife;
if (lifePublishPosts !== 1 || publishPayload?.life_job_id !== lifeJob.id || publishPayload?.content !== lifeJob.content) {
  throw new Error(`accepted Life publish was not one exact captured request: ${JSON.stringify(publishPayload)}`);
}

qz.state.status.login.uin = 20002;
const discarded = qz.discardLifeDraftAssociation();
await new Promise(resolve => setTimeout(resolve, 0));
acceptAction.handlers.click();
await discarded;
if (qz.state.lifeAssociation !== null || elements.get("publishContent").value !== "") {
  throw new Error("confirmed discard after account change did not clear the stale local composition");
}

qz.state.lifeJob = {
  ...lifeJob, publish_state: "unknown",
  publish_diagnostic: { id: "abcdef012345", code: "DAEMON_UNAVAILABLE", phase: "controller_call", message: "ignored arbitrary server text TOKEN", at: 1 },
  preview_diagnostic: { id: "fedcba987654", code: "REMOTE_POLICY_FAILED", phase: "preview_read", message: "untrusted", at: 1 },
};
qz.renderLifeDraft();
const diagnostics = elements.get("lifeDraftDiagnostics").textContent;
if (!diagnostics.includes("abcdef012345") || !diagnostics.includes("fedcba987654") || !diagnostics.includes("不说明发布原因")) {
  throw new Error(`persistent diagnostic records were not rendered: ${diagnostics}`);
}
if (diagnostics.includes("TOKEN") || diagnostics.includes("ignored arbitrary server text")) throw new Error("diagnostic rendering trusted an arbitrary message");
rejectLifePreview = true;
qz.state.status.login.uin = 10001;
qz.state.lifeJob = { ...lifeJob, media: [{ media_id: "opaque-preview-id", name: "preview.png" }], has_media: true };
await qz.renderLifeDraftPreviews(qz.state.lifeJob);
if (qz.state.lifeJob.preview_diagnostic_reference?.id !== "0123456789ab" || !elements.get("lifeDraftDiagnostics").textContent.includes("0123456789ab")) {
  throw new Error(`message-only preview error was not safely retained in persistent status: ${JSON.stringify({ref:qz.state.lifeJob.preview_diagnostic_reference,status:qz.state.status,owner:qz.state.lifeJob.account_uin,text:elements.get("lifeDraftDiagnostics").textContent})}`);
}
''',
        encoding="utf-8",
    )

    subprocess.run(
        ["node", str(harness), str(instrumented_app)],
        check=True,
        cwd=ROOT,
    )

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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the frontend smoke test")
def test_page_frontend_reply_uses_inline_form_and_cached_detail(tmp_path: Path) -> None:
    harness = tmp_path / "qzone_page_harness.mjs"
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

  setAttribute(name, value) {
    this[name] = String(value);
  }

  focus() {
    this.focused = true;
  }

  querySelector(selector) {
    return find(this, selector);
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
]) {
  elements.set(id, new Element(id));
}

const account = new Element("account");
account.className = "account";
const accountAvatar = new Element("account-avatar");
accountAvatar.className = "avatar";
account.append(accountAvatar);

const tabs = ["friends", "self", "profile"].map((scope) => {
  const tab = new Element(`tab-${scope}`);
  tab.dataset.scope = scope;
  return tab;
});

globalThis.document = {
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

let promptCalls = 0;
let replyPayload = null;
let detailResolve;
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
  images: [],
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
  prompt() {
    promptCalls += 1;
    return "should not be used";
  },
  confirm() {
    return true;
  },
  AstrBotPluginPage: {
    ready() {
      return Promise.resolve({ pluginName: "astrbot_plugin_qzone_ultra", pageName: "qzone" });
    },
    apiGet(endpoint) {
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
        return detailPromise;
      }
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
      return Promise.resolve({});
    },
    upload(endpoint, file) {
      if (endpoint !== "page/upload-media") {
        throw new Error(`unexpected upload ${endpoint}`);
      }
      return Promise.resolve({
        media: {
          name: file.name,
          data_url: "data:image/png;base64,AA==",
        },
      });
    },
  },
};

await import(pathToFileURL(process.argv[2]).href);
await new Promise((resolve) => setTimeout(resolve, 30));

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

if (elements.get("detailContent").hidden) {
  throw new Error("cached detail was not rendered immediately");
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

detailResolve({
  post: {
    ...feedPost,
    stats: { likes: 3, comments: 1 },
    comments: [...feedPost.comments],
  },
});
await new Promise((resolve) => setTimeout(resolve, 30));

if (!byText(elements.get("detailContent"), "回复 Friend：谢谢")) {
  throw new Error("stale detail response overwrote the local reply");
}

const mediaInput = elements.get("mediaInput");
mediaInput.files = [{ name: "photo.png" }];
await mediaInput.handlers.change();
await new Promise((resolve) => setTimeout(resolve, 30));

if (elements.get("mediaStrip").children.length !== 1) {
  throw new Error("unwrapped upload payload was not accepted");
}
''',
        encoding="utf-8",
    )

    subprocess.run(
        ["node", str(harness), str(ROOT / "pages" / "qzone" / "app.js")],
        check=True,
        cwd=ROOT,
    )

/* ==========================================================================
 * 爱丽丝的图片助手 · WebUI (pages/alice/app.js)
 *
 * 架构（自上而下分区，改动请对号入座）：
 *   01 常量      视图清单、图标白名单、兜底主题、防抖时长
 *   02 状态      单一可变 state 对象；所有渲染都是 state -> DOM 的纯函数
 *   03 工具      el()/icon() DOM 构造器（只用 textContent，杜绝 innerHTML 拼接）
 *   04 桥接      bridge.apiGet/apiPost/upload/download 的薄封装 + unwrap/assertOk
 *   05 偏好      主题 / 紧凑 / 当前 tab 的应用与 600ms 防抖持久化
 *   06 数据      meta / health / config / commands 的按需加载与错误态
 *   07 组件      卡片、键值表、pill、骨架屏、空态、内联错误
 *   08 视图      概览 / 找图 / 溯源 / 配置 / 指令表 / 关于
 *   09 外壳      tabbar、状态栏、hash 路由、灯箱、toast
 *   10 启动      boot()：ready -> state -> meta/health -> 首帧渲染
 *
 * 约束：AstrBot 自动注入 bridge（window.AstrBotPluginPage），本文件不做 import；
 * 静态资源全部相对路径；SPA 走 hash 路由；后端不可用时降级为内联错误态，不白屏。
 * ========================================================================== */

/* ---- 01 常量 ------------------------------------------------------------- */

const SVG_NS = "http://www.w3.org/2000/svg";

const VIEWS = [
  { key: "overview", icon: "gauge", label: "概览", tip: "健康检查 · 能力开关 · 运行时" },
  { key: "search", icon: "search", label: "找图", tip: "描述检索 · 多源回退 · VLM 复核" },
  { key: "reverse", icon: "compass", label: "溯源", tip: "上传或贴图 · 多策略反查来源" },
  { key: "config", icon: "sliders", label: "配置", tip: "全部配置项 · 过滤 · 批量保存" },
  { key: "commands", icon: "terminal", label: "指令表", tip: "全部聊天指令与别名" },
  { key: "about", icon: "info", label: "关于", tip: "插件信息 · 主题 · 安全须知" },
];
const VIEW_KEYS = VIEWS.map((v) => v.key);
const VIEW_LABEL = new Map(VIEWS.map((v) => [v.key, v.label]));
const DEFAULT_VIEW = "overview";
const DEFAULT_THEME = "wonderland";

/** index.html 里实际存在的 symbol id；后端给的 icon 不在其中就回退。 */
const KNOWN_ICONS = new Set([
  "gauge", "search", "compass", "sliders", "terminal", "info", "refresh", "compact",
  "expand", "caret", "chev-right", "check", "close", "copy", "download", "upload",
  "eye", "eye-off", "trash", "link", "external", "save", "undo", "palette", "alert",
  "shield", "filter", "plus", "minus", "image", "clock", "spark", "layers", "key",
  "globe", "tag", "folder", "zoom", "play", "user", "book", "bell", "gift", "wand",
  "toggles", "brain", "plug", "history", "layout",
]);

/** 后端 meta 拿不到时的兜底主题清单，保证主题选择器永远可用。 */
const FALLBACK_THEMES = [
  { key: "wonderland", label: "奇境", accent: "#d4af37" },
  { key: "aurora", label: "极光", accent: "#3ddc97" },
  { key: "midnight", label: "午夜", accent: "#7c8cff" },
  { key: "sakura", label: "樱雪", accent: "#e8749a" },
  { key: "paper", label: "纸稿", accent: "#4a6fa5" },
  { key: "sunset", label: "落日", accent: "#ff8a4c" },
];
const THEME_KEYS = new Set(FALLBACK_THEMES.map((t) => t.key));

const LEVEL_ICON = { ok: "check", warn: "alert", error: "close", info: "info" };
const LEVEL_TEXT = { ok: "正常", warn: "注意", error: "异常", info: "信息" };
const PREF_SAVE_DELAY = 600;

/* ---- 02 状态 ------------------------------------------------------------- */

const state = {
  booted: false,
  locale: "zh-CN",
  view: DEFAULT_VIEW,
  theme: DEFAULT_THEME,
  compact: false,
  themes: FALLBACK_THEMES.slice(),
  meta: null,
  metaError: null,
  health: null,
  healthError: null,
  healthBusy: false,

  /* 配置页 */
  cfg: {
    loaded: false,
    busy: false,
    error: null,
    groups: [],
    nav: [],
    fields: [],
    current: null,
    expanded: new Set(),
    query: "",
    drafts: new Map(),
    rejected: new Map(),
    revealed: new Set(),
    saving: false,
  },

  /* 指令表 */
  cmd: { loaded: false, busy: false, error: null, groups: [], total: 0, query: "" },

  /* 找图 */
  search: {
    query: "",
    source: "auto",
    count: 1,
    review: true,
    busy: false,
    error: null,
    data: null,
  },

  /* 溯源 */
  reverse: {
    url: "",
    upload: null,
    strategies: null,
    busy: false,
    uploading: false,
    error: null,
    data: null,
  },
};

/* ---- 03 工具 ------------------------------------------------------------- */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** 把 children（节点 / 字符串 / 数组 / null）安全地挂到 node 上。字符串一律走文本节点。 */
function append(node, children) {
  if (children === null || children === undefined || children === false) return node;
  if (Array.isArray(children)) {
    for (const child of children) append(node, child);
    return node;
  }
  node.appendChild(children instanceof Node ? children : document.createTextNode(String(children)));
  return node;
}

/**
 * 极简元素构造器。attrs 支持 class / text / dataset / on<Event> / 其余按属性写入。
 * 故意不提供 innerHTML 通道：后端字符串只能经 text/append 落成文本节点。
 */
function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const key of Object.keys(attrs)) {
      const value = attrs[key];
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (value === true) node.setAttribute(key, "");
      else node.setAttribute(key, String(value));
    }
  }
  return append(node, children);
}

function icon(name, extra) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "icon" + (extra ? " " + extra : ""));
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  const id = KNOWN_ICONS.has(name) ? name : "info";
  use.setAttribute("href", "#i-" + id);
  svg.appendChild(use);
  return svg;
}

const iconOr = (name, fallback) => icon(KNOWN_ICONS.has(name) ? name : fallback);

function fmtBytes(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return "—";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
}

function fmtMs(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n)) return "—";
  return n >= 1000 ? (n / 1000).toFixed(1) + " s" : Math.round(n) + " ms";
}

function fmtSeconds(seconds) {
  const n = Number(seconds);
  if (!Number.isFinite(n) || n <= 0) return "—";
  if (n < 60) return Math.round(n) + " 秒";
  if (n < 3600) return Math.round(n / 60) + " 分钟";
  return (n / 3600).toFixed(1) + " 小时";
}

function fmtNum(value) {
  const n = Number(value);
  return Number.isFinite(n) ? String(n) : "—";
}

const plain = (value, dash = "—") => {
  if (value === null || value === undefined) return dash;
  const s = String(value).trim();
  return s === "" ? dash : s;
};

/** 只放行 http(s) 与 data:image，其它统一当作不可用链接。 */
function safeUrl(raw) {
  const s = typeof raw === "string" ? raw.trim() : "";
  if (!s) return null;
  if (/^https?:\/\//i.test(s)) return s;
  if (/^data:image\//i.test(s)) return s;
  return null;
}

function deepEqual(a, b) {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((item, i) => deepEqual(item, b[i]));
  }
  if (a === null || b === null || typeof a !== "object" || typeof b !== "object") return false;
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => deepEqual(a[k], b[k]));
}

function debounce(fn, delay) {
  let timer = null;
  return function debounced(...args) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => { timer = null; fn(...args); }, delay);
  };
}

/** 把 text 按 query 命中处切成 文本 / <mark> 片段塞进 node（不产生 HTML 字符串）。 */
function fillHighlight(node, text, query) {
  const source = String(text === null || text === undefined ? "" : text);
  const needle = String(query || "").trim().toLowerCase();
  if (!needle) { node.textContent = source; return node; }
  const haystack = source.toLowerCase();
  let from = 0;
  let hit = haystack.indexOf(needle, from);
  if (hit < 0) { node.textContent = source; return node; }
  node.textContent = "";
  while (hit >= 0) {
    if (hit > from) node.appendChild(document.createTextNode(source.slice(from, hit)));
    node.appendChild(el("mark", { class: "mark", text: source.slice(hit, hit + needle.length) }));
    from = hit + needle.length;
    hit = haystack.indexOf(needle, from);
  }
  if (from < source.length) node.appendChild(document.createTextNode(source.slice(from)));
  return node;
}

const TOAST_KIND = { ok: "ok", err: "err", warn: "warn", info: "info" };

function toast(message, kind = "info", ttl = 4200) {
  const host = $("#toasts");
  if (!host) return;
  const cls = TOAST_KIND[kind] || "info";
  const node = el("div", { class: "toast " + cls, role: "status" }, [
    icon(LEVEL_ICON[kind === "err" ? "error" : kind] || "info"),
    el("span", { text: plain(message, "操作完成") }),
  ]);
  host.appendChild(node);
  const kill = () => {
    node.classList.add("is-leaving");
    setTimeout(() => node.remove(), 220);
  };
  const timer = setTimeout(kill, ttl);
  node.addEventListener("click", () => { clearTimeout(timer); kill(); });
}

function errText(error) {
  const raw = (error && (error.message || error.msg)) || String(error || "");
  const cleaned = raw.replace(/^Error:\s*/, "").trim();
  return cleaned || "请求失败";
}

async function copyText(text) {
  const value = String(text === null || text === undefined ? "" : text);
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch (_) { /* 落到下面的兜底 */ }
  try {
    const pad = el("textarea", { value, "aria-hidden": "true" });
    pad.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
    document.body.appendChild(pad);
    pad.focus();
    pad.select();
    const done = document.execCommand("copy");
    pad.remove();
    return done;
  } catch (_) {
    return false;
  }
}

/* ---- 04 桥接 ------------------------------------------------------------- */

/** AstrBot 注入的 SDK：不同版本挂载名不同，取第一个具备 apiGet / ready 的对象。 */
function resolveBridge() {
  const candidates = [
    window.AstrBotPluginPage,
    window.astrbotPluginPage,
    window.bridge,
    window.astrbotBridge,
    window.pluginBridge,
  ];
  for (const item of candidates) {
    if (item && typeof item === "object"
      && (typeof item.apiGet === "function" || typeof item.ready === "function")) return item;
  }
  return null;
}

const bridge = resolveBridge();

const NO_BRIDGE = "AstrBot 未注入 bridge SDK，请升级 AstrBot 或在 Dashboard 内打开本页";

/** 兼容「有信封 / 无信封」两种后端返回形态。 */
function unwrap(res) {
  if (res && typeof res === "object" && res.data !== undefined && res.status !== undefined) {
    return res.data;
  }
  return res;
}

/** 统一校验 {status:"ok"|"error"} 契约，error 直接抛出给调用方的 catch。 */
function assertOk(body) {
  if (!body || typeof body !== "object") throw new Error("后端返回格式异常");
  if (body.status === "error") throw new Error(plain(body.message, "后端返回错误"));
  return body;
}

async function apiGet(endpoint, params) {
  if (!bridge || typeof bridge.apiGet !== "function") throw new Error(NO_BRIDGE);
  return assertOk(unwrap(await bridge.apiGet(endpoint, params)));
}

async function apiPost(endpoint, body) {
  if (!bridge || typeof bridge.apiPost !== "function") throw new Error(NO_BRIDGE);
  return assertOk(unwrap(await bridge.apiPost(endpoint, body)));
}

async function apiUpload(endpoint, file) {
  if (!bridge || typeof bridge.upload !== "function") throw new Error(NO_BRIDGE);
  return assertOk(unwrap(await bridge.upload(endpoint, file)));
}

async function apiDownload(endpoint, params, filename) {
  if (!bridge || typeof bridge.download !== "function") throw new Error(NO_BRIDGE);
  return bridge.download(endpoint, params, filename);
}

/** bridge.t 的安全包装：拿不到就用中文兜底文案。 */
function t(key, fallback) {
  try {
    if (bridge && typeof bridge.t === "function") {
      const value = bridge.t(key, fallback);
      if (typeof value === "string" && value && value !== key) return value;
    }
  } catch (_) { /* ignore */ }
  return fallback;
}

async function withBusy(node, action) {
  if (node) { node.classList.add("is-busy"); node.disabled = true; }
  try {
    return await action();
  } finally {
    if (node) { node.classList.remove("is-busy"); node.disabled = false; }
  }
}
/* ---- 05 偏好（主题 / 紧凑 / 当前 tab，600ms 防抖回写后端） ---------------- */

function themeMeta(key) {
  return state.themes.find((item) => item.key === key)
    || FALLBACK_THEMES.find((item) => item.key === key)
    || FALLBACK_THEMES[0];
}

function applyTheme(key, options = {}) {
  const next = THEME_KEYS.has(key) ? key : DEFAULT_THEME;
  state.theme = next;
  document.documentElement.setAttribute("data-alice-theme", next);
  renderThemeControl();
  if (state.view === "about") renderAbout();
  if (options.persist !== false) pushPrefs();
}

function applyCompact(on, options = {}) {
  state.compact = Boolean(on);
  document.documentElement.setAttribute("data-alice-compact", state.compact ? "1" : "0");
  const btn = $("#btn-compact");
  if (btn) btn.setAttribute("aria-pressed", state.compact ? "true" : "false");
  if (options.persist !== false) pushPrefs();
}

const pushPrefs = debounce(async () => {
  if (!state.booted) return;
  try {
    await apiPost("state", {
      theme: state.theme,
      tab: state.view,
      compact: state.compact,
      config_group: state.cfg.current || null,
    });
  } catch (_) {
    /* 偏好持久化失败不打扰用户：老版本后端可能没有 state 端点。 */
  }
}, PREF_SAVE_DELAY);

async function loadPrefs() {
  try {
    const body = await apiGet("state");
    const prefs = (body && body.state) || {};
    if (typeof prefs.theme === "string") applyTheme(prefs.theme, { persist: false });
    if (typeof prefs.compact === "boolean") applyCompact(prefs.compact, { persist: false });
    if (typeof prefs.config_group === "string" && prefs.config_group) state.cfg.current = prefs.config_group;
    if (typeof prefs.tab === "string" && VIEW_KEYS.includes(prefs.tab)) return prefs.tab;
  } catch (_) {
    /* 忽略：偏好只是锦上添花 */
  }
  return null;
}

/* ---- 06 数据加载 --------------------------------------------------------- */

async function loadMeta() {
  try {
    const body = await apiGet("meta");
    state.meta = body;
    state.metaError = null;
    if (Array.isArray(body.themes) && body.themes.length) {
      state.themes = body.themes.filter((item) => item && THEME_KEYS.has(item.key));
      if (!state.themes.length) state.themes = FALLBACK_THEMES.slice();
    }
    const plugin = body.plugin || {};
    const title = $("#brand-title");
    const sub = $("#brand-sub");
    if (title) title.textContent = plain(plugin.display_name, "爱丽丝的图片助手");
    if (sub) sub.textContent = plain(plugin.tagline, "找图 · 溯源 · Pixiv · VLM 复核");
    if (plugin.display_name) document.title = plugin.display_name + " · 控制台";
  } catch (error) {
    state.meta = null;
    state.metaError = errText(error);
  }
  renderThemeControl();
  renderTabbar();
  renderStatus();
}

async function loadHealth() {
  state.healthBusy = true;
  try {
    const body = await apiGet("health");
    state.health = body;
    state.healthError = null;
  } catch (error) {
    state.health = null;
    state.healthError = errText(error);
  } finally {
    state.healthBusy = false;
  }
}

function indexConfig(groups) {
  const nav = [];
  const fields = [];
  const walk = (list, depth, parentKey, trail) => {
    for (const group of Array.isArray(list) ? list : []) {
      if (!group || typeof group !== "object") continue;
      const key = parentKey ? parentKey + "/" + String(group.key) : String(group.key || "");
      const label = plain(group.label, group.key);
      const path = trail.concat([label]);
      const children = Array.isArray(group.groups) ? group.groups : [];
      nav.push({ key, group, depth, parent: parentKey, label, trail: path, hasChildren: children.length > 0 });
      for (const field of Array.isArray(group.fields) ? group.fields : []) {
        if (field && typeof field === "object" && field.path) fields.push({ field, navKey: key, trail: path });
      }
      walk(children, depth + 1, key, path);
    }
  };
  walk(groups, 0, "", []);
  return { nav, fields };
}

function adoptConfig(groups) {
  state.cfg.groups = Array.isArray(groups) ? groups : [];
  const indexed = indexConfig(state.cfg.groups);
  state.cfg.nav = indexed.nav;
  state.cfg.fields = indexed.fields;
  state.cfg.loaded = true;
  const keys = new Set(indexed.nav.map((item) => item.key));
  if (!state.cfg.current || !keys.has(state.cfg.current)) {
    state.cfg.current = indexed.nav.length ? indexed.nav[0].key : null;
  }
  expandAncestors(state.cfg.current);
}

function expandAncestors(navKey) {
  if (!navKey) return;
  const parts = String(navKey).split("/");
  for (let i = 1; i <= parts.length; i += 1) {
    state.cfg.expanded.add(parts.slice(0, i).join("/"));
  }
}

async function loadConfig(force = false) {
  if (state.cfg.loaded && !force) return;
  state.cfg.busy = true;
  state.cfg.error = null;
  renderConfig();
  try {
    const body = await apiGet("config");
    adoptConfig(body.groups);
  } catch (error) {
    state.cfg.error = errText(error);
    state.cfg.loaded = false;
  } finally {
    state.cfg.busy = false;
    renderConfig();
    renderTabbar();
    renderStatus();
  }
}

async function loadCommands(force = false) {
  if (state.cmd.loaded && !force) return;
  state.cmd.busy = true;
  state.cmd.error = null;
  renderCommands();
  try {
    const body = await apiGet("commands");
    const groups = Array.isArray(body.groups) ? body.groups : [];
    state.cmd.groups = groups;
    state.cmd.total = groups.reduce((sum, g) => sum + (Array.isArray(g.commands) ? g.commands.length : 0), 0);
    state.cmd.loaded = true;
  } catch (error) {
    state.cmd.error = errText(error);
    state.cmd.loaded = false;
  } finally {
    state.cmd.busy = false;
    renderCommands();
    renderTabbar();
    renderStatus();
  }
}

/** 首次进入某个 tab 时按需拉数据。 */
function ensureViewData(view) {
  if (view === "config") loadConfig(false);
  else if (view === "commands") loadCommands(false);
}

/* ---- 07 通用组件 --------------------------------------------------------- */

function card(spec) {
  const head = [];
  if (spec.icon) head.push(el("span", { class: "head-icon" }, icon(spec.icon)));
  const headText = el("span", { class: "head-text" });
  if (spec.eyebrow) headText.appendChild(el("span", { class: "eyebrow", text: spec.eyebrow }));
  headText.appendChild(el("h2", { text: plain(spec.title, "") }));
  if (spec.desc) headText.appendChild(el("p", { text: spec.desc }));
  head.push(headText);
  if (spec.actions && spec.actions.length) head.push(el("div", { class: "head-actions" }, spec.actions));

  const parts = [el("div", { class: "card-head" }, head)];
  parts.push(el("div", { class: "card-body" + (spec.flush ? " flush" : "") }, spec.body || []));
  if (spec.foot && spec.foot.length) parts.push(el("div", { class: "card-foot" }, spec.foot));

  const classes = ["card", "hoverable"];
  if (spec.className) classes.push(spec.className);
  return el("section", { class: classes.join(" ") }, parts);
}

/** rows: [标签, 值(字符串或节点), { mono:false, dim:true }] */
function kvTable(rows) {
  const box = el("dl", { class: "kv" });
  for (const row of rows) {
    if (!row) continue;
    const [label, value, opts] = row;
    const valueNode = value instanceof Node
      ? value
      : el("span", { text: plain(value) });
    const holder = el("dd", { class: "kv-v" + (opts && opts.dim ? " dim" : "") }, valueNode);
    box.appendChild(el("div", { class: "kv-row" }, [
      el("dt", { class: "kv-k", text: label }),
      holder,
    ]));
  }
  return box;
}

function pill(text, level, iconName) {
  const cls = ["pill"];
  if (level) cls.push(level);
  const kids = [];
  if (iconName) kids.push(icon(iconName));
  kids.push(el("span", { text: plain(text) }));
  return el("span", { class: cls.join(" ") }, kids);
}

const levelClass = (level) => (level === "error" ? "err" : level === "warn" ? "warn" : level === "ok" ? "ok" : "info");

function codeChip(text) {
  return el("code", { text: plain(text) });
}

function emptyBox(iconName, title, desc) {
  return el("div", { class: "empty" }, [
    icon(iconName, "xl"),
    el("strong", { text: title }),
    desc ? el("span", { text: desc }) : null,
  ]);
}

function inlineError(message, retry) {
  const kids = [icon("alert"), el("span", { class: "msg", text: plain(message, "请求失败") })];
  if (retry) {
    kids.push(el("button", {
      class: "btn sm ghost",
      type: "button",
      onclick: retry,
    }, [icon("refresh", "sm"), el("span", { text: "重试" })]));
  }
  return el("div", { class: "inline-error" }, kids);
}

function skeletonLines(count = 3) {
  const box = el("div", { class: "skeleton" });
  const widths = ["w-60", "w-80", "w-40", "w-80", "w-60"];
  for (let i = 0; i < count; i += 1) {
    box.appendChild(el("div", { class: "sk line " + widths[i % widths.length] }));
  }
  return box;
}

function skeletonTiles(count = 4) {
  const box = el("div", { class: "imggrid" });
  for (let i = 0; i < count; i += 1) box.appendChild(el("div", { class: "sk tile" }));
  return box;
}

function switchControl(labelText, checked, onChange, extra) {
  const input = el("input", { type: "checkbox", onchange: (ev) => onChange(ev.target.checked) });
  input.checked = Boolean(checked);
  if (extra && extra.ariaLabel) input.setAttribute("aria-label", extra.ariaLabel);
  return el("label", { class: "switch" }, [
    input,
    el("span", { class: "switch-track", "aria-hidden": "true" }),
    el("span", { class: "switch-text" + (checked ? "" : " off"), text: labelText }),
  ]);
}

function chipButton(labelText, active, onClick, opts) {
  const kids = [];
  if (opts && opts.icon) kids.push(icon(opts.icon));
  kids.push(el("span", { text: labelText }));
  if (opts && opts.note) kids.push(el("span", { class: "n", text: opts.note }));
  return el("button", {
    class: "chip",
    type: "button",
    "aria-pressed": active ? "true" : "false",
    title: (opts && opts.title) || labelText,
    onclick: onClick,
  }, kids);
}

function iconButton(iconName, label, onClick, opts) {
  return el("button", {
    class: "icon-btn" + (opts && opts.className ? " " + opts.className : ""),
    type: "button",
    "aria-label": label,
    title: label,
    onclick: onClick,
  }, icon(iconName, opts && opts.iconClass));
}

function textButton(iconName, label, onClick, opts) {
  return el("button", {
    class: "btn" + (opts && opts.className ? " " + opts.className : ""),
    type: "button",
    title: (opts && opts.title) || label,
    onclick: onClick,
  }, [iconName ? icon(iconName, "sm") : null, el("span", { text: label })]);
}

function filterBar(placeholder, value, onInput) {
  const input = el("input", {
    type: "search",
    placeholder,
    "aria-label": placeholder,
    value,
    oninput: (ev) => onInput(ev.target.value),
  });
  return el("div", { class: "filterbar" }, [icon("filter", "sm"), input]);
}
/* ---- 08 视图 ------------------------------------------------------------- */

/* 视图内部的「局部刷新锚点」：表单只建一次，避免每次输入都重建导致焦点丢失。 */
let searchResultHost = null;
let reversePreviewHost = null;
let reverseResultHost = null;
let reverseUrlInput = null;
let cfgNavHost = null;
let cfgPanelHost = null;
let cfgDirtyNote = null;
let cfgSaveBtn = null;
let cfgDiscardBtn = null;
let cmdListHost = null;

const viewNode = (key) => $('.view[data-view="' + key + '"]');

/** 用节点数组整体替换某个视图的内容（自动过滤空值）。 */
function paint(key, nodes) {
  const host = viewNode(key);
  if (!host) return null;
  host.replaceChildren(...nodes.filter(Boolean));
  return host;
}

function renderView(key) {
  if (key === "overview") renderOverview();
  else if (key === "search") renderSearch();
  else if (key === "reverse") renderReverse();
  else if (key === "config") renderConfig();
  else if (key === "commands") renderCommands();
  else if (key === "about") renderAbout();
}

function labelledRow(label, node, hint) {
  return el("div", { class: "field" }, [
    el("span", { class: "field-label", text: label }),
    node,
    hint ? el("span", { class: "field-hint", text: hint }) : null,
  ]);
}

function normLevel(level) {
  const key = String(level === null || level === undefined ? "info" : level).toLowerCase();
  return LEVEL_ICON[key] ? key : "info";
}

/** errors 可能是字符串数组，也可能是 {message}/{reason} 对象数组，统一成一行文字。 */
function messageOf(item) {
  if (typeof item === "string") return item;
  if (!item || typeof item !== "object") return plain(item);
  const head = item.strategy || item.source || item.stage;
  const body = item.message || item.detail || item.reason || item.error;
  const parts = [head ? String(head) : null, body ? String(body) : null].filter(Boolean);
  return parts.length ? parts.join("：") : plain(item);
}

function foldList(title, items, opts) {
  const lines = (Array.isArray(items) ? items : []).map(messageOf).filter((s) => s && s !== "—");
  if (!lines.length) return null;
  const kids = [
    icon("caret", "caret"),
    el("span", { text: title }),
    pill(String(lines.length), (opts && opts.level) || "mute"),
  ];
  return el("details", { class: "fold" }, [
    el("summary", {}, kids),
    el("div", { class: "fold-body" }, lines.map((line) => el("p", { text: line }))),
  ]);
}

/* -- 08.1 概览 ------------------------------------------------------------- */

const FEATURE_LABEL = {
  find_image: "文字找图",
  reverse_image: "以图溯源",
  pixiv: "Pixiv 检索",
  soutu: "搜图神器",
  serpapi: "SerpApi",
  llm_tools: "LLM 工具调用",
};

function checkNode(item) {
  const lvl = normLevel(item && item.level);
  return el("div", { class: "check", dataset: { level: lvl } }, [
    el("span", { class: "check-icon" }, icon(LEVEL_ICON[lvl])),
    el("div", { class: "check-body" }, [
      el("span", { class: "check-label", text: plain(item && item.label, plain(item && item.key, "检查项")) }),
      el("span", { class: "check-value", text: plain(item && item.value) }),
      item && item.hint ? el("span", { class: "check-hint", text: item.hint }) : null,
    ]),
    pill(LEVEL_TEXT[lvl], levelClass(lvl)),
  ]);
}

function statNode(num, label, note) {
  return el("div", { class: "stat" }, [
    el("span", { class: "stat-num", text: fmtNum(num) }),
    el("span", { class: "stat-label", text: label }),
    note ? el("span", { class: "stat-note", text: note }) : null,
  ]);
}

function togglePills(entries) {
  const box = el("div", { class: "chips" });
  if (!entries.length) { box.appendChild(pill("暂无数据", "mute")); return box; }
  for (const item of entries) {
    box.appendChild(pill(item.label, item.on ? "ok" : "mute", item.on ? "check" : "close"));
  }
  return box;
}

function healthLevelCounts() {
  const checks = (state.health && Array.isArray(state.health.checks)) ? state.health.checks : [];
  const acc = { ok: 0, warn: 0, error: 0, info: 0 };
  for (const item of checks) acc[normLevel(item && item.level)] += 1;
  return acc;
}

function healthSummaryRow() {
  const acc = healthLevelCounts();
  if (!(acc.ok + acc.warn + acc.error + acc.info)) return null;
  const row = el("div", { class: "row tight" });
  for (const key of ["error", "warn", "ok", "info"]) {
    if (!acc[key]) continue;
    row.appendChild(pill(LEVEL_TEXT[key] + " " + acc[key], levelClass(key), LEVEL_ICON[key]));
  }
  return row;
}

async function reloadHealth(btn) {
  await withBusy(btn, () => loadHealth());
  if (state.view === "overview") renderOverview();
  renderStatus();
}

function healthCard() {
  const body = [];
  if (state.healthBusy && !state.health) body.push(skeletonLines(4));
  else if (state.healthError) body.push(inlineError(state.healthError, () => reloadHealth(null)));
  else {
    const checks = (state.health && Array.isArray(state.health.checks)) ? state.health.checks : [];
    if (!checks.length) body.push(emptyBox("shield", "暂无健康检查项", "后端没有返回任何检查结果"));
    else body.push(el("div", { class: "grid cols-2" }, checks.map(checkNode)));
  }
  const summary = healthSummaryRow();
  return card({
    eyebrow: "HEALTH",
    icon: "shield",
    title: "健康检查",
    desc: "凭据、依赖与运行环境自检",
    actions: [iconButton("refresh", "重新检查", (ev) => reloadHealth(ev.currentTarget), { className: "sm" })],
    body,
    foot: summary ? [summary] : null,
  });
}

function capabilityCard() {
  const meta = state.meta || {};
  const features = (meta.features && typeof meta.features === "object") ? meta.features : {};
  const featureEntries = Object.keys(features).map((key) => ({
    label: plain(FEATURE_LABEL[key], key),
    on: Boolean(features[key]),
  }));
  const listEntries = (arr) => (Array.isArray(arr) ? arr : []).map((item) => ({
    label: plain(item && item.label, plain(item && item.key, "未命名")),
    on: Boolean(item && item.enabled),
  }));
  return card({
    eyebrow: "CAPABILITY",
    icon: "layers",
    title: "能力与来源",
    desc: "功能开关、检索源与溯源策略",
    body: [
      labelledRow("功能开关", togglePills(featureEntries)),
      labelledRow("检索源", togglePills(listEntries(meta.sources))),
      labelledRow("溯源策略", togglePills(listEntries(meta.strategies))),
    ],
  });
}

function runtimeCard() {
  const rt = (state.health && state.health.runtime) || null;
  const body = [];
  if (state.healthBusy && !rt) body.push(skeletonLines(5));
  else if (!rt) body.push(emptyBox("info", "暂无运行时信息", state.healthError || "后端未返回 runtime 字段"));
  else {
    const providers = Array.isArray(rt.providers) ? rt.providers : [];
    const providerText = providers.length
      ? providers.map((p) => plain(p && p.id, "?") + " / " + plain(p && p.model, "?")).join("，")
      : "—";
    body.push(kvTable([
      ["Web 后端", rt.web_backend],
      ["Playwright", rt.playwright],
      ["VLM 提供商", rt.vlm_provider],
      ["已注册提供商", providerText, { dim: !providers.length }],
      ["图片上下文会话", fmtNum(rt.image_context_sessions)],
      ["缓存图片", fmtNum(rt.cached_images)],
    ]));
  }
  return card({ eyebrow: "RUNTIME", icon: "spark", title: "运行时", desc: "进程内依赖与缓存状态", body });
}

function limitsCard() {
  const limits = (state.meta && state.meta.limits) || {};
  return card({
    eyebrow: "LIMITS",
    icon: "clock",
    title: "配额与限制",
    desc: "由后端下发，本页表单据此约束取值",
    body: [kvTable([
      ["单次最多张数", fmtNum(limits.search_count_max)],
      ["预览最多张数", fmtNum(limits.preview_max_items)],
      ["预览有效期", fmtSeconds(limits.preview_ttl_seconds)],
      ["上传体积上限", fmtBytes(limits.upload_max_bytes)],
    ])],
  });
}

function renderOverview() {
  const counts = (state.meta && state.meta.counts) || {};
  const nodes = [];
  if (state.metaError) {
    nodes.push(card({
      eyebrow: "BACKEND",
      icon: "alert",
      title: "后端接口不可用",
      className: "accent",
      body: [
        inlineError(state.metaError, () => refreshAll(null)),
        el("p", { class: "field-hint", text: "请确认 AstrBot 版本支持插件页面，并在 Dashboard 内打开本页面。界面仍可浏览，但数据为空。" }),
      ],
    }));
  }
  nodes.push(el("div", { class: "grid cols-4" }, [
    statNode(counts.commands, "聊天指令", "可在指令表查看"),
    statNode(counts.config_fields, "配置项", "可在配置页编辑"),
    statNode(counts.sources, "检索源", "找图可用来源"),
    statNode(counts.strategies, "溯源策略", "反查引擎"),
  ]));
  nodes.push(healthCard());
  nodes.push(el("div", { class: "grid cols-2" }, [capabilityCard(), runtimeCard()]));
  nodes.push(limitsCard());
  paint("overview", nodes);
}
/* -- 08.2 找图 ------------------------------------------------------------- */

function searchMax() {
  const raw = Number(state.meta && state.meta.limits && state.meta.limits.search_count_max);
  return Number.isFinite(raw) && raw >= 1 ? Math.min(Math.round(raw), 20) : 5;
}

function sourceChoices() {
  const list = [{ key: "auto", label: "自动选源", note: "按回退顺序" }];
  const raw = (state.meta && Array.isArray(state.meta.sources)) ? state.meta.sources : [];
  for (const item of raw) {
    if (!item || !item.key) continue;
    list.push({
      key: String(item.key),
      label: plain(item.label, item.key),
      note: item.enabled ? null : "未启用",
    });
  }
  return list;
}

/** 带文案自更新的开关：避免为了刷新一个字而重建整块表单。 */
function liveSwitch(onText, offText, checked, onChange, ariaLabel) {
  const node = switchControl(checked ? onText : offText, checked, (value) => {
    const text = node.querySelector(".switch-text");
    if (text) {
      text.textContent = value ? onText : offText;
      text.classList.toggle("off", !value);
    }
    onChange(value);
  }, { ariaLabel: ariaLabel || onText });
  return node;
}

function searchFormCard() {
  const maxCount = searchMax();
  if (state.search.count > maxCount) state.search.count = maxCount;

  const input = el("input", {
    type: "text",
    placeholder: "例如：银发少女 白裙 逆光",
    value: state.search.query,
    "aria-label": "图片描述",
    oninput: (ev) => { state.search.query = ev.target.value; },
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); runSearch(submit); }
  });

  const chips = el("div", { class: "chips" });
  const chipMap = new Map();
  for (const choice of sourceChoices()) {
    const node = chipButton(choice.label, state.search.source === choice.key, () => {
      state.search.source = choice.key;
      for (const [key, btn] of chipMap) {
        btn.setAttribute("aria-pressed", key === choice.key ? "true" : "false");
      }
    }, { note: choice.note, title: "使用「" + choice.label + "」检索" });
    chipMap.set(choice.key, node);
    chips.appendChild(node);
  }

  const num = el("span", { class: "num", text: String(state.search.count), "aria-live": "polite" });
  const setCount = (next) => {
    state.search.count = Math.min(maxCount, Math.max(1, next));
    num.textContent = String(state.search.count);
  };
  const stepper = el("div", { class: "stepper" }, [
    iconButton("minus", "减少一张", () => setCount(state.search.count - 1), { className: "sm" }),
    num,
    iconButton("plus", "增加一张", () => setCount(state.search.count + 1), { className: "sm" }),
  ]);

  const review = liveSwitch("VLM 复核已开", "VLM 复核已关", state.search.review, (value) => {
    state.search.review = value;
  }, "启用 VLM 复核");

  const submit = textButton("search", "开始找图", () => runSearch(submit), { className: "primary" });

  return card({
    eyebrow: "SEARCH",
    icon: "search",
    title: "按描述找图",
    desc: "多源回退检索，可选 VLM 复核筛掉不匹配的结果",
    body: [
      labelledRow("图片描述", input, "按 Enter 直接提交；描述越具体命中率越高。"),
      labelledRow("检索源", chips),
      el("div", { class: "row" }, [
        el("span", { class: "field-label", text: "张数" }),
        stepper,
        el("span", { class: "field-hint", text: "最多 " + maxCount + " 张" }),
        el("span", { class: "spacer" }),
        review,
        submit,
      ]),
    ],
  });
}

async function runSearch(btn) {
  if (state.search.busy) return;
  const query = String(state.search.query || "").trim();
  if (!query) { toast("请先填写图片描述", "warn"); return; }
  state.search.busy = true;
  state.search.error = null;
  renderSearchResults();
  try {
    await withBusy(btn, async () => {
      const body = await apiPost("search", {
        query,
        source: state.search.source,
        count: state.search.count,
        review: state.search.review,
      });
      state.search.data = body;
      const result = body.result || {};
      if (result.success === false) toast("没有找到合适的图片，可换个描述再试", "warn");
      else toast("找图完成 · " + fmtMs(body.elapsed_ms), "ok");
    });
  } catch (error) {
    state.search.data = null;
    state.search.error = errText(error);
    toast("找图失败：" + state.search.error, "err");
  } finally {
    state.search.busy = false;
    renderSearchResults();
  }
}

function guessExt(url) {
  const matched = /\.(png|jpe?g|webp|gif|bmp)(?:[?#]|$)/i.exec(String(url || ""));
  return matched ? "." + matched[1].toLowerCase() : ".png";
}

async function downloadImage(item, btn) {
  const token = item && item.token;
  if (!token) { toast("该图片没有可下载的 token", "warn"); return; }
  const safeToken = String(token).replace(/[^\w.-]+/g, "");
  const filename = "alice-" + (safeToken || "image") + guessExt(item && item.url);
  try {
    await withBusy(btn, () => apiDownload("image", { token }, filename));
    toast("已开始下载 " + filename, "ok");
  } catch (error) {
    toast("下载失败：" + errText(error), "err");
  }
}

function shotCard(item, index) {
  const label = plain(item && item.label, "图片 " + (index + 1));
  const preview = safeUrl(item && item.preview);
  const origin = safeUrl(item && item.url);
  const src = preview || origin;
  const width = Number(item && item.width);
  const height = Number(item && item.height);
  const dims = (width > 0 && height > 0) ? width + " × " + height : "尺寸未知";
  const score = Number(item && item.score);

  const shotKids = [];
  if (src) {
    const img = el("img", { alt: label, loading: "lazy", decoding: "async" });
    img.src = src;
    shotKids.push(img);
  } else {
    shotKids.push(el("span", { class: "shot-none" }, icon("image", "xl")));
  }
  shotKids.push(el("span", { class: "shot-tag", text: label }));
  shotKids.push(el("span", { class: "zoom" }, icon("zoom", "sm")));

  const shot = el("button", {
    class: "shot-btn",
    type: "button",
    "aria-label": "放大查看 " + label,
    onclick: () => openLightbox(origin || src, label + " · " + dims),
  }, shotKids);

  const acts = [];
  if (item && item.token) {
    acts.push(textButton("download", "下载", (ev) => downloadImage(item, ev.currentTarget), { className: "sm" }));
  }
  if (origin) {
    acts.push(el("a", {
      class: "btn sm ghost",
      href: origin,
      target: "_blank",
      rel: "noopener noreferrer",
      title: "在新标签打开原图",
    }, [icon("external", "sm"), el("span", { text: "原图" })]));
  }

  const metaLine = [fmtBytes(item && item.bytes)];
  if (Number.isFinite(score)) metaLine.push("得分 " + score.toFixed(2));

  return el("div", { class: "shot-card" }, [
    shot,
    el("div", { class: "shot-meta" }, [
      el("b", { text: dims }),
      el("span", { class: "mono", text: metaLine.join(" · ") }),
    ]),
    acts.length ? el("div", { class: "shot-acts" }, acts) : null,
  ]);
}

function traceTimeline(trace) {
  return el("div", { class: "timeline" }, trace.map((item) => {
    const lvl = normLevel(item && item.level);
    const head = plain(item && item.stage, "");
    const name = plain(item && item.label, "");
    const label = [head, name].filter((s) => s && s !== "—").join(" · ") || "步骤";
    return el("div", { class: "tl-item", dataset: { level: lvl } }, [
      el("span", { class: "tl-rail" }, el("span", { class: "tl-dot" })),
      el("div", { class: "tl-body" }, [
        el("span", { class: "tl-label", text: label }),
        item && item.detail ? el("span", { class: "tl-detail", text: item.detail }) : null,
      ]),
    ]);
  }));
}

function searchResultNodes(data) {
  const result = (data && data.result) || {};
  const images = Array.isArray(data.images) ? data.images : [];
  const trace = Array.isArray(data.trace) ? data.trace : [];
  const attempted = Array.isArray(result.attempted_sources) ? result.attempted_sources : [];

  const badges = el("div", { class: "row tight" }, [
    result.success === false ? pill("未命中", "warn", "alert") : pill("成功", "ok", "check"),
    pill("来源 " + plain(result.source), "info", "globe"),
    pill("耗时 " + fmtMs(data.elapsed_ms), "mute", "clock"),
    result.review_fallback ? pill("复核回退", "warn", "alert") : null,
    result.delivery_uncertain ? pill("投递不确定", "warn", "alert") : null,
  ].filter(Boolean));

  const attemptedRow = attempted.length
    ? el("div", { class: "row tight" }, [
      el("span", { class: "field-hint", text: "尝试顺序" }),
      ...attempted.map((key) => codeChip(key)),
    ])
    : null;

  const grid = images.length
    ? el("div", { class: "imggrid" }, images.map(shotCard))
    : emptyBox("image", "本次没有返回图片", "可以换个描述、放宽复核或切换检索源");

  const clear = textButton("undo", "清空结果", () => {
    state.search.data = null;
    state.search.error = null;
    renderSearchResults();
  }, { className: "sm ghost" });

  const resultCard = card({
    eyebrow: "RESULT",
    icon: "image",
    title: "检索结果 · " + images.length + " 张",
    desc: "点击图片放大；下载会经 AstrBot 后端取原图",
    actions: [clear],
    body: [badges, attemptedRow, grid].filter(Boolean),
  });

  const folds = [
    foldList("错误明细", result.errors, { level: "err" }),
    foldList("警告明细", result.warnings, { level: "warn" }),
  ].filter(Boolean);

  const traceCard = card({
    eyebrow: "TRACE",
    icon: "layers",
    title: "执行轨迹",
    desc: "本次耗时 " + fmtMs(data.elapsed_ms),
    body: [
      trace.length ? traceTimeline(trace) : emptyBox("layers", "没有轨迹记录", "后端未返回 trace 字段"),
      ...folds,
    ],
  });

  return [resultCard, traceCard];
}

function renderSearchResults() {
  if (!searchResultHost) return;
  const nodes = [];
  if (state.search.busy) {
    nodes.push(card({
      eyebrow: "SEARCHING",
      icon: "clock",
      title: "正在检索",
      desc: "多源回退与 VLM 复核可能需要较长时间，请耐心等待",
      body: [skeletonTiles(4), skeletonLines(3)],
    }));
  } else if (state.search.error) {
    nodes.push(card({
      eyebrow: "ERROR",
      icon: "alert",
      title: "检索失败",
      body: [inlineError(state.search.error, () => runSearch(null))],
    }));
  } else if (!state.search.data) {
    nodes.push(card({
      eyebrow: "RESULT",
      icon: "image",
      title: "检索结果",
      body: [emptyBox("search", "还没有检索记录", "填写描述后点击「开始找图」")],
    }));
  } else {
    nodes.push(...searchResultNodes(state.search.data));
  }
  searchResultHost.replaceChildren(...nodes.filter(Boolean));
}

function renderSearch() {
  searchResultHost = el("div", { class: "stack" });
  paint("search", [searchFormCard(), searchResultHost]);
  renderSearchResults();
}
/* -- 08.3 溯源 ------------------------------------------------------------- */

function strategyChoices() {
  const raw = (state.meta && Array.isArray(state.meta.strategies)) ? state.meta.strategies : [];
  const list = raw.filter((item) => item && item.key).map((item) => ({
    key: String(item.key),
    label: plain(item.label, item.key),
    enabled: item.enabled !== false,
  }));
  if (list.length) return list;
  return [
    { key: "saucenao", label: "SauceNAO", enabled: true },
    { key: "google_lens", label: "Google Lens", enabled: true },
    { key: "ascii2d", label: "Ascii2D", enabled: true },
  ];
}

/** null 表示「跟随后端默认」：即所有 enabled 的策略。 */
function activeStrategies() {
  const choices = strategyChoices();
  if (Array.isArray(state.reverse.strategies)) {
    const allowed = new Set(choices.map((item) => item.key));
    return state.reverse.strategies.filter((key) => allowed.has(key));
  }
  return choices.filter((item) => item.enabled).map((item) => item.key);
}

function toggleStrategy(key) {
  const current = new Set(activeStrategies());
  if (current.has(key)) current.delete(key);
  else current.add(key);
  state.reverse.strategies = strategyChoices()
    .map((item) => item.key)
    .filter((item) => current.has(item));
}

function uploadMaxBytes() {
  const raw = Number(state.meta && state.meta.limits && state.meta.limits.upload_max_bytes);
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
}

async function handleReverseFile(file) {
  if (!file) return;
  if (file.type && !/^image\//i.test(file.type)) { toast("请选择图片文件", "warn"); return; }
  const max = uploadMaxBytes();
  if (max && file.size > max) { toast("图片超过上限 " + fmtBytes(max), "warn"); return; }
  if (state.reverse.uploading) return;
  state.reverse.uploading = true;
  state.reverse.error = null;
  renderReversePreview();
  try {
    const body = await apiUpload("upload", file);
    state.reverse.upload = {
      token: body.token,
      preview: body.preview,
      width: body.width,
      height: body.height,
      bytes: body.bytes,
      filename: body.filename || file.name,
    };
    state.reverse.url = "";
    if (reverseUrlInput) reverseUrlInput.value = "";
    toast("图片已上传，可直接开始溯源", "ok");
  } catch (error) {
    state.reverse.upload = null;
    state.reverse.error = errText(error);
    toast("上传失败：" + state.reverse.error, "err");
  } finally {
    state.reverse.uploading = false;
    renderReversePreview();
  }
}

function clearReverseUpload() {
  state.reverse.upload = null;
  state.reverse.error = null;
  renderReversePreview();
}

function reverseDropzone() {
  const input = el("input", {
    type: "file",
    accept: "image/*",
    onchange: (ev) => {
      const file = ev.target.files && ev.target.files[0];
      ev.target.value = "";
      handleReverseFile(file);
    },
  });
  const max = uploadMaxBytes();
  const zone = el("div", {
    class: "dropzone",
    role: "button",
    tabindex: "0",
    "aria-label": "选择或拖拽图片上传",
    onclick: () => input.click(),
    onkeydown: (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); input.click(); }
    },
    ondragover: (ev) => { ev.preventDefault(); zoneOver(true); },
    ondragenter: (ev) => { ev.preventDefault(); zoneOver(true); },
    ondragleave: () => zoneOver(false),
    ondrop: (ev) => {
      ev.preventDefault();
      zoneOver(false);
      const file = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      handleReverseFile(file);
    },
  }, [
    icon("upload", "xl"),
    el("strong", { text: "拖拽图片到此处，或点击选择" }),
    el("span", { text: "也可以在本页直接 Ctrl+V 粘贴剪贴板里的图片" + (max ? "；单张上限 " + fmtBytes(max) : "") }),
    input,
  ]);
  function zoneOver(on) { zone.classList.toggle("is-over", on); }
  return zone;
}

function reverseUrlRow() {
  reverseUrlInput = el("input", {
    type: "url",
    placeholder: "https://example.com/image.jpg",
    value: state.reverse.url,
    "aria-label": "图片直链",
    oninput: (ev) => { state.reverse.url = ev.target.value; },
  });
  reverseUrlInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); runReverse(null); }
  });
  const clear = iconButton("close", "清空链接", () => {
    state.reverse.url = "";
    if (reverseUrlInput) reverseUrlInput.value = "";
  });
  return el("div", { class: "urlbar" }, [reverseUrlInput, clear]);
}

function reversePreviewNode() {
  if (state.reverse.uploading) {
    return el("div", { class: "preview-card" }, [
      el("div", { class: "sk block" }),
      el("div", { class: "preview-meta" }, [skeletonLines(2)]),
    ]);
  }
  const up = state.reverse.upload;
  if (!up) {
    if (state.reverse.error) return inlineError(state.reverse.error, null);
    return null;
  }
  const shot = el("div", { class: "shot" });
  const src = safeUrl(up.preview);
  if (src) {
    const img = el("img", { alt: "已上传图片预览", decoding: "async" });
    img.src = src;
    shot.appendChild(img);
  } else {
    shot.appendChild(icon("image", "xl"));
  }
  const width = Number(up.width);
  const height = Number(up.height);
  const dims = (width > 0 && height > 0) ? width + " × " + height : "尺寸未知";
  return el("div", { class: "preview-card" }, [
    shot,
    el("div", { class: "preview-meta" }, [
      el("b", { text: plain(up.filename, "已上传图片") }),
      el("span", { class: "mono", text: dims + " · " + fmtBytes(up.bytes) }),
      el("div", { class: "row tight" }, [
        pill("已就绪", "ok", "check"),
        textButton("trash", "移除", () => clearReverseUpload(), { className: "sm ghost danger" }),
      ]),
    ]),
  ]);
}

function renderReversePreview() {
  if (!reversePreviewHost) return;
  const node = reversePreviewNode();
  reversePreviewHost.replaceChildren(...(node ? [node] : []));
}

function reverseFormCard() {
  const chips = el("div", { class: "chips" });
  const chipMap = new Map();
  const syncChips = () => {
    const active = new Set(activeStrategies());
    for (const [key, btn] of chipMap) btn.setAttribute("aria-pressed", active.has(key) ? "true" : "false");
  };
  for (const choice of strategyChoices()) {
    const node = chipButton(choice.label, false, () => { toggleStrategy(choice.key); syncChips(); }, {
      note: choice.enabled ? null : "未启用",
      title: "切换策略「" + choice.label + "」",
    });
    chipMap.set(choice.key, node);
    chips.appendChild(node);
  }
  syncChips();

  reversePreviewHost = el("div", { class: "stack tight" });
  const submit = textButton("compass", "开始溯源", () => runReverse(submit), { className: "primary" });

  return card({
    eyebrow: "REVERSE",
    icon: "compass",
    title: "以图溯源",
    desc: "上传图片或贴图片直链，多策略并行反查来源",
    body: [
      reverseDropzone(),
      reversePreviewHost,
      labelledRow("图片直链", reverseUrlRow(), "已上传图片时优先使用上传结果。"),
      labelledRow("溯源策略", chips, "至少选择一项；未启用的策略可能会直接返回错误。"),
      el("div", { class: "row" }, [el("span", { class: "spacer" }), submit]),
    ],
  });
}

async function runReverse(btn) {
  if (state.reverse.busy) return;
  const token = state.reverse.upload && state.reverse.upload.token ? state.reverse.upload.token : null;
  const rawUrl = String(state.reverse.url || "").trim();
  const url = token ? null : safeUrl(rawUrl);
  if (!token && !url) {
    toast(rawUrl ? "图片链接需要是 http(s) 直链" : "请先上传图片或填写图片直链", "warn");
    return;
  }
  const strategies = activeStrategies();
  if (!strategies.length) { toast("请至少选择一个溯源策略", "warn"); return; }

  state.reverse.busy = true;
  state.reverse.error = null;
  renderReverseResults();
  try {
    await withBusy(btn, async () => {
      const body = await apiPost("reverse", { image_url: url, token, strategies });
      state.reverse.data = body;
      const hits = Array.isArray(body.results) ? body.results.length : 0;
      if (hits) toast("溯源完成 · " + hits + " 条结果 · " + fmtMs(body.elapsed_ms), "ok");
      else toast("溯源完成，但没有匹配结果", "warn");
    });
  } catch (error) {
    state.reverse.data = null;
    state.reverse.error = errText(error);
    toast("溯源失败：" + state.reverse.error, "err");
  } finally {
    state.reverse.busy = false;
    renderReverseResults();
  }
}

function similarityBar(item) {
  const score = Number(item && item.score);
  const track = el("span", { class: "track" });
  const fill = el("i", { class: "fill" });
  track.appendChild(fill);
  const hasScore = Number.isFinite(score) && score > 0;
  const percent = hasScore ? Math.max(0, Math.min(100, score <= 1 ? score * 100 : score)) : 0;
  fill.style.width = percent.toFixed(1) + "%";
  const text = plain(item && item.similarity, hasScore ? percent.toFixed(1) + "%" : "—");
  return el("div", { class: "simbar" }, [
    track,
    el("span", { class: "val" + (hasScore ? "" : " none"), text }),
  ]);
}

function reverseResultItem(item) {
  const title = plain(item && item.title, "未命名结果");
  const link = safeUrl(item && item.url);
  const thumb = safeUrl(item && item.thumbnail);

  const shotKids = [];
  if (thumb) {
    const img = el("img", { alt: title, loading: "lazy", decoding: "async" });
    img.src = thumb;
    shotKids.push(img);
  } else {
    shotKids.push(icon("image", "lg"));
  }
  const shot = el("button", {
    class: "result-shot",
    type: "button",
    "aria-label": "放大查看 " + title,
    onclick: () => openLightbox(thumb, title),
  }, shotKids);

  const extraPills = [];
  const extra = (item && item.extra && typeof item.extra === "object") ? item.extra : {};
  for (const key of Object.keys(extra).slice(0, 4)) {
    const value = extra[key];
    if (value === null || value === undefined || value === "") continue;
    extraPills.push(pill(key + " " + String(value), "mute", "tag"));
  }

  const acts = [];
  if (link) {
    acts.push(el("a", {
      class: "btn sm",
      href: link,
      target: "_blank",
      rel: "noopener noreferrer",
      title: "在新标签打开来源页",
    }, [icon("external", "sm"), el("span", { text: "打开原链接" })]));
    acts.push(iconButton("copy", "复制链接", async () => {
      const done = await copyText(link);
      toast(done ? "链接已复制" : "复制失败，请手动选择", done ? "ok" : "warn");
    }, { className: "sm" }));
  } else {
    acts.push(pill("无可用链接", "mute", "alert"));
  }

  return el("div", { class: "result-item" }, [
    shot,
    el("div", { class: "result-main" }, [
      el("div", { class: "row tight" }, [
        pill(plain(item && item.source, "未知来源"), "info", "globe"),
        ...extraPills,
      ]),
      el("span", { class: "result-title", text: title }),
      el("span", { class: "result-sub", text: "作者：" + plain(item && item.author) }),
      similarityBar(item),
      el("div", { class: "result-acts" }, acts),
    ]),
  ]);
}

function reverseResultNodes(data) {
  const results = Array.isArray(data.results) ? data.results : [];
  const list = results.length
    ? el("div", { class: "stack tight" }, results.map(reverseResultItem))
    : emptyBox("compass", "没有匹配到来源", "可以换个策略组合，或换一张更清晰的原图");
  const fold = foldList("策略错误", data.errors, { level: "err" });
  return [card({
    eyebrow: "MATCHES",
    icon: "compass",
    title: "溯源结果 · " + results.length + " 条",
    desc: "相似度条按后端给出的 score 绘制",
    actions: [textButton("undo", "清空结果", () => {
      state.reverse.data = null;
      state.reverse.error = null;
      renderReverseResults();
    }, { className: "sm ghost" })],
    body: [
      el("div", { class: "row tight" }, [
        pill("耗时 " + fmtMs(data.elapsed_ms), "mute", "clock"),
        pill("策略 " + activeStrategies().length, "info", "layers"),
      ]),
      list,
      fold,
    ].filter(Boolean),
  })];
}

function renderReverseResults() {
  if (!reverseResultHost) return;
  const nodes = [];
  if (state.reverse.busy) {
    nodes.push(card({
      eyebrow: "SEARCHING",
      icon: "clock",
      title: "正在反查",
      desc: "多个第三方站点串行/并行查询，可能耗时较久",
      body: [skeletonLines(2), el("div", { class: "sk block" }), el("div", { class: "sk block" })],
    }));
  } else if (state.reverse.error) {
    nodes.push(card({
      eyebrow: "ERROR",
      icon: "alert",
      title: "溯源失败",
      body: [inlineError(state.reverse.error, () => runReverse(null))],
    }));
  } else if (!state.reverse.data) {
    nodes.push(card({
      eyebrow: "MATCHES",
      icon: "compass",
      title: "溯源结果",
      body: [emptyBox("compass", "还没有溯源记录", "上传图片或贴链接后点击「开始溯源」")],
    }));
  } else {
    nodes.push(...reverseResultNodes(state.reverse.data));
  }
  reverseResultHost.replaceChildren(...nodes.filter(Boolean));
}

function renderReverse() {
  reverseResultHost = el("div", { class: "stack" });
  paint("reverse", [el("div", { class: "split" }, [reverseFormCard(), reverseResultHost])]);
  renderReversePreview();
  renderReverseResults();
}
/* -- 08.4 配置 ------------------------------------------------------------- */

/** 当前生效值：有草稿取草稿，否则取后端快照值。 */
function draftValue(field) {
  const path = String(field.path);
  return state.cfg.drafts.has(path) ? state.cfg.drafts.get(path) : field.value;
}

/** 过滤匹配：label / path / desc 任一命中（query 需已转小写）。 */
function cfgMatches(field, query) {
  if (!query) return true;
  const f = field || {};
  return [f.label, f.path, f.desc].some(
    (v) => String(v === null || v === undefined ? "" : v).toLowerCase().includes(query),
  );
}

/** 某个分组（含其所有后代）里命中过滤的字段数。 */
function cfgMatchCount(navKey, query) {
  const prefix = navKey + "/";
  let hits = 0;
  for (const item of state.cfg.fields) {
    if (item.navKey !== navKey && !item.navKey.startsWith(prefix)) continue;
    if (cfgMatches(item.field, query)) hits += 1;
  }
  return hits;
}

/** 只有所有祖先都展开时，导航项才可见。 */
function cfgNavVisible(item) {
  const parts = item.key.split("/");
  for (let i = 1; i < parts.length; i += 1) {
    if (!state.cfg.expanded.has(parts.slice(0, i).join("/"))) return false;
  }
  return true;
}

function toList(value) {
  if (Array.isArray(value)) return value.map((v) => String(v)).filter((v) => v.trim() !== "");
  if (value === null || value === undefined || value === "") return [];
  return String(value).split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
}

function defaultText(field) {
  const value = field.default;
  if (value === null || value === undefined) return "无";
  if (typeof value === "boolean") return value ? "启用" : "关闭";
  if (Array.isArray(value)) return value.length ? value.map(String).join("、") : "空列表";
  const s = String(value);
  return s.trim() === "" ? "空" : s;
}

function clampNum(field, raw) {
  let n = Number(raw);
  if (!Number.isFinite(n)) return null;
  if (String(field.kind) === "int") n = Math.round(n);
  const min = Number(field.min);
  const max = Number(field.max);
  if (Number.isFinite(min)) n = Math.max(min, n);
  if (Number.isFinite(max)) n = Math.min(max, n);
  return n;
}

/** 写入草稿：与后端值一致则移除草稿，顺带清掉该项的拒绝态。 */
function setDraft(path, value, field, box) {
  if (deepEqual(value, field.value)) state.cfg.drafts.delete(path);
  else state.cfg.drafts.set(path, value);
  if (state.cfg.rejected.delete(path) && box) {
    box.classList.remove("is-rejected");
    const stale = box.querySelector(".field-error");
    if (stale) stale.remove();
  }
  if (box) box.classList.toggle("is-dirty", state.cfg.drafts.has(path));
  updateConfigDirty();
}

/* -- 08.4.1 各 kind 的控件 -------------------------------------------------- */

function boolControl(field, box) {
  const path = String(field.path);
  return liveSwitch("已启用", "已关闭", Boolean(draftValue(field)), (value) => {
    setDraft(path, value, field, box);
  }, plain(field.label, path));
}

function numberField(field, box, onSync) {
  const path = String(field.path);
  const isInt = String(field.kind) === "int";
  const step = Number.isFinite(Number(field.step)) ? Number(field.step) : (isInt ? 1 : 0.1);
  const input = el("input", {
    type: "number",
    step: String(step),
    "aria-label": plain(field.label, path),
    oninput: (ev) => {
      const n = Number(ev.target.value);
      if (Number.isFinite(n)) {
        setDraft(path, isInt ? Math.round(n) : n, field, box);
        if (onSync) onSync(n);
      }
    },
    onchange: (ev) => {
      const n = clampNum(field, ev.target.value);
      if (n === null) {
        const back = Number(field.value);
        ev.target.value = Number.isFinite(back) ? String(back) : "";
        return;
      }
      ev.target.value = String(n);
      setDraft(path, n, field, box);
      if (onSync) onSync(n);
    },
  });
  if (Number.isFinite(Number(field.min))) input.min = String(field.min);
  if (Number.isFinite(Number(field.max))) input.max = String(field.max);
  const current = Number(draftValue(field));
  input.value = Number.isFinite(current) ? String(current) : "";
  return input;
}

function numberControl(field, box) {
  const path = String(field.path);
  const min = Number(field.min);
  const max = Number(field.max);
  if (!(Number.isFinite(min) && Number.isFinite(max) && max > min)) return numberField(field, box, null);

  const isInt = String(field.kind) === "int";
  const step = Number.isFinite(Number(field.step)) ? Number(field.step) : (isInt ? 1 : (max - min) / 100);
  const range = el("input", {
    type: "range",
    min: String(min),
    max: String(max),
    step: String(step),
    "aria-label": plain(field.label, path) + "（滑块）",
  });
  const number = numberField(field, box, (value) => { range.value = String(value); });
  const current = Number(draftValue(field));
  range.value = String(Number.isFinite(current) ? current : min);
  range.addEventListener("input", (ev) => {
    const n = clampNum(field, ev.target.value);
    if (n === null) return;
    number.value = String(n);
    setDraft(path, n, field, box);
  });
  return el("div", { class: "range-row" }, [range, number]);
}

function textControl(field, box) {
  const path = String(field.path);
  const input = el("input", {
    type: "text",
    "aria-label": plain(field.label, path),
    placeholder: "留空表示使用默认值",
    oninput: (ev) => setDraft(path, ev.target.value, field, box),
  });
  input.value = String(plain(draftValue(field), ""));
  return input;
}

function passwordControl(field, box) {
  const path = String(field.path);
  const revealed = state.cfg.revealed.has(path);
  const input = el("input", {
    type: revealed ? "text" : "password",
    autocomplete: "off",
    spellcheck: "false",
    "aria-label": plain(field.label, path),
    placeholder: "凭据仅在本页遮蔽显示",
    oninput: (ev) => setDraft(path, ev.target.value, field, box),
  });
  input.value = String(plain(draftValue(field), ""));

  const btn = iconButton(revealed ? "eye-off" : "eye", revealed ? "隐藏凭据" : "显示凭据", () => {
    const shown = state.cfg.revealed.has(path);
    if (shown) state.cfg.revealed.delete(path);
    else state.cfg.revealed.add(path);
    const next = !shown;
    input.type = next ? "text" : "password";
    const label = next ? "隐藏凭据" : "显示凭据";
    btn.setAttribute("aria-label", label);
    btn.setAttribute("title", label);
    btn.replaceChildren(icon(next ? "eye-off" : "eye"));
  }, { className: "sm" });

  return el("div", { class: "pw-wrap" }, [input, btn]);
}

function textareaControl(field, box) {
  const path = String(field.path);
  const area = el("textarea", {
    rows: "5",
    "aria-label": plain(field.label, path),
    oninput: (ev) => setDraft(path, ev.target.value, field, box),
  });
  area.value = String(plain(draftValue(field), ""));
  return area;
}

function selectControl(field, box) {
  const path = String(field.path);
  const options = Array.isArray(field.options) ? field.options : [];
  const select = el("select", {
    "aria-label": plain(field.label, path),
    onchange: (ev) => setDraft(path, ev.target.value, field, box),
  });
  const current = String(plain(draftValue(field), ""));
  let matched = false;
  for (const opt of options) {
    if (!opt || opt.value === undefined) continue;
    const value = String(opt.value);
    const node = el("option", { value, text: plain(opt.label, value) });
    if (value === current) { node.selected = true; matched = true; }
    select.appendChild(node);
  }
  if (!matched) {
    const ghost = el("option", {
      value: current,
      text: current === "" ? "（未设置）" : current + "（当前值不在选项内）",
    });
    ghost.selected = true;
    select.insertBefore(ghost, select.firstChild);
  }
  return el("div", { class: "select-wrap" }, [select, icon("caret", "sm")]);
}

function listControl(field, box) {
  const path = String(field.path);
  const chips = el("div", { class: "chips" });
  const area = el("textarea", {
    rows: "4",
    placeholder: "每行一条",
    "aria-label": plain(field.label, path) + "（每行一条）",
  });
  const parse = (text) => String(text).split(/\r?\n/).map((s) => s.trim()).filter(Boolean);

  const paintChips = (items) => {
    if (!items.length) {
      chips.replaceChildren(el("span", { class: "field-hint", text: "（空列表）" }));
      return;
    }
    chips.replaceChildren(...items.map((value, index) => el("span", { class: "chip value" }, [
      el("span", { text: value }),
      el("button", {
        class: "x",
        type: "button",
        "aria-label": "删除第 " + (index + 1) + " 条",
        onclick: () => {
          const next = parse(area.value);
          next.splice(index, 1);
          area.value = next.join("\n");
          setDraft(path, next, field, box);
          paintChips(next);
        },
      }, icon("close")),
    ])));
  };

  const initial = toList(draftValue(field));
  area.value = initial.join("\n");
  paintChips(initial);
  area.addEventListener("input", () => {
    const next = parse(area.value);
    setDraft(path, next, field, box);
    paintChips(next);
  });
  return el("div", { class: "stack tight" }, [area, chips]);
}

function cfgControl(field, box) {
  const kind = String(field.kind || "text");
  if (kind === "bool") return boolControl(field, box);
  if (kind === "int" || kind === "float") return numberControl(field, box);
  if (kind === "password") return passwordControl(field, box);
  if (kind === "textarea") return textareaControl(field, box);
  if (kind === "select") return selectControl(field, box);
  if (kind === "list") return listControl(field, box);
  return textControl(field, box);
}
/* -- 08.4.2 字段盒 / 分组 / 导航 -------------------------------------------- */

function cfgFieldBox(field, query) {
  const path = String(field.path);
  const kind = String(field.kind || "text");
  const wide = kind === "textarea" || kind === "list";
  const classes = ["field", "cfg-field"];
  if (wide) classes.push("wide");
  if (state.cfg.drafts.has(path)) classes.push("is-dirty");
  if (state.cfg.rejected.has(path)) classes.push("is-rejected");
  const box = el("div", { class: classes.join(" ") });

  const labelRow = el("div", { class: "field-label" });
  labelRow.appendChild(el("span", { class: "dirty-dot", "aria-hidden": "true" }));
  labelRow.appendChild(fillHighlight(el("span"), plain(field.label, path), query));
  labelRow.appendChild(fillHighlight(el("span", { class: "field-path" }), path, query));
  box.appendChild(labelRow);

  box.appendChild(cfgControl(field, box));

  if (field.desc) box.appendChild(fillHighlight(el("span", { class: "field-hint" }), field.desc, query));
  if (kind !== "password") {
    box.appendChild(el("span", { class: "field-hint", text: "默认：" + defaultText(field) }));
  }
  const reason = state.cfg.rejected.get(path);
  if (reason) box.appendChild(el("span", { class: "field-error", text: "已拒绝：" + reason }));
  return box;
}

function cfgFieldsNode(group, query) {
  const fields = (Array.isArray(group.fields) ? group.fields : [])
    .filter((f) => f && typeof f === "object" && f.path && cfgMatches(f, query));
  if (!fields.length) return null;
  return el("div", { class: "cfg-fields" }, fields.map((f) => cfgFieldBox(f, query)));
}

/** 子分组递归渲染；整棵子树没有命中项时返回 null（配合过滤自动收起）。 */
function cfgSectionNode(group, depth, query) {
  if (!group || typeof group !== "object") return null;
  const fieldsNode = cfgFieldsNode(group, query);
  const children = (Array.isArray(group.groups) ? group.groups : [])
    .map((child) => cfgSectionNode(child, Math.min(depth + 1, 2), query))
    .filter(Boolean);
  if (!fieldsNode && !children.length) return null;

  const head = el("div", { class: "cfg-section-head" }, [
    iconOr(group.icon, "folder"),
    el("div", { class: "head-text" }, [
      fillHighlight(el("h3"), plain(group.label, group.key), query),
      group.desc ? el("p", { text: group.desc }) : null,
    ]),
  ]);
  const cls = "cfg-section" + (depth > 0 ? " depth-" + depth : "");
  return el("section", { class: cls }, [head, fieldsNode, ...children]);
}

function cfgNavItem(item, query) {
  const expanded = state.cfg.expanded.has(item.key);
  const active = state.cfg.current === item.key;
  const hits = query ? cfgMatchCount(item.key, query) : 0;

  const kids = [];
  kids.push(item.hasChildren
    ? icon("chev-right", "twist" + (expanded ? " is-open" : ""))
    : iconOr(item.group && item.group.icon, "folder"));
  kids.push(fillHighlight(el("span", { class: "label" }), item.label, query));
  if (query) kids.push(el("span", { class: "n", text: String(hits) }));

  const btn = el("button", {
    class: "cfg-nav-item" + (query && !hits ? " is-dim" : ""),
    type: "button",
    "aria-current": active ? "true" : "false",
    title: item.trail.join(" / "),
    onclick: () => {
      if (active && item.hasChildren) {
        if (expanded) state.cfg.expanded.delete(item.key);
        else state.cfg.expanded.add(item.key);
      } else {
        state.cfg.current = item.key;
        expandAncestors(item.key);
        if (item.hasChildren) state.cfg.expanded.add(item.key);
      }
      pushPrefs();
      renderConfigBody();
    },
  }, kids);
  btn.style.setProperty("--depth", String(item.depth));
  return btn;
}

function cfgPanelCard(query) {
  const current = state.cfg.nav.find((item) => item.key === state.cfg.current) || null;
  if (!current) {
    return card({
      eyebrow: "GROUP",
      icon: "sliders",
      title: "没有可编辑的配置",
      body: [emptyBox("folder", "后端没有返回配置分组", "确认插件配置 schema 是否已加载")],
    });
  }
  const group = current.group || {};
  const fieldsNode = cfgFieldsNode(group, query);
  const children = (Array.isArray(group.groups) ? group.groups : [])
    .map((child) => cfgSectionNode(child, 1, query))
    .filter(Boolean);

  const body = [el("div", { class: "cfg-crumb", text: current.trail.join(" / ") })];
  if (!fieldsNode && !children.length) {
    body.push(query
      ? emptyBox("filter", "该分组没有命中项", "换个关键词，或清空过滤框")
      : emptyBox("folder", "该分组没有直接配置项", "请在左侧选择它的子分组"));
  } else {
    if (fieldsNode) body.push(fieldsNode);
    body.push(...children);
  }

  return card({
    eyebrow: "GROUP",
    icon: KNOWN_ICONS.has(group.icon) ? group.icon : "sliders",
    title: current.label,
    desc: group.desc || null,
    actions: [textButton("refresh", "重载", () => loadConfig(true), { className: "sm ghost", title: "丢弃本地未保存改动并重新拉取配置" })],
    body,
  });
}

/* -- 08.4.3 保存 / 放弃 / 局部刷新 ------------------------------------------ */

function updateConfigDirty() {
  const count = state.cfg.drafts.size;
  if (cfgDirtyNote) {
    cfgDirtyNote.className = "pill " + (count ? "warn" : "mute");
    cfgDirtyNote.replaceChildren(
      icon(count ? "alert" : "check"),
      el("span", { text: count ? count + " 项待保存" : "没有未保存改动" }),
    );
  }
  if (cfgSaveBtn) cfgSaveBtn.disabled = count === 0 || state.cfg.saving;
  if (cfgDiscardBtn) cfgDiscardBtn.disabled = count === 0 || state.cfg.saving;
  renderTabbar();
}

async function saveConfig(btn) {
  if (state.cfg.saving || !state.cfg.drafts.size) return;
  const changes = {};
  for (const [path, value] of state.cfg.drafts) changes[path] = value;
  state.cfg.saving = true;
  updateConfigDirty();
  try {
    await withBusy(btn, async () => {
      const body = await apiPost("config", { changes });
      const applied = Array.isArray(body.applied) ? body.applied : [];
      const rejected = Array.isArray(body.rejected) ? body.rejected : [];
      for (const path of applied) state.cfg.drafts.delete(String(path));
      state.cfg.rejected = new Map(rejected
        .filter((item) => item && item.path)
        .map((item) => [String(item.path), plain(item.reason, "后端拒绝了该取值")]));
      if (Array.isArray(body.groups)) adoptConfig(body.groups);
      /* 后端快照已经等于草稿值的项不再算作待保存。 */
      const snapshot = new Map(state.cfg.fields.map((item) => [String(item.field.path), item.field.value]));
      for (const [path, value] of Array.from(state.cfg.drafts)) {
        if (snapshot.has(path) && deepEqual(snapshot.get(path), value)) state.cfg.drafts.delete(path);
      }
      if (rejected.length) toast("已应用 " + applied.length + " 项，" + rejected.length + " 项被拒绝", "warn", 5600);
      else toast("已保存 " + applied.length + " 项配置", "ok");
    });
  } catch (error) {
    toast("保存失败：" + errText(error), "err", 5600);
  } finally {
    state.cfg.saving = false;
    renderConfig();
    renderStatus();
  }
}

function discardConfig() {
  if (!state.cfg.drafts.size) return;
  const count = state.cfg.drafts.size;
  state.cfg.drafts.clear();
  state.cfg.rejected.clear();
  renderConfig();
  toast("已放弃 " + count + " 项未保存改动", "info");
}

function renderConfigBody() {
  if (!cfgNavHost || !cfgPanelHost) return;
  const query = state.cfg.query.trim().toLowerCase();
  cfgNavHost.replaceChildren(...state.cfg.nav.filter(cfgNavVisible).map((item) => cfgNavItem(item, query)));
  cfgPanelHost.replaceChildren(cfgPanelCard(query));
}

function resetConfigHosts() {
  cfgNavHost = null;
  cfgPanelHost = null;
  cfgDirtyNote = null;
  cfgSaveBtn = null;
  cfgDiscardBtn = null;
}

function renderConfig() {
  if (state.cfg.error) {
    resetConfigHosts();
    paint("config", [card({
      eyebrow: "CONFIG",
      icon: "alert",
      title: "配置加载失败",
      desc: "老版本 AstrBot 可能没有该接口",
      body: [inlineError(state.cfg.error, () => loadConfig(true))],
    })]);
    return;
  }
  if (!state.cfg.loaded) {
    resetConfigHosts();
    paint("config", [card({
      eyebrow: "CONFIG",
      icon: "sliders",
      title: "正在读取配置",
      body: [skeletonLines(3), el("div", { class: "sk block" })],
    })]);
    return;
  }

  cfgDirtyNote = el("span", { class: "pill mute" });
  cfgSaveBtn = textButton("save", "保存", () => saveConfig(cfgSaveBtn), { className: "primary" });
  cfgDiscardBtn = textButton("undo", "放弃", () => discardConfig(), { className: "ghost" });

  const toolbar = card({
    eyebrow: "CONFIG",
    icon: "sliders",
    title: "配置项",
    desc: "左侧选择分组，右侧编辑；改动会先攒成草稿，点「保存」才写回后端",
    className: "cfg-toolbar",
    body: [el("div", { class: "row" }, [
      filterBar("过滤：名称 / 路径 / 说明", state.cfg.query, (value) => {
        state.cfg.query = value;
        renderConfigBody();
      }),
      cfgDirtyNote,
      el("span", { class: "spacer" }),
      cfgDiscardBtn,
      cfgSaveBtn,
    ])],
  });

  cfgNavHost = el("nav", { class: "card cfg-nav", "aria-label": "配置分组导航" });
  cfgPanelHost = el("div", { class: "stack" });
  paint("config", [toolbar, el("div", { class: "cfg" }, [cfgNavHost, cfgPanelHost])]);
  updateConfigDirty();
  renderConfigBody();
}
/* -- 08.5 指令表 ----------------------------------------------------------- */

function cmdMatches(command, query) {
  if (!query) return true;
  const c = command || {};
  const bag = [c.cmd, c.usage, c.desc].concat(Array.isArray(c.tags) ? c.tags : []);
  return bag.some((v) => String(v === null || v === undefined ? "" : v).toLowerCase().includes(query));
}

async function copyCommand(command, btn) {
  const text = String(plain(command && command.cmd, ""));
  if (text === "—" || text === "") { toast("这条指令没有可复制的内容", "warn"); return; }
  const done = await copyText(text);
  toast(done ? "已复制：" + text : "复制失败，请手动选择文本", done ? "ok" : "warn");
  if (btn) {
    btn.replaceChildren(icon(done ? "check" : "close"));
    setTimeout(() => btn.replaceChildren(icon("copy")), 1200);
  }
}

function cmdRow(command, query) {
  const main = el("div", { class: "cmd-main" }, [
    fillHighlight(el("code"), plain(command.cmd), query),
  ]);
  if (command.desc) main.appendChild(fillHighlight(el("span", { class: "cmd-desc" }), command.desc, query));

  const usage = String(command.usage === null || command.usage === undefined ? "" : command.usage).trim();
  if (usage && usage !== String(command.cmd || "").trim()) {
    main.appendChild(fillHighlight(el("span", { class: "cmd-usage" }), "用法：" + usage, query));
  }
  const tags = (Array.isArray(command.tags) ? command.tags : []).map(String).filter(Boolean);
  if (tags.length) {
    main.appendChild(el("div", { class: "row tight" }, tags.map((tag) => pill(tag, "info", "tag"))));
  }

  const copyBtn = iconButton("copy", "复制指令", (ev) => copyCommand(command, ev.currentTarget), { className: "sm" });
  return el("div", { class: "cmd-row" }, [main, copyBtn]);
}

function cmdGroupCard(group, query) {
  const commands = (Array.isArray(group.commands) ? group.commands : [])
    .filter((item) => item && typeof item === "object" && cmdMatches(item, query));
  if (!commands.length) return null;
  return card({
    eyebrow: "COMMANDS",
    icon: KNOWN_ICONS.has(group.icon) ? group.icon : "terminal",
    title: plain(group.label, group.key),
    desc: commands.length + " 条指令",
    body: [el("div", { class: "cmd-list" }, commands.map((item) => cmdRow(item, query)))],
  });
}

function renderCommandsBody() {
  if (!cmdListHost) return;
  const query = state.cmd.query.trim().toLowerCase();
  const cards = state.cmd.groups.map((group) => cmdGroupCard(group || {}, query)).filter(Boolean);
  if (!cards.length) {
    cmdListHost.replaceChildren(card({
      eyebrow: "COMMANDS",
      icon: "terminal",
      title: "没有匹配的指令",
      body: [query
        ? emptyBox("filter", "没有命中任何指令", "换个关键词，或清空过滤框")
        : emptyBox("terminal", "后端没有返回指令", "确认插件已正确注册指令处理器")],
    }));
    return;
  }
  cmdListHost.replaceChildren(...cards);
}

function renderCommands() {
  if (state.cmd.error) {
    cmdListHost = null;
    paint("commands", [card({
      eyebrow: "COMMANDS",
      icon: "alert",
      title: "指令表加载失败",
      body: [inlineError(state.cmd.error, () => loadCommands(true))],
    })]);
    return;
  }
  if (!state.cmd.loaded) {
    cmdListHost = null;
    paint("commands", [card({
      eyebrow: "COMMANDS",
      icon: "terminal",
      title: "正在读取指令表",
      body: [skeletonLines(5)],
    })]);
    return;
  }

  const toolbar = card({
    eyebrow: "COMMANDS",
    icon: "terminal",
    title: "全部聊天指令",
    desc: "点右侧图标即可复制指令原文到剪贴板",
    body: [el("div", { class: "row" }, [
      filterBar("过滤：指令 / 说明 / 别名 / 标签", state.cmd.query, (value) => {
        state.cmd.query = value;
        renderCommandsBody();
      }),
      pill("共 " + state.cmd.total + " 条", "info", "layers"),
      el("span", { class: "spacer" }),
      textButton("refresh", "重载", (ev) => loadCommands(true), { className: "sm ghost" }),
    ])],
  });

  cmdListHost = el("div", { class: "stack" });
  paint("commands", [toolbar, cmdListHost]);
  renderCommandsBody();
}

/* -- 08.6 关于 ------------------------------------------------------------- */

function aboutInfoCard() {
  if (state.metaError) {
    return card({
      eyebrow: "PLUGIN",
      icon: "alert",
      title: "插件信息不可用",
      body: [inlineError(state.metaError, () => refreshAll(null))],
    });
  }
  const meta = state.meta || {};
  const plugin = meta.plugin || {};
  const repo = safeUrl(plugin.repo);
  const repoNode = repo
    ? el("a", { href: repo, target: "_blank", rel: "noopener noreferrer", text: repo })
    : el("span", { text: "—" });

  return card({
    eyebrow: "PLUGIN",
    icon: "info",
    title: plain(plugin.display_name, "爱丽丝的图片助手"),
    desc: plain(plugin.tagline, "找图 · 溯源 · Pixiv · VLM 复核"),
    body: [kvTable([
      ["插件标识", codeChip(plain(plugin.name, "astrbot_plugin_alice_image_assistant"))],
      ["版本", codeChip(plain(plugin.version))],
      ["作者", plain(plugin.author)],
      ["AstrBot 版本要求", codeChip(plain(plugin.astrbot_version))],
      ["Web 后端", plain(meta.backend)],
      ["仓库", repoNode],
    ])],
  });
}

function themeCardNode(item) {
  const accent = /^#[0-9a-fA-F]{3,8}$/.test(String(item.accent || "")) ? String(item.accent) : "#888888";
  const band = el("span", { class: "band", "aria-hidden": "true" });
  band.style.background = "linear-gradient(135deg, " + accent + ", color-mix(in srgb, " + accent + " 25%, transparent))";
  return el("button", {
    class: "theme-card",
    type: "button",
    "aria-current": state.theme === item.key ? "true" : "false",
    title: "切换到「" + plain(item.label, item.key) + "」主题",
    onclick: () => {
      applyTheme(item.key);
      toast("已切换主题：" + plain(item.label, item.key), "ok", 2400);
    },
  }, [
    band,
    el("span", { class: "name" }, [el("span", { text: plain(item.label, item.key) }), icon("check", "tick")]),
    el("span", { class: "hex", text: accent + " · " + plain(item.key) }),
  ]);
}

function aboutThemeCard() {
  const list = state.themes.length ? state.themes : FALLBACK_THEMES;
  return card({
    eyebrow: "THEME",
    icon: "palette",
    title: "界面主题",
    desc: "共 " + list.length + " 套，点击色卡即时切换，偏好会写回后端",
    body: [el("div", { class: "theme-grid" }, list.map(themeCardNode))],
  });
}

const SECURITY_NOTES = [
  "本页接口通过 register_web_api 注册，鉴权完全依赖 AstrBot Dashboard 的登录态，插件层不做额外校验。请不要把 Dashboard 直接暴露在公网。",
  "配置页里的 Pixiv refresh token、SauceNAO、SerpApi Key 都属于敏感凭据。页面只做遮蔽显示，点「显示」即可明文查看，请确认当前浏览器环境可信。",
  "找图与溯源会向第三方站点发起出网请求，并可能把图片 URL 交给 VLM 复核。涉密或私密图片请不要在此上传。",
];

function aboutSecurityCard() {
  return card({
    eyebrow: "SECURITY",
    icon: "shield",
    title: "安全须知",
    desc: "使用本控制台前请确认以下三点",
    className: "accent",
    body: [el("ul", { class: "notice" }, SECURITY_NOTES.map((text, index) => el("li", {}, [
      el("span", { class: "num", "aria-hidden": "true", text: String(index + 1) }),
      el("p", { text }),
    ])))],
  });
}

function renderAbout() {
  paint("about", [
    el("div", { class: "grid cols-2" }, [aboutInfoCard(), aboutSecurityCard()]),
    aboutThemeCard(),
  ]);
}
/* ---- 09 外壳：主题选择器 / tabbar / 状态栏 / 灯箱 / 路由 ------------------ */

const HEX_RE = /^#[0-9a-fA-F]{3,8}$/;

/** 只信任形如 #rgb…#rrggbbaa 的色值，其余一律回退，避免把后端字符串塞进 style。 */
function safeAccent(raw, fallback = "#888888") {
  const value = String(raw === null || raw === undefined ? "" : raw).trim();
  return HEX_RE.test(value) ? value : fallback;
}

function themeList() {
  return state.themes.length ? state.themes : FALLBACK_THEMES;
}

function renderThemeControl() {
  const list = themeList();
  const current = themeMeta(state.theme);

  const label = $("#theme-label");
  if (label) label.textContent = plain(current && current.label, state.theme);

  const swatch = $("#theme-swatch");
  if (swatch) {
    swatch.replaceChildren(...list.map((item) => {
      const bar = el("i");
      bar.style.background = safeAccent(item.accent);
      bar.style.opacity = item.key === state.theme ? "1" : "0.42";
      return bar;
    }));
  }

  const menu = $("#theme-menu");
  if (menu) {
    menu.replaceChildren(...list.map((item) => {
      const dot = el("span", { class: "dot", "aria-hidden": "true" });
      dot.style.background = safeAccent(item.accent);
      return el("button", {
        class: "theme-option",
        type: "button",
        role: "menuitemradio",
        "aria-checked": item.key === state.theme ? "true" : "false",
        onclick: () => {
          applyTheme(item.key);
          closeThemeMenu(true);
        },
      }, [
        dot,
        el("span", { text: plain(item.label, item.key) }),
        el("span", { class: "key", text: String(item.key) }),
        icon("check", "tick"),
      ]);
    }));
  }
}

function tabBadge(view) {
  if (view.key === "config") {
    const n = state.cfg.drafts.size;
    return n ? el("span", { class: "tab-badge warn", text: String(n) }) : null;
  }
  if (view.key === "commands") {
    const counts = (state.meta && state.meta.counts) || {};
    const total = state.cmd.total || Number(counts.commands) || 0;
    return total ? el("span", { class: "tab-badge", text: String(total) }) : null;
  }
  return null;
}

function focusTab(index) {
  const keys = VIEW_KEYS;
  const next = keys[(index + keys.length) % keys.length];
  goto(next);
  /* tabbar 每次切换都会重建，等一个宏任务再把焦点放到新按钮上。 */
  setTimeout(() => {
    const btn = $("#tab-" + next);
    if (btn) btn.focus();
  }, 0);
}

function renderTabbar() {
  const host = $("#tabbar");
  if (!host) return;
  host.replaceChildren(...VIEWS.map((view, index) => {
    const selected = state.view === view.key;
    return el("button", {
      class: "tab",
      id: "tab-" + view.key,
      type: "button",
      role: "tab",
      "aria-selected": selected ? "true" : "false",
      "aria-controls": "view-" + view.key,
      tabindex: selected ? "0" : "-1",
      title: view.label + " · " + view.tip,
      onclick: () => goto(view.key),
      onkeydown: (ev) => {
        if (ev.key === "ArrowRight") { ev.preventDefault(); focusTab(index + 1); }
        else if (ev.key === "ArrowLeft") { ev.preventDefault(); focusTab(index - 1); }
        else if (ev.key === "Home") { ev.preventDefault(); focusTab(0); }
        else if (ev.key === "End") { ev.preventDefault(); focusTab(VIEW_KEYS.length - 1); }
      },
    }, [
      icon(view.icon),
      el("span", { class: "tab-text", text: view.label }),
      tabBadge(view),
    ]);
  }));
}

function statusNum(value, cls) {
  return el("b", { class: cls || null, text: fmtNum(value) });
}

function renderStatus() {
  const left = $("#status-left");
  const right = $("#status-right");

  if (left) {
    if (state.metaError) {
      left.replaceChildren(el("b", { class: "err", text: "后端不可用" }), document.createTextNode("：" + state.metaError));
    } else {
      const meta = state.meta || {};
      const plugin = meta.plugin || {};
      const counts = meta.counts || {};
      const acc = healthLevelCounts();
      const total = acc.ok + acc.warn + acc.error + acc.info;
      const healthText = state.healthError
        ? el("b", { class: "err", text: "检查失败" })
        : (!total
          ? el("b", { text: "—" })
          : (acc.error
            ? el("b", { class: "err", text: "异常 " + acc.error })
            : (acc.warn ? el("b", { class: "warn", text: "注意 " + acc.warn }) : el("b", { class: "ok", text: "正常" }))));
      left.replaceChildren();
      append(left, [
        plain(plugin.display_name, "爱丽丝的图片助手"),
        " · 指令 ", statusNum(counts.commands),
        " · 配置项 ", statusNum(counts.config_fields),
        " · 检索源 ", statusNum(counts.sources),
        " · 溯源策略 ", statusNum(counts.strategies),
        " · 主题 ", el("b", { text: String(themeList().length) }),
        " · 健康 ", healthText,
      ]);
      if (state.cfg.drafts.size) {
        append(left, [" · ", el("b", { class: "warn", text: state.cfg.drafts.size + " 项待保存" })]);
      }
    }
  }

  if (right) {
    const meta = state.meta || {};
    const plugin = meta.plugin || {};
    const parts = [
      plain(VIEW_LABEL.get(state.view), state.view),
      plain(meta.backend, "unknown"),
      "v" + plain(plugin.version, "?"),
    ];
    right.textContent = parts.join(" · ");
  }
}

/* -- 09.1 灯箱 ------------------------------------------------------------- */

let lightboxReturn = null;

function openLightbox(src, caption) {
  const url = safeUrl(src);
  if (!url) { toast("这张图片没有可预览的地址", "warn"); return; }
  const box = $("#lightbox");
  const img = $("#lightbox-img");
  const cap = $("#lightbox-cap");
  if (!box || !img) return;
  lightboxReturn = document.activeElement;
  img.src = url;
  img.alt = plain(caption, "预览图");
  if (cap) cap.textContent = plain(caption, "");
  box.hidden = false;
  const close = $("#lightbox-close");
  if (close) close.focus();
}

function closeLightbox() {
  const box = $("#lightbox");
  const img = $("#lightbox-img");
  if (!box || box.hidden) return;
  box.hidden = true;
  if (img) img.removeAttribute("src");
  if (lightboxReturn && typeof lightboxReturn.focus === "function") lightboxReturn.focus();
  lightboxReturn = null;
}

/* -- 09.2 主题菜单 --------------------------------------------------------- */

function themeMenuOpen() {
  const menu = $("#theme-menu");
  return Boolean(menu && !menu.hidden);
}

function openThemeMenu() {
  const menu = $("#theme-menu");
  const btn = $("#btn-theme");
  if (!menu || !btn) return;
  menu.hidden = false;
  btn.setAttribute("aria-expanded", "true");
  const first = menu.querySelector('.theme-option[aria-checked="true"]') || menu.querySelector(".theme-option");
  if (first) first.focus();
}

function closeThemeMenu(refocus) {
  const menu = $("#theme-menu");
  const btn = $("#btn-theme");
  if (!menu || !btn) return;
  menu.hidden = true;
  btn.setAttribute("aria-expanded", "false");
  if (refocus) btn.focus();
}

/* -- 09.3 视图切换与 hash 路由 --------------------------------------------- */

function setView(view) {
  const key = VIEW_KEYS.includes(view) ? view : DEFAULT_VIEW;
  state.view = key;
  for (const section of $$(".view")) {
    section.hidden = section.dataset.view !== key;
  }
  renderTabbar();
  renderStatus();
  renderView(key);
  ensureViewData(key);
  if (state.booted) pushPrefs();
}

function goto(view) {
  const key = VIEW_KEYS.includes(view) ? view : DEFAULT_VIEW;
  if (location.hash === "#/" + key) { setView(key); return; }
  location.hash = "#/" + key;
}

function currentHashView() {
  const raw = String(location.hash || "").replace(/^#\/?/, "").split(/[?&]/)[0];
  return VIEW_KEYS.includes(raw) ? raw : null;
}

function syncFromHash() {
  const key = currentHashView();
  if (!key) {
    location.replace("#/" + DEFAULT_VIEW);
    return;
  }
  if (key !== state.view || !state.booted) setView(key);
}

/* -- 09.4 全局刷新与外壳事件 ----------------------------------------------- */

async function refreshAll(btn) {
  await withBusy(btn, async () => {
    await Promise.allSettled([loadMeta(), loadHealth()]);
    if (state.view === "config") await loadConfig(true);
    if (state.view === "commands") await loadCommands(true);
    renderView(state.view);
    renderStatus();
    if (state.metaError) toast("刷新失败：" + state.metaError, "err", 5200);
    else toast("已刷新后端数据", "ok");
  });
}

let resetTimer = null;

function disarmReset() {
  const btn = $("#btn-reset");
  const label = $("#reset-label");
  if (resetTimer) { clearTimeout(resetTimer); resetTimer = null; }
  if (btn) btn.classList.remove("is-armed");
  if (label) label.textContent = "重置偏好";
}

async function resetPrefs() {
  disarmReset();
  applyTheme(DEFAULT_THEME, { persist: false });
  applyCompact(false, { persist: false });
  state.cfg.query = "";
  state.cmd.query = "";
  try {
    await apiPost("state", { theme: DEFAULT_THEME, tab: DEFAULT_VIEW, compact: false, config_group: null });
  } catch (_) {
    /* 后端没有 state 端点也不影响本地已经生效的重置 */
  }
  if (state.cfg.loaded) renderConfig();
  if (state.cmd.loaded) renderCommands();
  toast("界面偏好已重置", "ok");
  goto(DEFAULT_VIEW);
}

function armReset() {
  const btn = $("#btn-reset");
  const label = $("#reset-label");
  if (btn) btn.classList.add("is-armed");
  if (label) label.textContent = "确认重置？";
  resetTimer = setTimeout(disarmReset, 4000);
}

function bindShell() {
  const compact = $("#btn-compact");
  if (compact) compact.addEventListener("click", () => applyCompact(!state.compact));

  const refresh = $("#btn-refresh");
  if (refresh) refresh.addEventListener("click", (ev) => refreshAll(ev.currentTarget));

  const reset = $("#btn-reset");
  if (reset) {
    reset.addEventListener("click", () => {
      if (reset.classList.contains("is-armed")) resetPrefs();
      else armReset();
    });
  }

  const themeBtn = $("#btn-theme");
  if (themeBtn) {
    themeBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (themeMenuOpen()) closeThemeMenu(false);
      else openThemeMenu();
    });
  }

  const closeBtn = $("#lightbox-close");
  if (closeBtn) closeBtn.addEventListener("click", closeLightbox);
  const box = $("#lightbox");
  if (box) {
    box.addEventListener("click", (ev) => { if (ev.target === box) closeLightbox(); });
  }

  document.addEventListener("click", (ev) => {
    if (!themeMenuOpen()) return;
    const picker = ev.target && ev.target.closest ? ev.target.closest(".theme-picker") : null;
    if (!picker) closeThemeMenu(false);
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if (themeMenuOpen()) { closeThemeMenu(true); return; }
    closeLightbox();
    disarmReset();
  });

  /* 溯源页支持直接 Ctrl+V 粘贴剪贴板里的图片。 */
  document.addEventListener("paste", (ev) => {
    if (state.view !== "reverse") return;
    const items = (ev.clipboardData && ev.clipboardData.items) || [];
    for (const item of items) {
      if (!item || item.kind !== "file" || !String(item.type || "").startsWith("image/")) continue;
      const file = item.getAsFile();
      if (file) { ev.preventDefault(); handleReverseFile(file); }
      return;
    }
  });

  window.addEventListener("hashchange", syncFromHash);
}
/* ---- 10 启动 ------------------------------------------------------------- */

function bootText(message) {
  const node = $("#boot-text");
  if (node) node.textContent = message;
}

function hideBoot() {
  const boot = $("#boot");
  const shell = $("#shell");
  if (shell) shell.classList.add("is-ready");
  if (!boot) return;
  boot.classList.add("is-gone");
  setTimeout(() => { boot.hidden = true; }, 320);
}

/** AstrBot 侧上下文变化（语言 / 主题等）时做一次轻量重绘。 */
function onBridgeContext(payload) {
  const ctx = payload && typeof payload === "object" ? payload : {};
  if (typeof ctx.locale === "string" && ctx.locale) state.locale = ctx.locale;
  renderThemeControl();
  renderStatus();
  renderView(state.view);
}

function subscribeContext() {
  if (!bridge) return;
  const sub = bridge.onContext ?? bridge.onContextChange;
  if (typeof sub !== "function") return;
  try {
    sub.call(bridge, onBridgeContext);
  } catch (_) {
    /* 老版本 bridge 没有上下文订阅，忽略即可 */
  }
}

async function boot() {
  bindShell();
  for (const section of $$(".view")) {
    section.id = "view-" + section.dataset.view;
  }

  bootText("正在连接 AstrBot…");
  if (!bridge) {
    toast(NO_BRIDGE, "err", 7000);
  } else if (typeof bridge.ready === "function") {
    try {
      const info = await bridge.ready();
      if (info && typeof info.locale === "string" && info.locale) state.locale = info.locale;
    } catch (error) {
      toast("bridge 初始化异常：" + errText(error), "warn", 5200);
    }
  }
  subscribeContext();

  bootText("读取界面偏好…");
  const prefTab = await loadPrefs();

  bootText("读取插件信息…");
  await Promise.allSettled([loadMeta(), loadHealth()]);

  const initial = currentHashView() || prefTab || DEFAULT_VIEW;
  state.booted = true;
  applyCompact(state.compact, { persist: false });
  renderThemeControl();
  history.replaceState(null, "", "#/" + initial);
  setView(initial);
  hideBoot();

  if (state.metaError) toast("后端不可用：" + state.metaError, "err", 6000);
}

boot();
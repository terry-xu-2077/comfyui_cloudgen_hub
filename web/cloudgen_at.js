/* 提示词富文本编辑器 + @ 菜单（参考 Terry H3 编辑器）
 *
 * 四个云端节点的 prompt 换成可视化编辑框：<Picture 1> / <Video 1> / <Audio 1> 显示成带缩略图的胶囊
 * （和 Terry H3 编辑器同一套标签，可互相复制）。输入 @（或 ＠）弹出已接入的参考素材，选中插入胶囊。
 * 原始 prompt 字符串仍保存在隐藏的 prompt 控件里，后端按平台换成对应写法：
 *   LibTV：<Picture N> → {{Node <参考图节点>}}
 *   小云雀：<Picture N> → 图片N、<Video N> → 视频N、<Audio N> → 音频N（按上传顺序）
 * ↑↓ 选择、Enter/Tab 插入、Esc 关闭，@ 后继续输入可筛选；右下角 </> 切换纯文本。
 */
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const TYPES = new Set(["CloudGen_XYQ_Image", "CloudGen_XYQ_Video", "CloudGen_LibTV_Image", "CloudGen_LibTV_Video",
  "CloudGen_Flow_Image", "CloudGen_Flow_Video"]);

/* ------------------------------------------------------------ 素材解析 */
function linkOf(graph, id) {
  if (id == null || !graph) return null;
  const ls = graph.links;
  return (ls?.get ? ls.get(id) : ls?.[id]) || null;
}
function upstream(node, input) {
  const link = linkOf(node.graph || app.graph, input?.link);
  if (!link) return null;
  const src = (node.graph || app.graph).getNodeById(link.origin_id);
  return src ? { node: src, slot: link.origin_slot || 0 } : null;
}
function viewUrl(v) {
  const file = typeof v === "object" && v ? (v.filename || v.name) : v;
  if (!file || /^(data:|blob:|https?:)/i.test(String(file))) return "";
  if (!/\.(png|jpe?g|webp|gif|bmp|tiff?)$/i.test(String(file))) return "";
  const q = new URLSearchParams({ filename: String(file).split(/[\\/]/).pop(), type: (typeof v === "object" && v?.type) || "input" });
  const sub = typeof v === "object" && v?.subfolder ? v.subfolder : String(file).includes("/") ? String(file).split("/").slice(0, -1).join("/") : "";
  if (sub) q.set("subfolder", sub);
  return api.apiURL(`/view?${q.toString()}`);
}
function fileWidget(node) {
  for (const w of node.widgets || []) {
    const url = viewUrl(w.value);
    if (url && (/^(image|file|filename)$/i.test(w.name) || url)) return { url, name: String(typeof w.value === "object" ? w.value.filename : w.value).split(/[\\/]/).pop() };
  }
  return null;
}
// 沿连线往上找图片来源：多路合并（CreateList / Batch 等）会展开成多项，单输入的处理节点向上取缩略图
function resolveImages(node, depth = 0) {
  if (!node || depth > 8) return [];
  // TerryXu lists expose the actual ordered assets behind their BUS socket.
  // Keep this optional so ordinary lists work without the TerryXu extension.
  if (typeof node.__terryBusListEntries === "function") {
    return node.__terryBusListEntries()
      .filter(entry => entry.type === "IMAGE" && entry.source && !entry.empty)
      .flatMap(entry => {
        const asset = entry.asset;
        if (asset?.path) return [{
          preview: viewUrl({ filename: asset.path.split("/").pop(),
            subfolder: asset.path.split("/").slice(0, -1).join("/"), type: asset.source || "input" }),
          name: asset.name || asset.path.split("/").pop(), title: asset.path,
        }];
        return resolveImages(entry.source.node, depth + 1);
      });
  }
  const own = node.imgs?.find?.((i) => i?.src)?.src;
  const fw = fileWidget(node);
  if (fw) return [{ preview: fw.url, name: fw.name, title: node.title }];
  // 输入类型可能是通配 "*"（如 创建列表 / CreateList 的自增输入），按连线实际传的类型判断
  const imgIns = (node.inputs || []).filter((i) => {
    if (i.link == null) return false;
    const l = linkOf(node.graph || app.graph, i.link);
    return [i.type, l?.type].some((t) => String(t || "").toUpperCase().includes("IMAGE"));
  });
  if (imgIns.length >= 2) {
    return imgIns.flatMap((i) => { const u = upstream(node, i); return u ? resolveImages(u.node, depth + 1) : []; });
  }
  if (imgIns.length === 1) {
    const u = upstream(node, imgIns[0]);
    const r = u ? resolveImages(u.node, depth + 1) : [];
    if (r.length === 1) return [{ ...r[0], preview: own || r[0].preview, title: node.title }];
    if (r.length) return r;
  }
  return [{ preview: own || "", name: node.title || node.type, title: node.title }];
}
function imagesFrom(node, inputName, role) {
  const inp = (node.inputs || []).find((i) => i.name === inputName);
  const u = inp ? upstream(node, inp) : null;
  if (!u) return [];
  return resolveImages(u.node).map((x) => ({ ...x, role }));
}
function widgetVal(node, name) { return node.widgets?.find((w) => w.name === name)?.value ?? ""; }
function lines(v, sep = /\r?\n/) { return String(v || "").split(sep).map((s) => s.trim().replace(/^"|"$/g, "")).filter(Boolean); }
function baseName(p) { return String(p).split(/[\\/]/).pop(); }

// 按后端上传顺序编号（顺序不能乱，否则 <Picture 2> 会指错图）
function assetsFor(node) {
  const t = node.comfyClass || node.type;
  const pics = [], vids = [], auds = [];
  let note = "";
  if (t === "CloudGen_XYQ_Image" || t === "CloudGen_Flow_Image") pics.push(...imagesFrom(node, "images", "参考图"));
  else if (t === "CloudGen_Flow_Video") {
    const ff = imagesFrom(node, "first_frame", "首帧");
    if (ff.length) pics.push(...ff, ...imagesFrom(node, "last_frame", "尾帧"));
    else pics.push(...imagesFrom(node, "ref_images", "参考图").slice(0, 3));
  }
  else if (t === "CloudGen_XYQ_Video") {
    pics.push(...imagesFrom(node, "first_frame", "首帧"), ...imagesFrom(node, "last_frame", "尾帧"), ...imagesFrom(node, "ref_images", "参考图"));
    for (const p of lines(widgetVal(node, "ref_video_path"))) vids.push({ name: baseName(p), title: p, role: "参考视频" });
    for (const p of lines(widgetVal(node, "audio_path"))) auds.push({ name: baseName(p), title: p, role: "参考音频" });
  } else if (t === "CloudGen_LibTV_Image") {
    pics.push(...imagesFrom(node, "images", "参考图"));
    for (const u of lines(widgetVal(node, "ref_urls"), /[,，\s]+/)) pics.push({ name: baseName(u), title: u, preview: u, role: "链接" });
  } else if (t === "CloudGen_LibTV_Video") {
    const ff = (node.inputs || []).find((i) => i.name === "first_frame")?.link != null;
    if (ff) note = "接了首帧时 LibTV 按首尾帧生成，参考图不会被使用";
    else {
      pics.push(...imagesFrom(node, "ref_images", "参考图"));
      for (const u of lines(widgetVal(node, "ref_urls"), /[,，\s]+/)) pics.push({ name: baseName(u), title: u, preview: u, role: "链接" });
    }
  }
  const out = [];
  pics.forEach((a, i) => out.push({ ...a, kind: "picture", index: i + 1, token: `<Picture ${i + 1}>`, label: `图 ${i + 1}` }));
  vids.forEach((a, i) => out.push({ ...a, kind: "video", index: i + 1, token: `<Video ${i + 1}>`, label: `视频 ${i + 1}` }));
  auds.forEach((a, i) => out.push({ ...a, kind: "audio", index: i + 1, token: `<Audio ${i + 1}>`, label: `音频 ${i + 1}` }));
  return { list: out, note };
}

let style = false;
function installStyle() {
  if (style) return; style = true;
  const s = document.createElement("style");
  s.textContent = `
.cg-at-menu{position:fixed;z-index:2147483000;width:300px;max-height:420px;overflow:auto;padding:8px;box-sizing:border-box;border:1px solid rgba(255,255,255,.14);border-radius:9px;background:var(--comfy-menu-bg,#17191c);box-shadow:0 18px 48px rgba(0,0,0,.52);color:var(--input-text,#ddd);font-family:Inter,system-ui,sans-serif}
.cg-at-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:0 4px 8px;border-bottom:1px solid rgba(255,255,255,.10)}
.cg-at-head b{font-size:13px}.cg-at-head span{font-size:10px;opacity:.5}
.cg-at-row{display:grid;grid-template-columns:52px minmax(0,1fr);gap:10px;align-items:center;padding:7px;border-radius:7px;cursor:pointer;border-bottom:1px solid rgba(255,255,255,.055)}
.cg-at-row:hover,.cg-at-row.on{background:rgba(255,255,255,.08)}
.cg-at-thumb{width:52px;height:52px;border-radius:7px;overflow:hidden;background:rgba(255,255,255,.07);display:grid;place-items:center;font-size:18px;opacity:.9}
.cg-at-thumb img{width:100%;height:100%;object-fit:cover}
.cg-at-info{min-width:0}.cg-at-info b,.cg-at-info small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cg-at-info b{font-size:12px}.cg-at-info small{margin-top:3px;font-size:10px;opacity:.55}
.cg-at-tag{display:inline-block;margin-left:6px;padding:0 5px;border-radius:4px;background:rgba(0,226,187,.12);color:rgba(205,255,246,.95);font-size:10px;font-weight:500}
.cg-at-empty{padding:16px 8px;text-align:center;font-size:11px;opacity:.6;line-height:1.5}`;
  s.textContent += `
.cg-pe-wrap{position:relative;width:100%;height:100%;min-height:0;overflow:hidden;box-sizing:border-box;color:var(--input-text,#ddd)}
.cg-pe{position:absolute;inset:0;box-sizing:border-box;padding:8px 10px 26px;overflow-x:hidden;overflow-y:auto;overscroll-behavior:contain;white-space:pre-wrap;overflow-wrap:anywhere;outline:none;border:1px solid transparent;border-radius:8px;background:var(--comfy-input-bg,#222);font:13px/1.65 Inter,"Microsoft YaHei",system-ui,sans-serif;cursor:text}
.cg-pe:focus{border-color:rgba(255,255,255,.18)}
.cg-pe:empty:before{content:attr(data-placeholder);opacity:.4;pointer-events:none}
.cg-pe.is-raw{font:12px/1.6 Consolas,"Courier New",monospace}
.cg-pe-tools{position:absolute;right:8px;bottom:5px;display:flex;align-items:center;gap:6px}
.cg-pe-state{font-size:10px;opacity:.45;pointer-events:none}
.cg-pe-view{width:30px;height:20px;padding:0;border:1px solid rgba(255,255,255,.12);border-radius:5px;background:rgba(255,255,255,.05);color:inherit;cursor:pointer;font:600 10px Consolas,monospace}
.cg-pchip{display:inline-flex;align-items:center;gap:4px;margin:1px 2px;padding:1px 6px 1px 2px;border-radius:5px;font-size:11px;line-height:1.5;white-space:nowrap;vertical-align:middle;user-select:all;cursor:pointer}
.cg-pchip img{width:24px;height:24px;object-fit:cover;border-radius:3px}
.cg-pchip .ic{display:grid;place-items:center;width:22px;height:22px;border-radius:3px;background:rgba(255,255,255,.09);font-size:11px}
.cg-pchip-picture{background:rgba(0,210,180,.12);color:rgb(184,255,242);box-shadow:inset 0 0 0 1px rgba(0,210,180,.24)}
.cg-pchip-video{background:rgba(76,170,255,.13);color:rgb(194,226,255);box-shadow:inset 0 0 0 1px rgba(76,170,255,.25)}
.cg-pchip-audio{background:rgba(255,174,70,.12);color:rgb(255,224,183);box-shadow:inset 0 0 0 1px rgba(255,174,70,.24)}
.cg-pchip-missing{background:rgba(255,90,90,.12);color:rgb(255,190,190);box-shadow:inset 0 0 0 1px rgba(255,90,90,.35)}`;
  document.head.append(s);
}


/* ------------------------------------------------------------ 胶囊编辑器 */
const CARET = "​";
const TOKEN = /<(Picture|Video|Audio)\s+(\d+)>/gi;
const KIND = { picture: "picture", video: "video", audio: "audio" };
const TAG = { picture: "Picture", video: "Video", audio: "Audio" };
const NAME = { picture: "图", video: "视频", audio: "音频" };

function promptWidget(node) { return node.widgets?.find((w) => w.name === "prompt"); }
function rawOf(node) { return String(promptWidget(node)?.value ?? ""); }
function setRaw(node, v, dirty = true) {
  const w = promptWidget(node); if (!w) return;
  w.value = v; if (w._state) w._state.value = v;
  if (dirty) { node.setDirtyCanvas?.(true, true); app.graph?.change?.(); }
}
function hideWidget(w) {
  w.hidden = true; w.options ||= {}; w.options.hidden = true; w.options.canvasOnly = true;
  if (w._state?.options) { w._state.options.hidden = true; w._state.options.canvasOnly = true; }
  w.computeSize = () => [0, -4];
  if (w.element?.style) w.element.style.display = "none";
  if (w.inputEl?.style) w.inputEl.style.display = "none";
}

function chipFor(node, raw, kind, index, assets = assetsFor(node).list) {
  const a = assets.find((x) => x.kind === kind && x.index === index);
  const chip = document.createElement("span");
  chip.className = `cg-pchip cg-pchip-${a ? kind : "missing"}`;
  chip.contentEditable = "false";
  chip.dataset.raw = raw;
  if (a?.preview && kind === "picture") {
    const img = document.createElement("img"); img.src = a.preview; img.alt = ""; img.draggable = false;
    img.onerror = () => { const ic = document.createElement("span"); ic.className = "ic"; ic.textContent = "▧"; img.replaceWith(ic); };
    chip.append(img);
  } else { const ic = document.createElement("span"); ic.className = "ic"; ic.textContent = kind === "video" ? "▶" : kind === "audio" ? "♪" : "▧"; chip.append(ic); }
  const t = document.createElement("span"); t.textContent = `${NAME[kind]}${index}`; chip.append(t);
  chip.title = a ? `${a.role}：${a.name || a.title}` : `没有第 ${index} 个${NAME[kind]}，请检查接入的参考素材`;
  return chip;
}
function appendText(el, text) {
  String(text).split("\n").forEach((part, i) => { if (i) el.append(document.createElement("br")); if (part) el.append(document.createTextNode(part)); });
}
function renderRich(node, el, raw) {
  el.replaceChildren();
  const assets = assetsFor(node).list;
  let last = 0;
  for (const m of String(raw).matchAll(TOKEN)) {
    appendText(el, raw.slice(last, m.index));
    const kind = KIND[m[1].toLowerCase()];
    el.append(chipFor(node, `<${TAG[kind]} ${Number(m[2])}>`, kind, Number(m[2]), assets));
    last = m.index + m[0].length;
  }
  appendText(el, raw.slice(last));
}
function serialize(root) {
  let out = "";
  const walk = (r) => {
    for (const c of r.childNodes) {
      if (c.nodeType === Node.TEXT_NODE) { out += c.nodeValue.replaceAll(CARET, ""); continue; }
      if (c.nodeType !== Node.ELEMENT_NODE) continue;
      if (c.dataset?.raw != null) { out += c.dataset.raw; continue; }
      if (c.tagName === "BR") { out += "\n"; continue; }
      const block = /^(DIV|P|LI)$/.test(c.tagName);
      if (block && out && !out.endsWith("\n")) out += "\n";
      if (!(block && c.childNodes.length === 1 && c.firstChild.tagName === "BR")) walk(c);
    }
  };
  walk(root);
  return out;
}

function sync(node) { const st = node.__cgPE; if (st && !st.rendering) setRaw(node, st.raw ? st.el.innerText : serialize(st.el)); }
function refresh(node, force = false) {
  const st = node.__cgPE; if (!st) return;
  if (!force && document.activeElement === st.el) return;
  st.rendering = true;
  try {
    if (st.raw) { st.el.replaceChildren(); appendText(st.el, rawOf(node)); }
    else renderRich(node, st.el, rawOf(node));
    const { list } = assetsFor(node);
    const c = { picture: 0, video: 0, audio: 0 }; list.forEach((a) => c[a.kind]++);
    st.state.textContent = [c.picture && `${c.picture} 图`, c.video && `${c.video} 视频`, c.audio && `${c.audio} 音频`].filter(Boolean).join(" · ") || "输入 @ 引用参考";
    st.view.textContent = st.raw ? "@" : "</>";
    st.view.title = st.raw ? "切回胶囊显示" : "显示纯文本原文";
    st.el.classList.toggle("is-raw", st.raw);
  } finally { st.rendering = false; }
}
function refreshSoon(node) { clearTimeout(node.__cgPET); node.__cgPET = setTimeout(() => refresh(node, true), 60); }

function insertNodes(el, range, nodes, node) {
  range.deleteContents();
  const marker = document.createTextNode(CARET), frag = document.createDocumentFragment();
  nodes.forEach((n) => frag.append(n)); frag.append(marker);
  range.insertNode(frag);
  const sel = getSelection(), r = document.createRange();
  r.setStart(marker, marker.length); r.collapse(true); sel.removeAllRanges(); sel.addRange(r);
  sync(node); el.focus({ preventScroll: true });
}

function ensureEditor(node) {
  if (node.__cgPE) return true;
  const w = promptWidget(node);
  if (!w || typeof node.addDOMWidget !== "function") return false;
  installStyle();
  const wrap = document.createElement("div"); wrap.className = "cg-pe-wrap";
  const el = document.createElement("div"); el.className = "cg-pe";
  el.contentEditable = "true"; el.spellcheck = false;
  el.dataset.placeholder = "输入提示词，输入 @ 引用参考图 / 视频 / 音频…";
  const tools = document.createElement("div"); tools.className = "cg-pe-tools";
  const state = document.createElement("span"); state.className = "cg-pe-state";
  const view = document.createElement("button"); view.type = "button"; view.className = "cg-pe-view";
  view.addEventListener("pointerdown", (e) => { e.preventDefault(); e.stopPropagation(); });
  view.addEventListener("click", () => { sync(node); node.__cgPE.raw = !node.__cgPE.raw; refresh(node, true); });
  tools.append(state, view); wrap.append(el, tools);

  el.addEventListener("input", (e) => { sync(node); if (!node.__cgPE.raw) onEditorInput(node, e); });
  el.addEventListener("compositionend", () => setTimeout(() => { sync(node); if (!node.__cgPE.raw) onEditorInput(node, {}); }, 0));
  el.addEventListener("keydown", (e) => { if (!onMenuKey(e)) e.stopPropagation(); });
  el.addEventListener("keyup", (e) => e.stopPropagation());
  el.addEventListener("blur", () => { sync(node); setTimeout(() => { if (document.activeElement !== el) { closeMenu(); refresh(node, true); } }, 120); });
  el.addEventListener("paste", (e) => {
    e.preventDefault(); e.stopPropagation();
    const text = e.clipboardData?.getData("text/plain") || "";
    const sel = getSelection(); if (!sel.rangeCount) return;
    if (node.__cgPE.raw) { document.execCommand("insertText", false, text); return; }
    const tmp = document.createElement("div"); renderRich(node, tmp, text);
    insertNodes(el, sel.getRangeAt(0), [...tmp.childNodes], node);
  });
  el.addEventListener("copy", (e) => {
    const sel = getSelection(); if (!sel.rangeCount || sel.isCollapsed) return;
    const text = serialize(sel.getRangeAt(0).cloneContents()); if (!text) return;
    e.preventDefault(); e.clipboardData?.setData("text/plain", text);
  });
  el.addEventListener("pointerdown", (e) => {
    e.stopPropagation();
    const chip = e.target.closest?.(".cg-pchip");
    if (chip && el.contains(chip)) { e.preventDefault(); openMenuForChip(node, chip); }
  });
  wrap.addEventListener("pointerdown", (e) => e.stopPropagation());
  wrap.addEventListener("wheel", (e) => { if (el.scrollHeight > el.clientHeight) e.stopPropagation(); }, { passive: true });

  const dom = node.addDOMWidget("prompt_editor", "cg_prompt_editor", wrap, {
    serialize: false, margin: 6,
    getMinHeight: () => 110, getMaxHeight: () => 900,
    getValue: () => rawOf(node),
    setValue: (v) => { setRaw(node, v, false); refresh(node, true); },
  });
  if (!dom) { wrap.remove(); return false; }
  dom.serialize = false;
  // 编辑框放回原 prompt 的位置
  const list = node.widgets, di = list.indexOf(dom), pi = list.indexOf(w);
  if (di > -1 && pi > -1 && di !== pi + 1) { list.splice(di, 1); list.splice(pi + 1, 0, dom); }
  hideWidget(w);
  node.__cgPE = { el, wrap, state, view, dom, raw: false, rendering: false };
  refresh(node, true);
  node.setSize?.([Math.max(node.size[0], 380), Math.max(node.size[1], node.computeSize?.()[1] ?? 0)]);
  return true;
}
function installSoon(node, n = 0) {
  if (node.__cgPE) return;
  setTimeout(() => {
    let ok = false;
    try { ok = ensureEditor(node); } catch (e) { console.warn("[CloudGen] 提示词编辑器初始化失败", e); }
    if (!ok && n < 10) installSoon(node, n + 1);
  }, n ? 100 * n : 0);
}

/* ------------------------------------------------------------ @ 菜单 */
let cur = null; // { node, el, range, chip, items, active, menu, all, note, q }
function closeMenu() { cur?.menu?.remove(); cur = null; }

function hitAtCaret(el) {
  const sel = getSelection(); if (!sel?.rangeCount || !sel.isCollapsed) return null;
  const r = sel.getRangeAt(0); let n = r.startContainer, off = r.startOffset;
  if (!el.contains(n)) return null;
  if (n.nodeType !== Node.TEXT_NODE) { const prev = n.childNodes[off - 1]; if (prev?.nodeType !== Node.TEXT_NODE) return null; n = prev; off = prev.length; }
  const before = n.nodeValue.slice(0, off).replaceAll(CARET, "");
  const m = before.match(/[@＠]([^\s@＠]{0,12})$/);
  if (!m) return null;
  const full = n.nodeValue.slice(0, off); const at = Math.max(full.lastIndexOf("@"), full.lastIndexOf("＠"));
  const range = document.createRange(); range.setStart(n, at); range.setEnd(n, off);
  return { range, q: m[1].toLowerCase() };
}
function onEditorInput(node, e) {
  if (e?.isComposing) return;
  const hit = hitAtCaret(node.__cgPE.el);
  if (hit) openMenu(node, hit); else if (cur && !cur.chip) closeMenu();
}
function openMenu(node, hit, chip = null) {
  installStyle();
  const { list, note } = assetsFor(node);
  if (!cur || cur.node !== node) {
    closeMenu();
    const menu = document.createElement("div"); menu.className = "cg-at-menu";
    menu.addEventListener("pointerdown", (ev) => { ev.preventDefault(); ev.stopPropagation(); });
    document.body.append(menu);
    cur = { node, el: node.__cgPE.el, menu, active: 0 };
  }
  Object.assign(cur, { all: list, note, range: hit?.range || null, q: hit?.q || "", chip });
  renderMenu();
}
function openMenuForChip(node, chip) { closeMenu(); openMenu(node, null, chip); }
function renderMenu() {
  if (!cur) return;
  const { menu, all, note, q } = cur;
  const items = all.filter((a) => !q || `${a.label}${a.token}${a.name}${a.role}`.toLowerCase().replace(/\s+/g, "").includes(q));
  cur.items = items; cur.active = Math.min(cur.active, Math.max(0, items.length - 1));
  menu.replaceChildren();
  const head = document.createElement("div"); head.className = "cg-at-head";
  head.innerHTML = `<b>${cur.chip ? "替换引用" : "引用参考"}</b><span>↑↓ 选择 · Enter 插入 · Esc 关闭</span>`;
  menu.append(head);
  items.forEach((a, i) => {
    const row = document.createElement("div"); row.className = "cg-at-row" + (i === cur.active ? " on" : "");
    const th = document.createElement("div"); th.className = "cg-at-thumb";
    if (a.preview && a.kind === "picture") { const img = document.createElement("img"); img.src = a.preview; img.alt = ""; img.onerror = () => { img.remove(); th.textContent = "▧"; }; th.append(img); }
    else th.textContent = a.kind === "video" ? "▶" : a.kind === "audio" ? "♪" : "▧";
    const info = document.createElement("div"); info.className = "cg-at-info";
    const b = document.createElement("b"); b.textContent = a.label;
    const tag = document.createElement("span"); tag.className = "cg-at-tag"; tag.textContent = a.role; b.append(tag);
    const sm = document.createElement("small"); sm.textContent = a.name || ""; sm.title = a.title || a.name || "";
    info.append(b, sm); row.append(th, info);
    row.addEventListener("pointerdown", (ev) => { ev.preventDefault(); ev.stopPropagation(); choose(a); });
    row.addEventListener("mouseenter", () => { cur.active = i; menu.querySelectorAll(".cg-at-row").forEach((r, k) => r.classList.toggle("on", k === i)); });
    menu.append(row);
  });
  if (!items.length || note) {
    const e = document.createElement("div"); e.className = "cg-at-empty";
    e.textContent = items.length ? note : all.length ? "没有匹配的参考素材" : (note || "还没有接入参考图。把图片连到 images / ref_images / 首尾帧，或在高级里填参考视频、音频路径后再用 @ 引用");
    menu.append(e);
  }
  placeMenu();
}
function placeMenu() {
  if (!cur) return;
  let r = cur.chip?.getBoundingClientRect() || cur.range?.getBoundingClientRect();
  if (!r || (!r.width && !r.height)) r = cur.el.getBoundingClientRect();
  const m = cur.menu, w = 300, h = Math.min(420, m.offsetHeight || 300);
  let left = r.left, top = r.bottom + 6;
  if (left + w > innerWidth - 8) left = innerWidth - w - 8;
  if (top + h > innerHeight - 8) top = Math.max(8, r.top - h - 6);
  Object.assign(m.style, { left: `${Math.max(8, Math.round(left))}px`, top: `${Math.max(8, Math.round(top))}px` });
}
function choose(a) {
  if (!cur) return;
  const { node, el, chip, range } = cur;
  const nchip = chipFor(node, a.token, a.kind, a.index);
  if (chip) { chip.replaceWith(nchip); sync(node); }
  else if (range) insertNodes(el, range, [nchip], node);
  closeMenu();
}
function onMenuKey(e) {
  if (!cur || e.isComposing || e.keyCode === 229) return false;
  const n = cur.items?.length || 0;
  if (e.key === "ArrowDown" && n) { cur.active = (cur.active + 1) % n; renderMenu(); }
  else if (e.key === "ArrowUp" && n) { cur.active = (cur.active - 1 + n) % n; renderMenu(); }
  else if ((e.key === "Enter" || e.key === "Tab") && n) choose(cur.items[cur.active]);
  else if (e.key === "Escape") closeMenu();
  else return false;
  e.preventDefault(); e.stopPropagation(); return true;
}
document.addEventListener("pointerdown", (e) => { if (cur && !cur.menu.contains(e.target) && !cur.el.contains(e.target)) closeMenu(); }, true);
window.addEventListener("resize", () => cur && placeMenu());
document.addEventListener("scroll", () => cur && placeMenu(), true);


/* ------------------------------------------------------------ 注册 */
app.registerExtension({
  name: "CloudGen.PromptEditor",
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (!TYPES.has(nodeData.name)) return;
    const created = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () { const r = created?.apply(this, arguments); installSoon(this); return r; };
    const configure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () { const r = configure?.apply(this, arguments); installSoon(this); refreshSoon(this); return r; };
    const conn = nodeType.prototype.onConnectionsChange;
    nodeType.prototype.onConnectionsChange = function () { const r = conn?.apply(this, arguments); refreshSoon(this); return r; };
    const wchg = nodeType.prototype.onWidgetChanged;
    nodeType.prototype.onWidgetChanged = function (name) { const r = wchg?.apply(this, arguments); if (["ref_video_path", "audio_path", "ref_urls"].includes(name)) refreshSoon(this); return r; };
    const ser = nodeType.prototype.onSerialize;
    nodeType.prototype.onSerialize = function () { if (this.__cgPE && !this.__cgPE.rendering) sync(this); return ser?.apply(this, arguments); };
    const exec = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function () { const r = exec?.apply(this, arguments); refreshSoon(this); return r; };
  },
});
// 上游换图（LoadImage 选了别的文件）时刷新缩略图
setInterval(() => {
  for (const node of app.graph?._nodes || []) {
    const st = node.__cgPE; if (!st || document.activeElement === st.el) continue;
    const sig = JSON.stringify(assetsFor(node).list.map((a) => [a.kind, a.preview, a.name]));
    if (sig !== st.sig) { st.sig = sig; refresh(node, true); }
  }
}, 1500);

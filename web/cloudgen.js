import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_TYPES = {
  CloudGen_XYQ_Image: { provider: "xyq", kind: "image" },
  CloudGen_XYQ_Video: { provider: "xyq", kind: "video" },
  CloudGen_LibTV_Image: { provider: "libtv", kind: "image" },
  CloudGen_LibTV_Video: { provider: "libtv", kind: "video" },
  CloudGen_Lovart_Image: { provider: "lovart", kind: "image" },
  CloudGen_Lovart_Video: { provider: "lovart", kind: "video" },
  CloudGen_Flow_Image: { provider: "flow", kind: "image" },
  CloudGen_Flow_Video: { provider: "flow", kind: "video" },
};
const DEFAULT = "默认";
const modelCache = {};

async function getJSON(url, opts) {
  const r = await api.fetchApi(url, opts);
  return await r.json();
}

async function loadModels(provider, kind, refresh = false) {
  const key = `${provider}:${kind}`;
  if (!refresh && modelCache[key]) return modelCache[key];
  const d = await getJSON(`/cloudgen/models?provider=${provider}&kind=${kind}${refresh ? "&refresh=1" : ""}`);
  if (d.error) throw new Error(d.error);
  modelCache[key] = d.models || [];
  return modelCache[key];
}

const fmt = (v) => (v === null || v === undefined || v === "" ? "?" : Number.isInteger(+v) ? String(+v) : (+v).toFixed(2));

/* ------------------------------------------------------------ node 增强 */
function setOptions(node, name, values, keepDefault = true) {
  const w = node.widgets?.find((x) => x.name === name);
  if (!w || !values || !values.length) return;
  const vals = keepDefault && w.options.values.includes(DEFAULT) ? [DEFAULT, ...values.filter((v) => v !== DEFAULT)] : values;
  w.options.values = [...new Set(vals.map(String))];
  if (!w.options.values.includes(String(w.value))) w.value = w.options.values[0];
}

async function applyModel(node, meta) {
  const mw = node.widgets?.find((w) => w.name === "model");
  if (!mw) return;
  if (meta.provider === "lovart") return; // Lovart 的参数因模型而异，用 extra_json 传，不做联动
  const list = modelCache[`${meta.provider}:${meta.kind}`] || [];
  let m = list.find((x) => x.name === mw.value);
  if (!m) return;
  if (meta.provider === "libtv" && !m._detail) {
    try {
      const d = await getJSON(`/cloudgen/model_detail?kind=${meta.kind}&name=${encodeURIComponent(m.name)}`);
      if (!d.error) Object.assign(m, { cost: d.cost ?? m.cost, options: d.options || {}, _detail: true });
    } catch (e) {}
  }
  node.cloudgenModel = m;
  if (meta.provider === "flow") {
    if (m.ratios?.length) setOptions(node, "ratio", m.ratios, false);
    if (meta.kind === "image" && m.resolutions?.length) setOptions(node, "resolution", m.resolutions, false);
    if (meta.kind === "video") {
      const names = { t2v: "文生视频", i2v: "首尾帧", r2v: "参考图生成" };
      setOptions(node, "mode", ["自动", ...(m.modes || []).map((x) => names[x]).filter(Boolean)], false);
      const dw = node.widgets.find((w) => w.name === "duration");
      const opts = m.duration?.options || [];
      if (dw && opts.length) { dw.options.values = opts; if (!opts.includes(Number(dw.value))) dw.value = m.duration.default ?? opts[opts.length - 1]; }
    }
    node.setDirtyCanvas?.(true, true);
    return;
  }
  if (meta.provider === "xyq") {
    if (m.ratios?.length) setOptions(node, "ratio", m.ratios);
    if (m.resolutions?.length) setOptions(node, "resolution", m.resolutions);
    if (meta.kind === "image") {
      if (m.efforts?.length) setOptions(node, "quality", m.efforts);
      else { const qw = node.widgets.find((w) => w.name === "quality"); if (qw) { qw.options.values = [DEFAULT]; qw.value = DEFAULT; } }
    }
    if (meta.kind === "video") {
      if (m.modes?.length) setOptions(node, "mode", ["自动", ...m.modes], false);
      const dw = node.widgets.find((w) => w.name === "duration");
      const du = m.duration || {};
      if (dw) {
        if (du.min !== undefined) { dw.options.min = 0; dw.options.max = du.max; }
        else if (du.options?.length) { dw.options.max = Math.max(...du.options); }
        dw.tooltip = du.options?.length ? `可选：${du.options.join("/")} 秒（0=默认）` : du.min !== undefined ? `${du.min}-${du.max} 秒，步长 ${du.step}（0=默认）` : "0=默认";
      }
    }
  } else {
    const o = m.options || {};
    const pick = (...names) => { for (const [k, v] of Object.entries(o)) if (names.includes(k.toLowerCase())) return v; return null; };
    const ratio = pick("ratio", "aspect_ratio", "aspectratio");
    if (ratio?.options?.length) setOptions(node, "ratio", ratio.options.map(String), false);
    const res = pick("resolution", "clarity");
    if (res?.options?.length && meta.kind === "video") setOptions(node, "resolution", res.options.map(String));
    const q = pick("quality") || (meta.kind === "image" ? res : null);
    if (q?.options?.length && meta.kind === "image") setOptions(node, "quality", q.options.map(String), false);
    const cnt = pick("count", "n", "num");
    const cw = node.widgets.find((w) => w.name === "count");
    if (cw && cnt?.options?.length) { cw.options.max = Math.max(...cnt.options.map(Number).filter((x) => !isNaN(x))); cw.tooltip = `可选：${cnt.options.join("/")}`; }
    const du = pick("duration", "duration_sec");
    const dw = node.widgets.find((w) => w.name === "duration");
    if (dw && du) {
      if (du.max !== undefined && du.max !== null) dw.options.max = du.max;
      dw.tooltip = du.options?.length ? `可选：${du.options.join("/")} 秒` : du.min !== undefined ? `${du.min}-${du.max} 秒（0=默认）` : dw.tooltip;
    }
    if (o.modes?.options?.length && meta.kind === "video") setOptions(node, "mode", ["自动", ...o.modes.options], false);
  }
  node.setDirtyCanvas(true, true);
}

async function initNode(node, meta, refresh = false) {
  const mw = node.widgets?.find((w) => w.name === "model");
  if (!mw) return;
  try {
    const list = await loadModels(meta.provider, meta.kind, refresh);
    if (list.length) {
      const names = meta.provider === "lovart" ? [DEFAULT, ...list.map((m) => m.label)] : list.map((m) => m.name);
      mw.options.values = names;
      if (!names.includes(mw.value)) mw.value = names[0];
    }
    node.cloudgenStatus = "";
  } catch (e) {
    node.cloudgenStatus = String(e.message || e).slice(0, 80);
  }
  await applyModel(node, meta);
}

let priceData = { rules: {}, observed: {} };
async function loadPrices() {
  try { priceData = await getJSON("/cloudgen/prices"); } catch (e) {}
  app.graph?.setDirtyCanvas(true, true);
}
const UNIT_LABEL = { per_run: "每次", per_image: "每张", per_sec: "每秒" };

function dimWidgetName(meta) { return meta.provider === "libtv" && meta.kind === "image" ? "quality" : "resolution"; }

function withYuan(meta, total, suffix, plus = "") {
  const y = yuan(meta.provider, total);
  return { text: `~${fmt(total)}${plus} 积分${y ? " " + y + plus : ""}${suffix}`, total };
}

// 参考视频时长：按路径向后端查询并缓存，查到后重画节点
const refDurCache = {};
function refVideoSeconds(node, raw) {
  const paths = String(raw || "").split(/\r?\n/).map((x) => x.trim().replace(/^"|"$/g, "")).filter(Boolean);
  if (!paths.length) return null;
  let seconds = 0, pending = false, missing = false;
  const need = [];
  for (const p of paths) {
    const v = refDurCache[p];
    if (v === undefined) { need.push(p); pending = true; }
    else if (v === "loading") pending = true;
    else if (v === null) missing = true;
    else seconds += v;
  }
  if (need.length) {
    need.forEach((p) => (refDurCache[p] = "loading"));
    getJSON(`/cloudgen/media_duration?paths=${encodeURIComponent(need.join("\n"))}`)
      .then((j) => { for (const it of j.items || []) refDurCache[it.path] = it.duration ?? null; })
      .catch(() => need.forEach((p) => (refDurCache[p] = null)))
      .finally(() => node.setDirtyCanvas?.(true, true));
  }
  return { seconds, pending, missing };
}

// 1 积分折合人民币：来自「套餐」里的 套餐价 ÷ 积分额度（或手填）
let planData = {};
async function loadPlans() { try { planData = await getJSON("/cloudgen/plans"); } catch (e) { /* 忽略 */ } }
function yuan(provider, credits) {
  const r = Number(planData?.[provider]?.credit_rate) || 0;
  if (!r || !(credits > 0)) return "";
  const v = credits * r;
  return "≈¥" + (v < 10 ? v.toFixed(2) : v < 100 ? v.toFixed(1) : Math.round(v).toString());
}

function estimateCost(node, meta) {
  const m = node.cloudgenModel;
  if (!m) return null;
  const wv = (n) => node.widgets?.find((w) => w.name === n)?.value;
  const dim = String(wv(dimWidgetName(meta)) ?? "默认");
  const key = `${meta.provider}:${meta.kind}:${m.name}`;
  const rule = priceData.rules?.[key];
  const ob = priceData.observed?.[`${key}|${dim}`];
  let base = null, unit = meta.kind === "image" ? "per_image" : "per_run";
  // 小云雀视频：带参考视频时，参考视频的时长也计费，单价可能不同
  const refInfo = meta.provider === "xyq" && meta.kind === "video" ? refVideoSeconds(node, wv("ref_video_path")) : null;
  if (rule) {
    unit = rule.unit || unit;
    const by = rule.by_res || {};
    // "默认" 换成模型自己的默认档；小云雀图片按 "分辨率|推理强度" 定价
    const res = dim === DEFAULT ? String(m.default_resolution ?? DEFAULT) : dim;
    const q = wv("quality");
    const eff = meta.provider === "xyq" && meta.kind === "image"
      ? String(q === undefined || q === DEFAULT ? (m.default_effort ?? "") : q) : "";
    base = (eff ? by[`${res}|${eff}`] : undefined) ?? by[res] ?? by[dim] ?? rule.default;
    if (refInfo) base = rule.ref_by_res?.[res] ?? rule.ref_by_res?.[dim] ?? rule.ref_default ?? base;
  }
  else if (m.cost) base = m.cost;
  if (base === null || base === undefined || base === "") {
    return ob ? withYuan(meta, ob.cost, " · 实测") : null;
  }
  base = Number(base);
  if (unit === "per_sec") {
    let d = Number(wv("duration")) || 0;
    if (!d) d = m.duration?.default || 5;
    if (refInfo) {
      if (refInfo.pending) return withYuan(meta, base * d, ` · ${d}秒+参考(读取中)`, "+");
      if (refInfo.missing) return withYuan(meta, base * d, ` · ${d}秒+参考(未读到时长)`, "+");
      const s = Math.ceil(refInfo.seconds);
      return withYuan(meta, base * (d + s), ` · ${d}秒+参考${s}秒`);
    }
    return withYuan(meta, base * d, ` · ${d}秒`);
  }
  if (unit === "per_image") {
    const c = Number(wv("count")) || 1;
    return withYuan(meta, base * c, c > 1 ? ` · ${c}张` : " · 每张");
  }
  return withYuan(meta, base, " · 每次");
}

function drawCoin(ctx, cx, cy, r) {
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.fillStyle = "#f5b82e"; ctx.fill();
  ctx.beginPath(); ctx.arc(cx, cy, r * 0.55, 0, Math.PI * 2); ctx.strokeStyle = "#7a5200"; ctx.lineWidth = 1.2; ctx.stroke();
}

function drawBadges(node, ctx) {
  const meta = NODE_TYPES[node.comfyClass || node.type];
  const est = meta ? estimateCost(node, meta) : null;
  ctx.save();
  if (est && !node.flags?.collapsed) {
    const H = LiteGraph.NODE_TITLE_HEIGHT || 30;
    ctx.font = "12px Inter, sans-serif";
    const tw = ctx.measureText(est.text).width;
    const w = tw + 34, h = 20, x = node.size[0] - w - 8, y = -H + (H - h) / 2;
    ctx.fillStyle = "#00000059";
    ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x, y, w, h, 6) : ctx.rect(x, y, w, h); ctx.fill();
    drawCoin(ctx, x + 12, y + h / 2, 5.5);
    ctx.fillStyle = "#e8e8e8"; ctx.textBaseline = "middle";
    ctx.fillText(est.text, x + 24, y + h / 2 + 0.5);
  }
  if (node.cloudgenStatus && !node.flags?.collapsed) {
    ctx.font = "12px sans-serif"; ctx.textBaseline = "alphabetic";
    ctx.fillStyle = "#9fd3ff";
    const lines = String(node.cloudgenStatus).split("\n").slice(0, 2);
    lines.forEach((l, i) => ctx.fillText(l.slice(0, 60), 8, node.size[1] + 16 + i * 15));
  }
  ctx.restore();
}

api.addEventListener("cloudgen.progress", ({ detail }) => {
  const node = app.graph?.getNodeById?.(Number(detail?.node) || detail?.node);
  if (node) { node.cloudgenStatus = detail.text; node.setDirtyCanvas(true, true); }
});

/* ------------------------------------------------------------ 侧边栏 */
const css = `
.cg-panel{padding:12px;font-size:13px;display:flex;flex-direction:column;gap:10px;color:var(--fg-color)}
.cg-card{border:1px solid var(--border-color);border-radius:10px;padding:10px 12px;background:var(--comfy-menu-bg)}
.cg-head{display:flex;justify-content:space-between;align-items:center;gap:6px}
.cg-title{font-size:14px;font-weight:600;display:flex;align-items:center;gap:6px;min-width:0}
.cg-big{font-size:24px;font-weight:600;color:#ffd75e;line-height:1.3}
.cg-dim{opacity:.65;font-size:12px}
.cg-err{color:#ff8a80;font-size:12px;word-break:break-all}
.cg-warn{margin-top:6px;padding:4px 8px;border-radius:6px;background:#ff8a8022;color:#ffab91;font-size:12px}
.cg-tag{padding:1px 7px;border-radius:8px;background:#ffd75e22;color:#ffd75e;font-size:11px;font-weight:400;white-space:nowrap}
.cg-dot{width:7px;height:7px;border-radius:50%;display:inline-block}
.cg-bar{height:5px;border-radius:3px;background:var(--border-color);margin:6px 0 4px;overflow:hidden}
.cg-bar>div{height:100%;background:linear-gradient(90deg,#4fc3f7,#81d4fa)}
.cg-kv{display:flex;justify-content:space-between;font-size:12px;margin-top:3px}
.cg-kv span:first-child{opacity:.6}
.cg-row{display:flex;gap:6px;align-items:center;margin-top:6px}
.cg-row input,.cg-row select,.cg-in{min-width:0;background:var(--comfy-input-bg);color:var(--input-text);border:1px solid var(--border-color);border-radius:6px;padding:4px 6px}
.cg-row input{flex:1}
.cg-btn{cursor:pointer;background:var(--comfy-input-bg);color:var(--input-text);border:1px solid var(--border-color);border-radius:6px;padding:4px 10px;white-space:nowrap}
.cg-btn:hover{filter:brightness(1.25)}
.cg-btn.primary{background:#3b82f6;border-color:#3b82f6;color:#fff}
.cg-mask{position:fixed;inset:0;background:#0008;z-index:10000;display:flex;align-items:center;justify-content:center}
.cg-dlg{width:min(860px,94vw);max-height:86vh;display:flex;flex-direction:column;background:var(--comfy-menu-bg);color:var(--fg-color);border:1px solid var(--border-color);border-radius:12px;box-shadow:0 10px 40px #0009;font-size:13px}
.cg-dlg-h{display:flex;align-items:center;gap:4px;padding:10px 14px;border-bottom:1px solid var(--border-color)}
.cg-tab{cursor:pointer;padding:5px 12px;border-radius:6px;opacity:.7}
.cg-tab.on{background:var(--comfy-input-bg);opacity:1;font-weight:600}
.cg-dlg-b{padding:14px;overflow:auto;flex:1}
.cg-sec{border:1px solid var(--border-color);border-radius:10px;padding:10px 12px;margin-bottom:12px}
.cg-sec h4{margin:0 0 8px;font-size:14px;display:flex;justify-content:space-between;align-items:center}
.cg-grid{display:grid;grid-template-columns:90px 1fr;gap:6px 10px;align-items:center}
.cg-grid input,.cg-grid select{background:var(--comfy-input-bg);color:var(--input-text);border:1px solid var(--border-color);border-radius:6px;padding:4px 6px;min-width:0}
.cg-ptable{width:100%;border-collapse:collapse}
.cg-ptable th{font-weight:500;opacity:.6;text-align:left;font-size:12px;padding:4px 6px;border-bottom:1px solid var(--border-color)}
.cg-ptable td{padding:5px 6px;border-bottom:1px solid var(--border-color);vertical-align:top}
.cg-chips{display:flex;flex-wrap:wrap;gap:6px}
.cg-chip{display:flex;align-items:center;gap:4px;font-size:12px;background:var(--comfy-input-bg);border-radius:6px;padding:2px 2px 2px 6px}
.cg-chip input{width:58px;background:transparent;border:none;border-left:1px solid var(--border-color);color:var(--input-text);padding:2px 4px}
.cg-seg{display:flex;gap:4px;margin-bottom:10px;flex-wrap:wrap}
`;

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else if (k === "value") e.value = v; else e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return e;
}
function ensureCss() { if (!document.getElementById("cg-style")) document.head.append(el("style", { id: "cg-style" }, css)); }

/* ---------- 侧边栏卡片（精简） */
function shortSpan(s) { return s ? String(s).replace(/(\d+)天\d+小时/, "$1天") : ""; }

function flowCard(d) {
  const f = d.flow || {};
  const card = el("div", { class: "cg-card" },
    el("div", { class: "cg-head" },
      el("div", { class: "cg-title" }, el("span", { class: "cg-dot", style: `background:${d.ok ? "#66bb6a" : "#ef5350"}` }), "Flow2API",
        el("span", { class: "cg-tag", title: f.base || "" }, "自建网关"))));
  if (d.ok) {
    card.append(el("div", {}, el("span", { class: "cg-big" }, `${f.active ?? 0}`), el("span", { class: "cg-dim" }, ` / ${f.total ?? 0} 个账号可用`)));
    if (f.banned) card.append(el("div", { class: "cg-warn" }, `⚠ ${f.banned} 个账号被限流（429）`));
    card.append(el("div", { class: "cg-dim" }, "Google Flow 积分请在 Flow2API 管理后台查看"));
  } else card.append(el("div", { class: "cg-err" }, (d.error || "不可用").slice(0, 120)));
  return card;
}

function balanceCard(d) {
  if (d.provider === "flow") return flowCard(d);
  const p = d.plan || {};
  const quota = Number(p.monthly_quota) || 0;
  const card = el("div", { class: "cg-card" },
    el("div", { class: "cg-head" },
      el("div", { class: "cg-title" }, el("span", { class: "cg-dot", style: `background:${d.ok ? "#66bb6a" : "#ef5350"}` }), d.name,
        (p.plan_name || d.member) ? el("span", { class: "cg-tag", title: d.member || "" }, p.plan_name || d.member) : null)));
  if (d.ok && d.balance_source === "none") {
    card.append(el("div", { class: "cg-dim", style: "margin:4px 0" }, "LibTV 接口不提供余额，在「⚙ 配置 → 套餐」里录入当前余额后，每次生成会自动扣减"));
  } else if (d.ok) {
    card.append(el("div", {}, el("span", { class: "cg-big" }, d.balance), quota ? el("span", { class: "cg-dim" }, ` / ${quota}`) : el("span", { class: "cg-dim" }, " 积分")));
    if (quota) card.append(el("div", { class: "cg-bar" }, el("div", { style: `width:${Math.max(0, Math.min(100, (Number(d.balance) / quota) * 100 || 0))}%` })));
    if (d.balance_source === "manual") card.append(el("div", { class: "cg-dim" }, `手动录入 ${String(p.manual_balance_at || "").slice(5)}${p.deducted ? `，之后已扣 ${fmt(p.deducted)}` : ""}`));
  } else {
    card.append(el("div", { class: "cg-err" }, d.provider === "libtv" ? "未授权，在「⚙ 配置 → 账户」里点 LibTV 授权" : (d.error || "不可用").slice(0, 80)));
  }
  if (p.next_reset) card.append(el("div", { class: "cg-kv" }, el("span", {}, "积分重置"), el("span", {}, `${p.next_reset} · 剩${shortSpan(p.reset_left)}`)));
  if (Number(p.daily_free) > 0 && d.ok) card.append(el("div", { class: "cg-kv" }, el("span", {}, "今日赠送"), el("span", {}, `剩 ${p.free_left ?? p.daily_free} / ${p.daily_free} · 先扣`)));
  if (p.expiring !== undefined && (Number(p.daily_free) > 0 || Number(p.topup) > 0) && d.ok) card.append(el("div", { class: "cg-kv" }, el("span", {}, "套餐内"), el("span", {}, `${p.expiring} · 重置清零`)));
  if (Number(p.topup) > 0 && d.ok) card.append(el("div", { class: "cg-kv" }, el("span", {}, "充值积分"), el("span", {}, `${p.topup_left ?? p.topup} · 最后扣、不清零`)));
  if (p.daily_budget && d.ok) card.append(el("div", { class: "cg-kv" }, el("span", {}, "日均可用"), el("span", {}, `≈ ${p.daily_budget}`)));
  if (p.plan_expire_text) card.append(el("div", { class: "cg-kv" }, el("span", {}, "套餐到期"), el("span", {}, p.plan_left === "已到期" ? "已到期" : `${p.plan_expire_text.slice(0, 10)} · 剩${shortSpan(p.plan_left)}`)));
  if (d.ok && d.provider === "xyq" && !p.plan_name && !p.plan_expire_text && !(quota > 0))
    card.append(el("div", { class: "cg-dim", style: "margin-top:4px" }, "接口只提供剩余积分；套餐、到期等需在「⚙ 配置 → 套餐」手动填写"));
  if (p.warn) card.append(el("div", { class: "cg-warn" }, "⚠ " + p.warn));
  return card;
}

async function renderPanel(root) {
  ensureCss();
  root.innerHTML = "";
  const panel = el("div", { class: "cg-panel" });
  root.append(panel);
  panel.append(el("div", { class: "cg-head" }, el("b", { style: "font-size:15px" }, "☁️ 云端积分"),
    el("div", { style: "display:flex;gap:6px" },
      el("button", { class: "cg-btn", onclick: () => renderPanel(root) }, "刷新"),
      el("button", { class: "cg-btn", onclick: () => openConfig("account", () => renderPanel(root)) }, "⚙ 配置"))));
  const cards = el("div", { style: "display:flex;flex-direction:column;gap:10px" }, el("div", { class: "cg-dim" }, "查询中…"));
  panel.append(cards);
  try {
    const d = await getJSON("/cloudgen/balances");
    cards.innerHTML = "";
    (d.items || []).forEach((it) => cards.append(balanceCard(it)));
    cards.append(el("div", { class: "cg-dim" }, `更新于 ${new Date().toLocaleTimeString()}`));
  } catch (e) { cards.innerHTML = ""; cards.append(el("div", { class: "cg-err" }, e.message)); }
  panel.append(await jobsCard(() => renderPanel(root)));
}

const ST_LABEL = { pending: "进行中", done: "完成", failed: "失败" };
async function jobsCard(refresh) {
  const d = await getJSON("/cloudgen/jobs").catch(() => ({ jobs: [] }));
  const all = d.jobs || [];
  const card = el("div", { class: "cg-card" });
  const pending = all.filter((j) => j.status === "pending").length;
  const today = new Date().toISOString().slice(0, 10);
  const spentToday = all.filter((j) => (j.created || "").startsWith(today) && j.cost > 0).reduce((s, j) => s + Number(j.cost), 0);
  const spentYuan = all.filter((j) => (j.created || "").startsWith(today) && j.cost > 0)
    .reduce((s, j) => s + Number(j.cost) * (Number(planData?.[j.provider]?.credit_rate) || 0), 0);
  card.append(el("div", { class: "cg-head" }, el("div", { class: "cg-title" }, "最近任务"),
    el("span", { class: "cg-dim" }, [pending ? `${pending} 个进行中` : null, spentToday ? `今日 -${fmt(spentToday)}${spentYuan > 0 ? ` (¥${spentYuan < 10 ? spentYuan.toFixed(2) : spentYuan.toFixed(1)})` : ""}` : null].filter(Boolean).join(" · "))));
  if (!all.length) card.append(el("div", { class: "cg-dim" }, "还没有任务"));
  const list = el("div");
  let showAll = false;
  const draw = () => {
    list.innerHTML = "";
    for (const j of all.slice(0, showAll ? 30 : 5)) {
      const color = j.status === "done" ? "#66bb6a" : j.status === "failed" ? "#ef5350" : "#ffca28";
      const cost = j.cost > 0 ? `${j.cost_src === "estimate" ? "≈" : ""}-${fmt(j.cost)}${yuan(j.provider, Number(j.cost)) ? " (" + yuan(j.provider, Number(j.cost)).slice(1) + ")" : ""}` : j.status === "pending" ? (j.progress ? `${Math.round(j.progress)}%` : "…") : "";
      const detail = el("div", { style: "display:none;padding:4px 0 2px 13px" },
        el("div", { class: "cg-dim", style: "word-break:break-all" }, j.prompt || ""),
        j.error ? el("div", { class: "cg-err" }, j.error.slice(0, 160)) : null,
        j.outputs?.length ? el("div", { class: "cg-dim" }, "已保存：" + j.outputs.map((p) => p.split(/[\\/]/).pop()).join("，")) : null);
      const msg = el("div", { class: "cg-dim", style: "word-break:break-all" });
      detail.append(el("div", { class: "cg-row" },
        j.status !== "failed" ? el("button", {
          class: "cg-btn", onclick: async (e) => {
            e.stopPropagation(); msg.textContent = "查询中…";
            const r = await getJSON("/cloudgen/jobs/fetch", { method: "POST", body: JSON.stringify({ id: j.id }) });
            msg.textContent = r.error ? r.error : r.status === "done" ? "已保存到 output/cloudgen：" + (r.outputs || []).map((p) => p.split(/[\\/]/).pop()).join("，")
              : `还在生成${r.progress ? " " + Math.round(r.progress) + "%" : ""}`;
          }
        }, j.status === "done" ? "下载结果" : "查询 / 取回") : null,
        el("button", { class: "cg-btn", onclick: async (e) => { e.stopPropagation(); await getJSON("/cloudgen/jobs/delete", { method: "POST", body: JSON.stringify({ id: j.id }) }); refresh(); } }, "删除")), msg);
      const row = el("div", { style: "padding:5px 0;border-top:1px solid var(--border-color);cursor:pointer", title: j.prompt || "" },
        el("div", { class: "cg-head" },
          el("div", { style: "display:flex;align-items:center;gap:6px;min-width:0" },
            el("span", { class: "cg-dot", style: `background:${color};flex:none` }),
            el("span", { style: "white-space:nowrap;overflow:hidden;text-overflow:ellipsis" }, j.model || ""),
            el("span", { class: "cg-dim", style: "flex:none" }, ({ xyq: "小云雀", libtv: "LibTV", lovart: "Lovart", flow: "Flow" })[j.provider] || j.provider)),
          el("span", { style: `flex:none;font-weight:600;color:${j.cost > 0 ? "#ffd75e" : "inherit"}` }, cost)),
        el("div", { class: "cg-dim", style: "padding-left:13px" }, `${(j.created || "").slice(5, 16)} · ${ST_LABEL[j.status] || j.status}`),
        detail);
      row.addEventListener("click", () => { detail.style.display = detail.style.display === "none" ? "block" : "none"; });
      list.append(row);
    }
    if (all.length > 5) list.append(el("a", { href: "#", class: "cg-dim", style: "display:block;margin-top:4px", onclick: (e) => { e.preventDefault(); showAll = !showAll; draw(); } }, showAll ? "收起" : `查看全部 ${all.length} 条`));
  };
  draw();
  card.append(list);
  return card;
}

/* ---------- 配置弹窗 */
function openConfig(tab = "account", onClose) {
  ensureCss();
  const mask = el("div", { class: "cg-mask" });
  const close = () => { mask.remove(); document.removeEventListener("keydown", esc); onClose?.(); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", esc);
  mask.addEventListener("mousedown", (e) => { if (e.target === mask) close(); });
  const body = el("div", { class: "cg-dlg-b" });
  const tabs = { account: "账户", plans: "套餐", prices: "积分价格" };
  const head = el("div", { class: "cg-dlg-h" });
  const show = (t) => {
    head.querySelectorAll(".cg-tab").forEach((x) => x.classList.toggle("on", x.dataset.t === t));
    body.innerHTML = "";
    ({ account: tabAccount, plans: tabPlans, prices: tabPrices })[t](body);
  };
  for (const [k, v] of Object.entries(tabs)) head.append(el("div", { class: "cg-tab", "data-t": k, onclick: () => show(k) }, v));
  head.append(el("div", { style: "flex:1" }), el("button", { class: "cg-btn", onclick: close }, "关闭"));
  mask.append(el("div", { class: "cg-dlg" }, head, body));
  document.body.append(mask);
  show(tab);
}

async function tabAccount(body) {
  const cfg = await getJSON("/cloudgen/config").catch(() => ({}));
  const keyIn = el("input", { type: "password", placeholder: cfg.xyq_configured ? `已设置 ${cfg.xyq_key_masked}（留空不修改）` : "粘贴 Access Key" });
  const teamIn = el("input", { placeholder: "个人空间留空", value: cfg.xyq_team_id || "" });
  const xyqMsg = el("span", { class: "cg-dim" });
  body.append(el("div", { class: "cg-sec" }, el("h4", {}, "小云雀", el("span", { class: "cg-dim" }, cfg.xyq_configured ? "已配置" : "未配置")),
    el("div", { class: "cg-dim", style: "margin-bottom:8px" }, "登录 xyq.jianying.com，顶部【CLI/API】→【API】→ 新建秘钥"),
    el("div", { class: "cg-grid" }, el("span", {}, "Access Key"), keyIn, el("span", {}, "团队 ID"), teamIn),
    el("div", { class: "cg-row" }, el("button", {
      class: "cg-btn primary", onclick: async () => {
        const b = { xyq_team_id: teamIn.value };
        if (keyIn.value.trim()) b.xyq_access_key = keyIn.value.trim();
        await getJSON("/cloudgen/config", { method: "POST", body: JSON.stringify(b) });
        delete modelCache["xyq:image"]; delete modelCache["xyq:video"];
        xyqMsg.textContent = "已保存"; refreshAllNodes();
      }
    }, "保存"), xyqMsg)));

  const fbIn = el("input", { value: cfg.flow_base_url || "", placeholder: "http://IP:端口" });
  const fkIn = el("input", { type: "password", placeholder: cfg.flow_key_set ? "已设置（留空不修改）" : "Flow2API 的 API Key" });
  const flowMsg = el("span", { class: "cg-dim" });
  body.append(el("div", { class: "cg-sec" }, el("h4", {}, "Flow2API", el("span", { class: "cg-dim" }, cfg.flow_key_set ? "已配置" : "未配置")),
    el("div", { class: "cg-dim", style: "margin-bottom:8px" }, "自建的 Flow2API 服务（Google Flow：Nano Banana / Veo 3.1）"),
    el("div", { class: "cg-grid" }, el("span", {}, "服务地址"), fbIn, el("span", {}, "API Key"), fkIn),
    el("div", { class: "cg-row" }, el("button", {
      class: "cg-btn primary", onclick: async () => {
        const b = { flow_base_url: fbIn.value.trim() };
        if (fkIn.value.trim()) b.flow_api_key = fkIn.value.trim();
        await getJSON("/cloudgen/config", { method: "POST", body: JSON.stringify(b) });
        delete modelCache["flow:image"]; delete modelCache["flow:video"];
        const t = await getJSON("/cloudgen/models?provider=flow&kind=image&refresh=1").catch((e) => ({ error: String(e) }));
        flowMsg.textContent = t.error ? `已保存，但连接失败：${t.error}` : `已保存，连接正常（${t.models?.length || 0} 个图片模型）`;
        refreshAllNodes();
      }
    }, "保存并测试"),
      el("button", { class: "cg-btn", title: "https://github.com/terry-xu-2077/flow2api",
        onclick: () => window.open("https://github.com/terry-xu-2077/flow2api", "_blank") }, "Flow2API仓库"),
      flowMsg)));

  const lt = cfg.libtv || {};
  const msg = el("div", { class: "cg-dim", style: "margin-top:6px;word-break:break-all" }, lt.login_error ? `上次授权失败：${lt.login_error}` : "");
  const sec = el("div", { class: "cg-sec" }, el("h4", {}, "LibTV", el("span", { class: "cg-dim" }, lt.authorized ? `已授权${lt.token_expires ? " · token 至 " + lt.token_expires : ""}` : "未授权")),
    el("div", { class: "cg-dim" }, "OAuth 授权，不需要 API Key：点按钮后在浏览器里微信扫码"),
    el("div", { class: "cg-row" },
      el("button", {
        class: "cg-btn primary", onclick: async () => {
          const d = await getJSON("/cloudgen/libtv/login", { method: "POST" });
          if (d.error) { msg.textContent = d.error; return; }
          msg.innerHTML = ""; msg.append("浏览器没弹出就手动打开：", el("a", { href: d.url, target: "_blank" }, "授权链接"));
          window.open(d.url, "_blank");
          const timer = setInterval(async () => {
            const c = await getJSON("/cloudgen/config");
            if (c.libtv?.authorized && !c.libtv?.login_running) {
              clearInterval(timer); delete modelCache["libtv:image"]; delete modelCache["libtv:video"];
              refreshAllNodes(); body.innerHTML = ""; tabAccount(body);
            }
            else if (c.libtv?.login_error) { clearInterval(timer); msg.textContent = c.libtv.login_error; }
          }, 2000);
          setTimeout(() => clearInterval(timer), 600000);
        }
      }, lt.authorized ? "重新授权" : "LibTV 授权"),
      lt.authorized ? el("button", { class: "cg-btn", onclick: async () => { await getJSON("/cloudgen/libtv/logout", { method: "POST" }); body.innerHTML = ""; tabAccount(body); } }, "退出") : null,
      lt.authorized ? el("button", {
        class: "cg-btn", title: "把 LibTV 全部工具的参数 schema 写到节点包目录 libtv_tools.json", onclick: async (ev) => {
          ev.target.textContent = "导出中…";
          const d = await getJSON("/cloudgen/libtv/tools");
          const p = await getJSON("/cloudgen/libtv/probe");
          ev.target.textContent = d.error ? "导出失败" : `已导出 ${d.count} 个工具`;
          msg.textContent = d.error || p.error || `${d.path}；接口样例：${p.path}`;
        }
      }, "导出诊断信息") : null),
    msg);
  body.append(sec);

  const lv = cfg.lovart || {};
  const lvMsg = el("div", { class: "cg-dim", style: "margin-top:6px;word-break:break-all" }, lv.login_error ? `上次授权失败：${lv.login_error}` : "");
  body.append(el("div", { class: "cg-sec" }, el("h4", {}, "Lovart", el("span", { class: "cg-dim" }, lv.authorized ? `已授权${lv.token_expires ? " · token 至 " + lv.token_expires : ""}` : "未授权")),
    el("div", { class: "cg-dim" }, "OAuth 授权，不需要 API Key：点按钮后在浏览器里登录 Lovart 并同意（视频模型多数需要 Lovart 会员）"),
    el("div", { class: "cg-row" },
      el("button", {
        class: "cg-btn primary", onclick: async () => {
          const d = await getJSON("/cloudgen/lovart/login", { method: "POST" });
          if (d.error) { lvMsg.textContent = d.error; return; }
          lvMsg.innerHTML = ""; lvMsg.append("浏览器没弹出就手动打开：", el("a", { href: d.url, target: "_blank" }, "授权链接"));
          window.open(d.url, "_blank");
          const timer = setInterval(async () => {
            const c = await getJSON("/cloudgen/config");
            if (c.lovart?.authorized && !c.lovart?.login_running) {
              clearInterval(timer); delete modelCache["lovart:image"]; delete modelCache["lovart:video"];
              refreshAllNodes(); body.innerHTML = ""; tabAccount(body);
            }
            else if (c.lovart?.login_error) { clearInterval(timer); lvMsg.textContent = c.lovart.login_error; }
          }, 2000);
          setTimeout(() => clearInterval(timer), 600000);
        }
      }, lv.authorized ? "重新授权" : "Lovart 授权"),
      lv.authorized ? el("button", { class: "cg-btn", onclick: async () => { await getJSON("/cloudgen/lovart/logout", { method: "POST" }); delete modelCache["lovart:image"]; delete modelCache["lovart:video"]; body.innerHTML = ""; tabAccount(body); } }, "退出") : null),
    lvMsg));
  await assetSection(body);
}

async function assetSection(body) {
  const info = await getJSON("/cloudgen/assets").catch(() => ({}));
  const msg = el("span", { class: "cg-dim" });
  const btn = (prov, label) => el("button", {
    class: "cg-btn", onclick: async () => {
      const r = await getJSON("/cloudgen/assets/clear", { method: "POST", body: JSON.stringify({ provider: prov }) });
      msg.textContent = `已清空 ${r.cleared} 条`;
    }
  }, label);
  body.append(el("div", { class: "cg-sec" }, el("h4", {}, "参考素材缓存", el("span", { class: "cg-dim" }, `LibTV ${info.libtv ?? 0} 条 · 小云雀 ${info.xyq ?? 0} 条`)),
    el("div", { class: "cg-dim" }, "同一张参考图只上传一次，之后直接复用（按图片内容识别）。LibTV 会先确认画布上的素材节点还在，被删了就自动重传；小云雀素材如果失效导致生成报错，在这里清空后重试。"),
    el("div", { class: "cg-row" }, btn("libtv", "清空 LibTV 缓存"), btn("xyq", "清空小云雀缓存"), msg)));
}

async function configuredMap() {
  const c = await getJSON("/cloudgen/config").catch(() => ({}));
  return { xyq: !!c.xyq_configured, libtv: !!c.libtv?.authorized, lovart: !!c.lovart?.authorized };
}

async function tabPlans(body) {
  const data = await getJSON("/cloudgen/plans").catch(() => ({}));
  const on = await configuredMap();
  body.append(el("div", { class: "cg-dim", style: "margin-bottom:10px" }, "到期日和重置日在两个网站的会员/积分页能看到。积分会在「重置日 + 重置时间」清零重发，例如 23 日 00:00 重置，就要在 22 日内用完。"));
  const shown = [["xyq", "小云雀"], ["libtv", "LibTV"]].filter(([prov]) => on[prov]);
  if (!shown.length) body.append(el("div", { class: "cg-dim" }, "还没有配置小云雀或 LibTV，请先在「账户」里配置。"));
  for (const [prov, name] of shown) {
    const p = data[prov] || {};
    const f = {
      plan_name: el("input", { value: p.plan_name || "", placeholder: "如 个人高级版VIP" }),
      plan_expire: el("input", { value: p.plan_expire || "", placeholder: "2027-06-27 或 2027-02-13 13:43" }),
      reset_day: el("input", { type: "number", min: "0", max: "31", value: p.reset_day || "", placeholder: "1-31，0=不重置" }),
      reset_time: el("input", { type: "time", value: p.reset_time || "00:00" }),
      monthly_quota: el("input", { type: "number", min: "0", value: p.monthly_quota || "", placeholder: "套餐每月发放的积分（不含每日赠送）" }),
      plan_price: el("input", { type: "number", min: "0", step: "any", style: "flex:1", value: p.plan_price ?? "", placeholder: prov === "libtv" ? "如 3999" : "如 759" }),
      plan_price_period: el("select", {}, el("option", { value: "year" }, "元/年"), el("option", { value: "month" }, "元/月")),
      topup: el("input", { type: "number", min: "0", value: p.topup || "", placeholder: "单独充值的积分（不随月重置），没有填 0" }),
      daily_free: el("input", { type: "number", min: "0", value: p.daily_free || "", placeholder: "每天赠送、次日清零的积分，没有填 0" }),
      yuan_per_credit: el("input", { type: "number", min: "0", step: "any", value: p.yuan_per_credit ?? "", placeholder: "留空=按套餐价自动算" }),
    };
    f.plan_price_period.value = p.plan_price_period || "year";
    const rateLine = el("div", { class: "cg-dim", style: "margin-top:6px" });
    const showRate = () => {
      const o = Number(f.yuan_per_credit.value), pr = Number(f.plan_price.value), q = Number(f.monthly_quota.value);
      const r = o > 0 ? o : pr > 0 && q > 0 ? pr / (q * (f.plan_price_period.value === "year" ? 12 : 1)) : 0;
      rateLine.textContent = r ? `1 积分 ≈ ¥${r.toFixed(4)}（100 积分 ≈ ¥${(r * 100).toFixed(2)}），节点右上角和任务列表会按这个折算人民币` : "填「套餐价格」和「每月额度」后，节点右上角会显示折合人民币";
    };
    for (const k of ["plan_price", "plan_price_period", "yuan_per_credit", "monthly_quota"]) f[k].addEventListener("input", showRate);
    showRate();
    if (prov === "libtv") {
      const eff = p.manual_balance === null || p.manual_balance === undefined ? "" : Number(p.manual_balance) - Number(p.deducted || 0);
      f.manual_balance = el("input", { type: "number", min: "0", step: "any", value: eff, placeholder: "照网站上「会员积分剩余」填写" });
    }
    const m = el("span", { class: "cg-dim" });
    body.append(el("div", { class: "cg-sec" }, el("h4", {}, name),
      el("div", { class: "cg-grid" }, "套餐名称", f.plan_name, "套餐到期", f.plan_expire, "每月重置日", f.reset_day, "重置时间", f.reset_time, "每月额度", f.monthly_quota, "每日赠送", f.daily_free, "充值积分", f.topup,
        "套餐价格", el("div", { style: "display:flex;gap:6px" }, f.plan_price, f.plan_price_period), "1积分=¥", f.yuan_per_credit,
        f.manual_balance ? ["当前余额", f.manual_balance] : null),
      rateLine,
      f.manual_balance ? el("div", { class: "cg-dim", style: "margin-top:6px" }, `LibTV 接口不提供余额：录入后每次生成会按任务返回的消耗（没有则按「积分价格」估算）自动扣减；过了重置时间自动失效。${p.manual_balance_at ? "上次录入 " + p.manual_balance_at : ""}`) : null,
      el("div", { class: "cg-row" }, el("button", {
        class: "cg-btn primary", onclick: async () => {
          const b = { provider: prov }; for (const [k, i] of Object.entries(f)) b[k] = i.value;
          const r = await getJSON("/cloudgen/plans", { method: "POST", body: JSON.stringify(b) });
          m.textContent = r.error ? r.error : "已保存";
          await loadPlans(); app.graph?.setDirtyCanvas?.(true, true);
        }
      }, "保存"), m)));
  }
}

const PRICE_GROUPS = [["xyq", "image", "小云雀 · 图片"], ["xyq", "video", "小云雀 · 视频"], ["libtv", "image", "LibTV · 图片"], ["libtv", "video", "LibTV · 视频"]];

async function dimOptions(provider, kind, m) {
  if (provider === "xyq") return (m.resolutions || []).map(String);
  try {
    const d = await getJSON(`/cloudgen/model_detail?kind=${kind}&name=${encodeURIComponent(m.name)}`);
    const o = d.options || {};
    const pick = (...ns) => Object.entries(o).find(([k]) => ns.includes(k.toLowerCase()))?.[1];
    const v = kind === "image" ? (pick("quality") || pick("resolution")) : pick("resolution", "clarity");
    return (v?.options || []).map(String);
  } catch (e) { return []; }
}

async function tabPrices(body, group = 0) {
  const on = await configuredMap();
  const groups = PRICE_GROUPS.filter((g) => on[g[0]]);
  if (!groups.length) { body.append(el("div", { class: "cg-dim" }, "还没有配置小云雀或 LibTV，请先在「账户」里配置。")); return; }
  const [prov, kind, title] = groups[Math.min(group, groups.length - 1)];
  const seg = el("div", { class: "cg-seg" }, groups.map((g, i) => el("div", { class: "cg-tab" + (i === group ? " on" : ""), onclick: () => { body.innerHTML = ""; tabPrices(body, i); } }, g[2])));
  body.append(seg, el("div", { class: "cg-dim", style: "margin-bottom:8px" },
    "按网站上标的价格填写。计费方式选「每秒」时按时长乘，「每张」按张数乘；某分辨率不填就用「默认」。节点标题栏的 ⚡ 显示按此估算的消耗，括号里的“实测”是上次运行前后余额差。"));
  const box = el("div", {}, el("div", { class: "cg-dim" }, "加载模型…"));
  body.append(box);
  let models;
  try { models = await loadModels(prov, kind); } catch (e) { box.innerHTML = ""; box.append(el("div", { class: "cg-err" }, e.message)); return; }
  await loadPrices();
  const rows = [];
  const table = el("table", { class: "cg-ptable" }, el("tr", {}, el("th", { style: "width:30%" }, "模型"), el("th", { style: "width:90px" }, "计费方式"), el("th", { style: "width:80px" }, "默认"), el("th", {}, kind === "image" && prov === "libtv" ? "按质量/分辨率" : "按分辨率")));
  for (const m of models) {
    const key = `${prov}:${kind}:${m.name}`;
    const r = priceData.rules?.[key] || {};
    const unit = el("select", { class: "cg-in" }, ...Object.entries(UNIT_LABEL).filter(([u]) => kind === "video" ? u !== "per_image" : u !== "per_sec").map(([u, l]) => el("option", { value: u }, l)));
    unit.value = r.unit || (kind === "image" ? "per_image" : "per_run");
    const def = el("input", { class: "cg-in", type: "number", min: "0", step: "any", style: "width:70px", value: r.default ?? "", placeholder: m.cost ? String(m.cost) : "" });
    const chips = el("div", { class: "cg-chips" }, el("span", { class: "cg-dim" }, "…"));
    const obsLine = el("div", { class: "cg-dim", style: "margin-top:2px" });
    const obs = Object.entries(priceData.observed || {}).filter(([k]) => k.startsWith(key + "|"));
    if (obs.length) obsLine.textContent = "实测：" + obs.map(([k, v]) => `${k.split("|")[1]} ${fmt(v.cost)}${v.duration ? `/${v.duration}s` : ""}${v.count > 1 ? `/${v.count}张` : ""}`).join("，");
    const resInputs = {};
    const rule = m.pricing_rule ? el("div", { class: "cg-dim", style: "margin-top:2px" }, m.pricing_rule) : null;
    table.append(el("tr", {}, el("td", {}, m.name, rule, obsLine), el("td", {}, unit), el("td", {}, def), el("td", {}, chips)));
    rows.push({ key, unit, def, resInputs, old: r });
    // 扫价得到、表格里没列出的价格（图片的 分辨率|推理强度 组合、带参考视频单价）：只展示，保存时原样保留
    const extra = [];
    const combos = Object.keys(r.by_res || {}).filter((k) => k.includes("|"));
    if (combos.length) extra.push(`另有 ${combos.length} 个「分辨率|推理强度」组合价`);
    if (r.ref_by_res && Object.keys(r.ref_by_res).length)
      extra.push("带参考视频每秒：" + Object.entries(r.ref_by_res).map(([k, v]) => `${k} ${fmt(v)}`).join("，"));
    if (extra.length) obsLine.after(el("div", { class: "cg-dim", style: "margin-top:2px" }, extra.join("；")));
    dimOptions(prov, kind, m).then((opts) => {
      chips.innerHTML = "";
      if (!opts.length) { chips.append(el("span", { class: "cg-dim" }, "该模型没有分辨率选项")); return; }
      for (const o of opts) {
        const i = el("input", { type: "number", min: "0", step: "any", value: r.by_res?.[o] ?? "" });
        resInputs[o] = i;
        chips.append(el("label", { class: "cg-chip" }, o, i));
      }
    });
  }
  const msg = el("span", { class: "cg-dim" });
  box.innerHTML = "";
  box.append(table, el("div", { class: "cg-row", style: "margin-top:10px" }, el("button", {
    class: "cg-btn primary", onclick: async () => {
      const rules = {};
      for (const r of rows) {
        const by = { ...(r.old.by_res || {}) };
        for (const [k, i] of Object.entries(r.resInputs)) { if (i.value !== "") by[k] = i.value; else delete by[k]; }
        rules[r.key] = (r.def.value === "" && !Object.keys(by).length) ? null
          : { ...r.old, unit: r.unit.value, default: r.def.value === "" ? null : r.def.value, by_res: by };
      }
      const res = await getJSON("/cloudgen/prices", { method: "POST", body: JSON.stringify({ rules }) });
      msg.textContent = res.error || `已保存（${title}）`;
      await loadPrices();
    }
  }, "保存价格"), msg));
}

function refreshAllNodes() {
  for (const node of app.graph?._nodes || []) {
    const meta = NODE_TYPES[node.comfyClass || node.type];
    if (meta) initNode(node, meta, true);
  }
}

/* ------------------------------------------------------------ 注册 */
/* ------------------------------------------------ Nodes 2.0（Vue 节点）模式
 * Vue 节点的标题栏是 DOM，不走画布 onDrawForeground；内置 API 节点的积分徽章来自服务端的
 * price_badge 规则，V1 节点没法声明。所以在 Vue 模式下把同款徽章（同样的 class）插进标题栏，
 * 进度/结果文字插到节点底部。Vue 重新渲染会丢掉插入的元素，用定时同步补回来。 */
function syncVueNode(node, meta) {
  const id = node.id;
  const header = document.querySelector(`[data-testid="node-header-${id}"]`);
  if (!header) return false;
  const row = header.firstElementChild || header;
  const est = estimateCost(node, meta);
  let wrap = row.querySelector(":scope > .cg-vbadge");
  if (!est) { wrap?.remove(); }
  else {
    const t = String(est.text);
    const sp = t.indexOf(" ");
    const req = sp > 0 ? t.slice(0, sp) : t, rest = sp > 0 ? t.slice(sp + 1) : "";
    const key = req + "|" + rest;
    if (!wrap || wrap.dataset.key !== key) {
      if (!wrap) { wrap = document.createElement("span"); wrap.className = "cg-vbadge flex min-w-0 shrink items-center"; row.appendChild(wrap); }
      wrap.dataset.key = key;
      wrap.innerHTML = "";
      const a = document.createElement("span");
      a.className = "flex h-5 shrink-0 items-center bg-component-node-widget-background p-1 text-xs " + (rest ? "rounded-l-full pr-1" : "rounded-full");
      a.innerHTML = '<i class="icon-[lucide--coins] h-full bg-amber-400"></i>';
      const at = document.createElement("span"); at.className = "truncate"; at.textContent = req; a.appendChild(at);
      wrap.appendChild(a);
      if (rest) {
        const b = document.createElement("span");
        b.className = "-ml-1 flex h-5 max-w-max min-w-0 grow basis-0 items-center truncate rounded-r-full bg-component-node-widget-background text-xs";
        const bt = document.createElement("span"); bt.className = "pr-2"; bt.textContent = rest; b.appendChild(bt);
        wrap.appendChild(b);
      }
      wrap.title = t;
    }
  }
  const body = document.querySelector(`[data-testid="node-body-${id}"]`);
  if (body) {
    // 状态文字放进节点底部那行（和「comfyui_cloudgen_hub」来源徽章同一行）；找不到这一行就放到节点底部
    const badgeRow = body.querySelector(":scope > .mt-auto");
    const target = badgeRow || body;
    let st = body.querySelector(".cg-vstatus");
    const text = node.cloudgenStatus ? String(node.cloudgenStatus).split("\n")[0] : "";
    if (!text) st?.remove();
    else {
      if (!st) {
        st = document.createElement("div"); st.className = "cg-vstatus";
        st.style.cssText = "min-width:0;font-size:12px;color:#9fd3ff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:20px" + (badgeRow ? ";flex:1" : ";padding:2px 10px 0");
      }
      if (st.parentElement !== target) target.appendChild(st);
      if (st.textContent !== text) { st.textContent = text; st.title = text; }
    }
  }
  return true;
}

setInterval(() => {
  const nodes = app.graph?._nodes;
  if (!nodes || !document.querySelector(".lg-node-header")) return;
  for (const node of nodes) {
    const meta = NODE_TYPES[node.comfyClass || node.type];
    if (meta) { try { syncVueNode(node, meta); } catch (e) { /* 忽略单个节点的问题 */ } }
  }
}, 400);

app.registerExtension({
  name: "CloudGen.LibTV.XYQ",
  async setup() {
    loadPrices();
    loadPlans();
    const reg = app.extensionManager?.registerSidebarTab;
    if (reg) {
      app.extensionManager.registerSidebarTab({
        id: "cloudgen-credits",
        icon: "pi pi-wallet",
        title: "云端积分",
        tooltip: "小云雀 / LibTV / Lovart 积分与授权",
        type: "custom",
        render: (root) => renderPanel(root),
      });
    }
  },
  async beforeRegisterNodeDef(nodeType, nodeData) {
    const meta = NODE_TYPES[nodeData.name];
    const isCredits = nodeData.name === "CloudGen_Credits";
    if (!meta && !isCredits) return;

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onCreated?.apply(this, arguments);
      if (meta) {
        const mw = this.widgets?.find((w) => w.name === "model");
        if (mw) {
          const cb = mw.callback;
          mw.callback = (...a) => { cb?.apply(mw, a); applyModel(this, meta); };
        }
        const b = this.addWidget("button", "🔄 刷新模型列表", null, () => initNode(this, meta, true));
        b.serialize = false;
        setTimeout(() => initNode(this, meta), 50);
      }
      if (isCredits) {
        const w = this.addWidget("text", "积分", "点击运行或等待加载…", () => {}, { multiline: true });
        w.serialize = false;
        this.cgText = w;
        const b = this.addWidget("button", "🔄 立即查询", null, () => loadCredits(this));
        b.serialize = false;
        setTimeout(() => loadCredits(this), 50);
        this.setSize([320, 160]);
      }
      return r;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const r = onConfigure?.apply(this, arguments);
      if (meta) setTimeout(() => initNode(this, meta), 100);
      return r;
    };

    const onDraw = nodeType.prototype.onDrawForeground;
    nodeType.prototype.onDrawForeground = function (ctx) {
      onDraw?.apply(this, arguments);
      drawBadges(this, ctx);
    };

    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      onExecuted?.apply(this, arguments);
      const info = message?.cloudgen_info?.[0];
      if (info) loadPlans();
      if (info) {
        if (isCredits && this.cgText) this.cgText.value = info;
        else this.cloudgenStatus = info.split("\n").slice(0, 2).join("\n");
        this.setDirtyCanvas(true, true);
      }
    };
  },
});

async function loadCredits(node) {
  try {
    const d = await getJSON("/cloudgen/balances");
    node.cgText.value = d.text || "";
  } catch (e) { node.cgText.value = String(e); }
  node.setDirtyCanvas(true, true);
}

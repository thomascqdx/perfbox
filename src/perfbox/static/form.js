// form.js — 把清单 params 渲染成表单。只操作 DOM 节点，不用 innerHTML。
// 导出 buildForm(command, {pick}) → {root, values, validate, setValues}
// pick(kind, multiple, extensions) → Promise<string[]> 由 app.js 注入
// （POST /api/pick）。

function el(tag, attrs = {}, children = []) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on") && typeof v === "function")
      n.addEventListener(k.slice(2), v);
    else if (v === true) n.setAttribute(k, "");
    else n.setAttribute(k, String(v));
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    n.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return n;
}

function emptyOf(v) {
  return v == null || v === "" || (Array.isArray(v) && v.length === 0);
}

// ---------------------------------------------------------- 单值控件
function pathWidget(p, pick, inList) {
  const input = el("input", {type: "text", "aria-label": p.label});
  if (p.default != null && p.default !== "")
    input.placeholder = `默认：${p.default}`;
  const accept = p.accept || ["file", "dir"];
  const btn = (kind, text) => el("button", {
    type: "button", text,
    onclick: async (ev) => {
      const b = ev.currentTarget;
      b.disabled = true;
      try {
        const paths = await pick(kind, inList ? true : false,
                                 p.extensions || null);
        if (paths.length) {
          input.value = paths[0];
          input.dispatchEvent(new CustomEvent(
            "picked", {bubbles: true, detail: paths.slice(1)}));
        }
      } catch { /* 取消或失败时静默 */ } finally {
        b.disabled = false;
      }
    }});
  const row = el("div", {class: "pathline"}, [input]);
  if (accept.includes("file")) row.append(btn("file", "浏览文件"));
  if (accept.includes("dir")) row.append(btn("dir", "浏览目录"));
  if (p.extensions && p.extensions.length)
    row.append(el("span", {class: "help", style: "align-self:center",
                           text: `支持 ${p.extensions.join(" ")}`}));
  return {el: row, input,
          get: () => input.value,
          set: (v) => { input.value = v == null ? "" : String(v); }};
}

function textWidget(p) {
  const input = el("input", {type: "text", "aria-label": p.label});
  if (p.default != null && p.default !== "")
    input.placeholder = `默认：${p.default}`;
  return {el: input, input, get: () => input.value,
          set: (v) => { input.value = v == null ? "" : String(v); }};
}

function numberWidget(p) {
  const input = el("input", {
    type: "text", "aria-label": p.label,
    inputmode: p.type === "integer" ? "numeric" : "decimal"});
  if (p.default != null && p.default !== "")
    input.placeholder = `默认：${p.default}`;
  return {el: input, input, get: () => input.value,
          set: (v) => { input.value = v == null ? "" : String(v); }};
}

function boolWidget(p) {
  const input = el("input", {type: "checkbox"});
  const lab = el("label", {class: "checkline"},
                 [input, el("span", {text: "启用"})]);
  return {el: lab, input, get: () => input.checked,
          set: (v) => { input.checked = !!v; }};
}

function choiceWidget(p) {
  const sel = el("select", {"aria-label": p.label});
  if (!p.required) sel.append(el("option", {value: "", text: "（不指定）"}));
  for (const c of p.choices || [])
    sel.append(el("option", {value: c, text: c}));
  return {el: sel, input: sel, get: () => sel.value,
          set: (v) => { sel.value = v == null ? "" : String(v); }};
}

function singleWidget(p, pick, inList) {
  switch (p.type) {
    case "path": return pathWidget(p, pick, inList);
    case "boolean": return boolWidget(p);
    case "choice": return choiceWidget(p);
    case "number": case "integer": return numberWidget(p);
    default: return textWidget(p);
  }
}

function checkNumber(p, v) {
  if (p.type !== "number" && p.type !== "integer") return null;
  if (emptyOf(v)) return null;
  const ok = p.type === "integer" ? /^[+-]?\d+$/.test(v.trim())
                                  : !Number.isNaN(Number(v));
  return ok ? null : (p.type === "integer" ? "请输入整数" : "请输入数字");
}

// ---------------------------------------------------------- 组合控件
function multiWidget(p, pick) {         // multiple：行列表
  const rows = el("div");
  const widgets = [];
  const addRow = (value) => {
    const w = singleWidget(p, pick, true);
    widgets.push(w);
    const rm = el("button", {type: "button", text: "移除",
      onclick: () => {
        const i = widgets.indexOf(w);
        if (i >= 0) widgets.splice(i, 1);
        row.remove();
      }});
    const row = el("div", {class: "mlrow"},
                   [el("div", {class: "mlfield"}, [w.el]), rm]);
    // 路径+multiple：浏览一次选多个文件时，其余路径追加成新行
    w.el.addEventListener("picked", (ev) => {
      for (const extra of ev.detail || []) addRow(extra);
    });
    rows.append(row);
    if (value !== undefined) w.set(value);
    return w;
  };
  addRow();
  const addBtn = el("button", {type: "button", class: "mladd",
                             text: "添加一项",
                             onclick: () => addRow()});
  const root = el("div", {class: "multilist"}, [rows, addBtn]);
  return {
    el: root,
    get: () => widgets.map(w => w.get()).filter(v => !emptyOf(v)),
    set: (arr) => {
      for (const w of [...widgets]) {
        const i = widgets.indexOf(w); widgets.splice(i, 1);
      }
      rows.replaceChildren();
      const vals = Array.isArray(arr) ? arr : (emptyOf(arr) ? [] : [arr]);
      if (!vals.length) addRow(); else for (const v of vals) addRow(v);
    },
    widgets,
  };
}

function countWidget(p, pick) {         // count=N：恰好 N 个并排
  const n = p.count;
  const names = n === 2 ? ["A 侧", "B 侧"]
                      : Array.from({length: n}, (_, i) => `第 ${i + 1} 项`);
  const widgets = [];
  const items = names.map((nm) => {
    const w = singleWidget(p, pick, false);
    widgets.push(w);
    return el("div", {class: "citem"},
              [el("div", {class: "clabel", text: nm}), w.el]);
  });
  return {
    el: el("div", {class: "countrow"}, items),
    get: () => widgets.map(w => w.get()),
    set: (arr) => {
      const vals = Array.isArray(arr) ? arr : [];
      widgets.forEach((w, i) => w.set(vals[i]));
    },
    widgets,
  };
}

// ---------------------------------------------------------- 行
function fieldFor(p, pick) {
  if (p.count) return countWidget(p, pick);
  if (p.multiple) return multiWidget(p, pick);
  return singleWidget(p, pick, false);
}

function rowFor(p, pick, idSeq) {
  const field = fieldFor(p, pick);
  const errEl = el("div", {class: "fielderr", hidden: true});
  const inner = [field.el, errEl];
  if (p.help) inner.push(el("div", {class: "help", text: p.help}));
  const label = el("label", {for: `f-${idSeq}`}, [p.label]);
  if (p.required) label.append(el("span", {class: "req", text: "必填"}));
  const focusEl = field.input || field.el.querySelector("input,select")
                  || field.el;
  if (focusEl.id == null || focusEl.id === "") focusEl.id = `f-${idSeq}`;
  const row = el("div", {class: "formrow"},
               [label, el("div", {class: "field"}, inner)]);
  return {row, field, errEl, focusEl, p};
}

// ---------------------------------------------------------- 表单
export function buildForm(command, {pick}) {
  const root = el("div");
  const items = [];
  let seq = 0;
  const normal = [], adv = [];
  for (const p of command.params || [])
    (p.advanced ? adv : normal).push(p);
  for (const p of normal) {
    const it = rowFor(p, pick, ++seq);
    items.push(it); root.append(it.row);
  }
  if (adv.length) {
    const det = el("details", {class: "advanced"});
    det.append(el("summary", {text: `高级选项（${adv.length} 项）`}));
    for (const p of adv) {
      const it = rowFor(p, pick, ++seq);
      items.push(it); det.append(it.row);
    }
    root.append(det);
  }

  function showErr(it, msg) {
    if (msg) {
      it.errEl.textContent = msg;
      it.errEl.hidden = false;
    } else {
      it.errEl.hidden = true;
    }
  }

  function validate() {
    let firstBad = null;
    for (const it of items) {
      const {p, field} = it;
      let msg = null;
      const raw = field.get();
      if (p.count) {
        const filled = raw.filter(v => !emptyOf(v)).length;
        if (filled === 0) {
          if (p.required) msg = `请填写${p.label}`;
        } else if (filled < p.count) {
          msg = `请填写全部 ${p.count} 项`;
        } else {
          for (const v of raw) {
            msg = checkNumber(p, v); if (msg) break;
          }
        }
      } else if (p.multiple) {
        if (p.required && raw.length === 0) msg = `请填写${p.label}`;
        else for (const v of raw) {
          msg = checkNumber(p, v); if (msg) break;
        }
      } else {
        if (p.required && emptyOf(raw)) msg = `请填写${p.label}`;
        else msg = checkNumber(p, raw);
      }
      showErr(it, msg);
      if (msg && !firstBad) firstBad = it;
    }
    if (firstBad) firstBad.focusEl.focus();
    return !firstBad;
  }

  function values() {
    const out = {};
    for (const it of items) {
      const {p, field} = it;
      const raw = field.get();
      if (p.type === "boolean") { out[p.name] = raw; continue; }
      if (p.count) {                    // 要么全填要么不送
        if (raw.filter(v => !emptyOf(v)).length === p.count)
          out[p.name] = raw;
        continue;
      }
      if (emptyOf(raw)) continue;
      out[p.name] = raw;
    }
    return out;
  }

  function setValues(vals) {
    if (!vals) return;
    for (const it of items)
      if (it.p.name in vals) it.field.set(vals[it.p.name]);
  }

  return {root, values, validate, setValues};
}

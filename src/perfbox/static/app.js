// app.js — perfbox 工作台。hash 路由 + API 轮询。
// 只构建 DOM 节点（textContent），不把服务器/清单字符串放进 innerHTML。

import {buildForm} from "./form.js";

// ------------------------------------------------------------- 基础
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

// token：#t= 进来时存 sessionStorage 然后跳走（不落进历史/日志）
if (location.hash.startsWith("#t=")) {
  sessionStorage.setItem("perfbox.token", location.hash.slice(3));
  location.replace(location.pathname + location.search + "#/");
}
const TOKEN = sessionStorage.getItem("perfbox.token") || "";

class GateError extends Error {}

async function api(path, {method = "GET", body} = {}) {
  const r = await fetch(path, {
    method,
    headers: {"X-Perfbox-Token": TOKEN,
              ...(body !== undefined ? {"Content-Type": "application/json"}
                                     : {})},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (r.status === 403) { showGate(); throw new GateError("未授权"); }
  let data = null;
  try { data = await r.json(); } catch { /* 空响应 */ }
  if (!r.ok)
    throw Object.assign(
      new Error((data && data.error) || `请求失败（${r.status}）`),
      {status: r.status});
  return data;
}

async function pick(kind, multiple, extensions) {
  const d = await api("/api/pick",
                      {method: "POST", body: {kind, multiple, extensions}});
  return d.paths || [];
}

const S = {state: null, jobs: [], manifestCache: new Map()};
const main = document.getElementById("main");
const railEl = document.getElementById("rail");
const statusEl = document.getElementById("statusline");
const bannerEl = document.getElementById("banner");
let viewTimers = [];

function clearViewTimers() {
  for (const t of viewTimers) clearTimeout(t);
  viewTimers = [];
}
function after(ms, fn) { return setTimeout(fn, ms); }
function viewAfter(ms, fn) {           // 视图切换时自动清掉
  const id = setTimeout(fn, ms);
  viewTimers.push(id);
  return id;
}

function showGate() {
  clearViewTimers();
  railEl.replaceChildren();
  statusEl.replaceChildren();
  bannerEl.hidden = true;
  main.replaceChildren(el("div", {class: "gate"}, [
    el("h1", {text: "perfbox"}),
    el("p", {text: "请从 perfbox 窗口里打印的地址打开本页"}),
  ]));
}

// ------------------------------------------------------------- 文案映射
const DOMAIN_COLORS = {startup: "#0F8B8D", jank: "#B4236C", io: "#C77700",
                       memory: "#6D3FC0", trace: "#2B7BB9"};
const STATE_TEXT = {queued: "排队中", running: "运行中", succeeded: "成功",
                    failed: "失败", cancelled: "已取消",
                    interrupted: "已中断"};
const ROLE_TEXT = {report: "报告", data: "数据", trace: "Trace"};
const TERMINAL = ["succeeded", "failed", "cancelled", "interrupted"];
const isTerminal = (j) => TERMINAL.includes(j.state);

function stateChip(state) {
  const cls = state === "running" ? "running"
            : state === "succeeded" ? "succeeded"
            : state === "failed" ? "failed" : "quiet";
  const chip = el("span", {class: `chip ${cls}`});
  if (state === "running") chip.append(el("span", {class: "pulse"}));
  chip.append(STATE_TEXT[state] || state);
  return chip;
}

function fmtTime(t) {
  if (!t) return "—";
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function fmtTimeShort(t) {
  if (!t) return "—";
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fmtDur(a, b) {
  if (a == null) return "—";
  const s = Math.max(0, (b == null ? Date.now() / 1000 : b) - a);
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${Math.floor(s % 60)} 秒`;
  return `${Math.floor(s / 3600)} 时 ${Math.floor((s % 3600) / 60)} 分`;
}
function toolTitle(id) {
  const t = (S.state?.tools || []).find(t => t.id === id);
  return t ? t.title : id;
}
function encodeRel(rel) {
  return rel.split("/").map(encodeURIComponent).join("/");
}
function fileUrl(job, rel) {
  return `/files/${TOKEN}/${job.id}/${encodeRel(rel)}`;
}
function copyBtn(text) {
  return el("button", {type: "button", text: "复制", onclick: async (ev) => {
    const b = ev.currentTarget;
    try {
      await navigator.clipboard.writeText(text);
      b.textContent = "已复制";
      after(1200, () => { b.textContent = "复制"; });
    } catch { /* 剪贴板不可用时静默 */ }
  }});
}

// ------------------------------------------------------------- 侧栏
function renderRail() {
  if (!S.state) return;
  const st = S.state;
  const cur = location.hash;
  const kids = [];
  kids.push(el("div", {class: "rail-brand"}, [
    el("div", {class: "wordmark", text: "perfbox"}),
    el("div", {class: "ver", text: st.version}),
  ]));
  const nav = el("nav", {class: "rail-nav"});
  const sel = el("select", {class: "rail-select",
    "aria-label": "选择工具",
    onchange: (ev) => {
      const v = ev.target.value;
      if (v) location.hash = `#/tool/${v}`;
    }});
  sel.append(el("option", {value: "", text: "选择工具"}));
  for (const d of st.domains || []) {
    const tools = (st.tools || []).filter(t => t.domain === d.id);
    if (!tools.length) continue;
    nav.append(el("div", {class: "rail-domain", text: d.title}));
    const grp = el("optgroup", {label: d.title});
    for (const t of tools) {
      const row = el("a", {
        class: "rail-tool" + (cur.startsWith(`#/tool/${t.id}`) ? " active" : ""),
        href: `#/tool/${t.id}`}, [
        el("span", {class: "dot",
                    style: `background:${DOMAIN_COLORS[t.domain] || "#999"}`}),
        el("span", {class: "t", text: t.title}),
        el("span", {
          class: "st" + (t.update ? " update" : ""),
          text: t.update ? `可更新 ${t.update}`
                         : (t.installed || "未安装")}),
      ]);
      nav.append(row);
      grp.append(el("option", {value: t.id, text:
        `${t.title}${t.installed ? " " + t.installed : "（未安装）"}`,
        selected: cur.startsWith(`#/tool/${t.id}`)}));
    }
    sel.append(grp);
  }
  kids.push(nav, sel);
  const running = S.jobs.filter(j => !isTerminal(j)).length;
  kids.push(el("div", {class: "rail-foot"}, [
    el("a", {href: "#/history",
             class: cur === "#/history" ? "active" : "", text: "历史任务"}),
    el("span", {class: "badge" + (running ? "" : " zero"),
                text: String(running)}),
  ]));
  railEl.replaceChildren(...kids);
}

function renderStatusline() {
  if (!S.state) return;
  statusEl.replaceChildren(
    el("span", {text: "工作目录"}),
    el("span", {class: "path", title: S.state.workdir,
                text: S.state.workdir}),
    copyBtn(S.state.workdir));
  if (S.state.config_error) {
    bannerEl.textContent =
      `配置文件无效：${S.state.config_error}（请检查 ` +
      `%LOCALAPPDATA%\\perfbox\\config.json）`;
    bannerEl.hidden = false;
  } else {
    bannerEl.hidden = true;
  }
}

async function refreshAll() {
  try {
    const [st, jb] = await Promise.all(
      [api("/api/state"), api("/api/jobs")]);
    S.state = st;
    S.jobs = jb.jobs || [];
    renderRail();
    renderStatusline();
  } catch (e) {
    if (!(e instanceof GateError)) {
      /* 暂时断线时保留旧状态 */
    }
  }
}

// ------------------------------------------------------------- 路由
function currentRoute() {
  const h = location.hash || "#/";
  let m;
  if ((m = h.match(/^#\/tool\/([a-z0-9-]+)(?:\/([a-z0-9-]+))?/)))
    return {name: "tool", id: m[1], cmd: m[2]};
  if ((m = h.match(/^#\/job\/([A-Za-z0-9-]+)/)))
    return {name: "job", id: m[1]};
  if (h === "#/history") return {name: "history"};
  return {name: "home"};
}

async function render() {
  clearViewTimers();
  if (!TOKEN) return showGate();
  const r = currentRoute();
  try {
    if (r.name === "tool") return await renderTool(r.id, r.cmd);
    if (r.name === "job") return await renderJob(r.id);
    if (r.name === "history") return await renderHistory();
    return await renderHome();
  } catch (e) {
    if (e instanceof GateError) return;
    main.replaceChildren(el("div", {class: "sheet sheet-pad"}, [
      el("p", {class: "error-text", text: e.message || String(e)}),
      el("button", {type: "button", text: "重试", onclick: render}),
    ]));
  }
}

// ------------------------------------------------------------- 首页
function jobRow(j, {ops = true} = {}) {
  const tr = el("tr", {onclick: () => { location.hash = `#/job/${j.id}`; }}, [
    el("td", {class: "jid", text: j.id}),
    el("td", {text: toolTitle(j.tool)}),
    el("td", {class: "tnum", text: j.command_title || j.command}),
    el("td", {}, [stateChip(j.state)]),
    el("td", {class: "tnum", text: fmtTimeShort(j.started)}),
    el("td", {class: "tnum", text: fmtDur(j.started, j.finished)}),
  ]);
  if (ops) {
    tr.append(el("td", {class: "ops"}, [
      isTerminal(j) ? el("button", {
        type: "button", text: "再次运行",
        onclick: (ev) => { ev.stopPropagation(); rerun(j); }}) : null,
    ]));
  }
  return tr;
}

function histTable(jobs, ops) {
  const cols = ["任务标识", "工具", "命令", "状态", "开始时间", "耗时"];
  if (ops) cols.push("");
  return el("div", {class: "tablewrap"}, [el("table", {class: "hist"}, [
    el("thead", {}, [el("tr", {}, cols.map(c => el("th", {text: c})))]),
    el("tbody", {}, jobs.map(j => jobRow(j, {ops}))),
  ])]);
}

function rerun(j) {
  sessionStorage.setItem("perfbox.rerun", JSON.stringify({
    tool: j.tool, command: j.command, values: j.values || {}}));
  location.hash = `#/tool/${j.tool}`;
}

async function renderHome() {
  await refreshAll();
  const kids = [
    el("p", {class: "muted", text: "从左侧选择一个工具开始"}),
  ];
  const recent = S.jobs.slice(0, 8);
  const sheet = el("div", {class: "sheet"});
  if (recent.length) {
    sheet.append(histTable(recent, true));
  } else {
    sheet.append(el("div", {class: "empty",
                            text: "还没有任务，选个工具提交第一个任务。"}));
  }
  kids.push(sheet);
  main.replaceChildren(...kids);
}

// ------------------------------------------------------------- 工具页
async function manifestFor(id) {
  if (!S.manifestCache.has(id)) {
    const d = await api(`/api/tools/${id}`);
    S.manifestCache.set(id, d);
  }
  return S.manifestCache.get(id);
}

async function renderTool(id, cmdId) {
  let data;
  try {
    data = await manifestFor(id);
  } catch (e) {
    if (e instanceof GateError) return;
    main.replaceChildren(el("div", {class: "sheet sheet-pad"}, [
      el("p", {class: "error-text", text: e.message}),
      el("button", {type: "button", text: "重试", onclick: render}),
    ]));
    return;
  }
  const m = data.manifest;
  const commands = m.commands || [];
  let cmd = commands.find(c => c.id === cmdId) ||
            (cmdId ? null : commands[0]);
  // 用相同参数再次运行：跳转时带上命令 id
  const stash = JSON.parse(sessionStorage.getItem("perfbox.rerun") || "null");
  if (stash && stash.tool === id && stash.command &&
      stash.command !== (cmd && cmd.id) &&
      commands.some(c => c.id === stash.command)) {
    location.hash = `#/tool/${id}/${stash.command}`;
    return;   // hashchange 会再渲染一次
  }
  if (stash && stash.tool === id && stash.command &&
      stash.command !== (cmd && cmd.id) &&
      !commands.some(c => c.id === stash.command))
    sessionStorage.removeItem("perfbox.rerun");   // 命令已不存在，丢掉
  if (!cmd) cmd = commands[0];

  const kids = [];
  const head = el("div", {class: "page-head"}, [
    el("h1", {}, [
      el("span", {class: "stripe",
                  style: `background:${DOMAIN_COLORS[m.domain] || "#999"}`}),
      m.title || id,
    ]),
    m.summary ? el("div", {class: "summary", text: m.summary}) : null,
    el("div", {class: "meta", text: data.installed
        ? `已安装 ${data.installed}`
        : "未安装，提交任务时会自动下载安装"}),
  ]);
  kids.push(head);

  if (commands.length > 1) {
    kids.push(el("div", {class: "segmented"}, commands.map(c =>
      el("a", {href: `#/tool/${id}/${c.id}`,
               class: c === cmd ? "active" : "", text: c.title}))));
  }

  if (cmd.kind === "app") {
    kids.push(el("div", {class: "sheet sheet-pad"}, [
      el("h2", {text: cmd.title, style: "font-size:16px"}),
      cmd.summary ? el("p", {class: "muted", text: cmd.summary}) : null,
      el("p", {}, [el("button", {type: "button", disabled: true,
                                 text: "打开"})]),
      el("p", {class: "muted small",
               text: "这个命令会打开工具自带的界面，下一版 perfbox 支持。"}),
    ]));
    main.replaceChildren(...kids);
    return;
  }

  // 任务型：表单
  const form = buildForm(cmd, {pick});
  if (stash && stash.tool === id && stash.command === cmd.id) {
    form.setValues(stash.values);
    sessionStorage.removeItem("perfbox.rerun");
  }
  const sheet = el("div", {class: "sheet"});
  sheet.append(form.root);

  // 结果目录行
  const odInput = el("input", {type: "text", "aria-label": "结果目录"});
  const odExtra = el("div", {hidden: true}, [
    el("div", {class: "pathline", style: "margin-top:6px"}, [
      odInput,
      el("button", {type: "button", text: "浏览目录", onclick: async (ev) => {
        const b = ev.currentTarget; b.disabled = true;
        try {
          const paths = await pick("dir", false, null);
          if (paths.length) odInput.value = paths[0];
        } catch { /* 取消 */ } finally { b.disabled = false; }
      }}),
    ]),
  ]);
  const odToggle = el("button", {type: "button", text: "改到别处",
    onclick: () => {
      const show = odExtra.hidden;
      odExtra.hidden = !show;
      odToggle.textContent = show ? "用默认位置" : "改到别处";
      if (!show) odInput.value = "";
    }});
  sheet.append(el("div", {class: "formrow"}, [
    el("div", {class: "flabel", text: "结果目录"}),
    el("div", {class: "field"}, [
      el("div", {class: "pathline"}, [
        el("input", {type: "text", readonly: true, class: "mono",
                     value: `${S.state ? S.state.workdir : ""}\\out\\<任务标识>`}),
        odToggle,
      ]),
      odExtra,
    ]),
  ]));

  // 提交行
  const errEl = el("div", {class: "err", hidden: true});
  const btnText = "开始任务";
  const busyText = data.installed ? "正在提交…"
                                  : `正在下载并安装 ${m.title || id}…`;
  const submit = el("button", {type: "button", class: "primary",
                               text: btnText});
  submit.addEventListener("click", async () => {
    errEl.hidden = true;
    if (!form.validate()) return;
    const body = {tool: id, command: cmd.id, values: form.values()};
    if (!odExtra.hidden && odInput.value.trim())
      body.out_dir = odInput.value.trim();
    submit.disabled = true;
    submit.textContent = busyText;
    try {
      const job = await api("/api/jobs", {method: "POST", body});
      location.hash = `#/job/${job.id}`;
    } catch (e) {
      if (!(e instanceof GateError)) {
        errEl.textContent = e.message;
        errEl.hidden = false;
      }
      submit.disabled = false;
      submit.textContent = btnText;
    }
  });
  sheet.append(el("div", {class: "submitline"}, [
    errEl,
    el("div", {class: "actions"}, [submit]),
  ]));
  kids.push(sheet);
  main.replaceChildren(...kids);
}

// ------------------------------------------------------------- 任务页
async function renderJob(id) {
  const poll = async (logOffset) =>
    api(`/api/jobs/${id}?log_offset=${logOffset}`);

  // 票据骨架
  const jidEl = el("div", {class: "jid", text: id});
  const chipSlot = el("span");
  const subEl = el("div", {class: "sub"});
  const tStart = el("span", {class: "num"});
  const tEnd = el("span", {class: "num"});
  const tDur = el("span", {class: "num"});
  const outdirEl = el("div", {class: "outdir"});
  const actionsEl = el("div", {class: "actions"});
  const failEl = el("div", {class: "failnote", hidden: true});
  const missEl = el("div", {class: "missing", hidden: true});
  const logPre = el("pre");
  const resultsEl = el("div", {class: "results", hidden: true});
  const cmdDet = el("details", {class: "cmdline"}, [
    el("summary", {text: "命令行"}),
    el("code"),
  ]);

  const ticket = el("div", {class: "sheet"}, [
    el("div", {class: "ticket"}, [
      el("div", {class: "headline"}, [jidEl, chipSlot]),
      subEl,
      el("div", {class: "times"}, [
        el("div", {}, [el("span", {class: "k", text: "开始"}), tStart]),
        el("div", {}, [el("span", {class: "k", text: "结束"}), tEnd]),
        el("div", {}, [el("span", {class: "k", text: "耗时"}), tDur]),
      ]),
      outdirEl,
      actionsEl,
    ]),
    failEl, missEl,
    el("div", {class: "logpanel"}, [el("h2", {text: "日志"}), logPre]),
    resultsEl,
    cmdDet,
  ]);
  main.replaceChildren(ticket);

  let logOffset = 0;
  let lastJob = null;
  let done = false;

  async function cmdTitle(j) {
    try {
      const d = await manifestFor(j.tool);
      const c = (d.manifest.commands || []).find(c => c.id === j.command);
      return (c && c.title) || j.command;
    } catch { return j.command; }
  }

  function updateStatic(j) {
    chipSlot.replaceChildren(stateChip(j.state));
    tStart.textContent = fmtTime(j.started);
    tEnd.textContent = fmtTime(j.finished);
    tDur.textContent = fmtDur(j.started, j.finished);
    outdirEl.replaceChildren(
      el("span", {class: "muted", text: "结果目录"}),
      el("span", {class: "mono", text: j.out_dir}),
      copyBtn(j.out_dir));
    const acts = [];
    if (!isTerminal(j)) {
      acts.push(el("button", {type: "button", text: "取消任务",
        onclick: async () => {
          if (!window.confirm("取消这个任务？")) return;
          try { await api(`/api/jobs/${j.id}/cancel`, {method: "POST"}); }
          catch (e) { /* 状态会在下一次轮询刷新 */ }
        }}));
    } else {
      acts.push(el("button", {type: "button", text: "用相同参数再次运行",
                              onclick: () => rerun(j)}));
      const primary = (j.outputs || []).find(o => o.primary);
      if (primary && !j.result_missing)
        acts.push(el("button", {type: "button",
          text: "在资源管理器中显示", onclick: async () => {
            try {
              await api("/api/reveal", {method: "POST",
                body: {job: j.id, path: primary.path}});
            } catch { /* 资源管理器打不开时静默 */ }
          }}));
    }
    actionsEl.replaceChildren(...acts);

    // 命令行
    const argv = (j.argv || []).map(a =>
      /\s/.test(a) ? `"${a}"` : a).join(" ");
    cmdDet.querySelector("code").textContent = argv;

    failEl.hidden = true;
    if (j.state === "failed") {
      failEl.textContent = j.error ||
        (j.exit_code != null
          ? `任务失败（退出码 ${j.exit_code}），日志见上方`
          : "任务失败，日志见上方");
      failEl.hidden = false;
    } else if (j.error) {
      failEl.textContent = j.error;
      failEl.hidden = false;
    }
    missEl.hidden = true;
    if (j.result_missing) {
      missEl.textContent = "结果已不在原位置";
      missEl.hidden = false;
    }
  }

  function updateResults(j) {
    if (!isTerminal(j)) return;
    resultsEl.hidden = false;
    const kids = [el("h2", {text: "结果"})];
    const outs = j.outputs || [];
    const primary = outs.find(o => o.primary);
    if (primary && !j.result_missing)
      kids.push(el("div", {class: "primary-open"}, [
        el("a", {class: "", href: fileUrl(j, primary.path),
                 target: "_blank", rel: "noopener"}, [
          el("button", {type: "button", class: "primary",
                        text: "打开主报告"})])]));
    if (!outs.length && !j.result_missing)
      kids.push(el("div", {class: "muted", text: "没有匹配到结果文件"}));
    for (const o of outs) {
      const ops = j.result_missing ? [] : [
        el("a", {href: fileUrl(j, o.path), target: "_blank",
                 rel: "noopener", text: "打开"}),
        el("a", {href: "javascript:void 0", text: "在资源管理器中显示",
          onclick: async (ev) => {
            ev.preventDefault();
            try {
              await api("/api/reveal", {method: "POST",
                                        body: {job: j.id, path: o.path}});
            } catch { /* 静默 */ }
          }}),
      ];
      kids.push(el("div", {class: "outrow"}, [
        el("span", {class: "role", text: ROLE_TEXT[o.role] || "文件"}),
        el("span", {class: "p", text: o.path}),
        el("span", {class: "ops"}, ops),
      ]));
    }
    resultsEl.replaceChildren(...kids);
  }

  async function tick() {
    let j;
    try {
      j = await poll(logOffset);
    } catch (e) {
      if (e instanceof GateError) return;
      if (e.status === 404) {
        done = true;
        main.replaceChildren(el("div", {class: "sheet sheet-pad"},
          [el("p", {class: "error-text", text: e.message})]));
        return;
      }
      return;   // 暂时失败，下一轮再试
    }
    logOffset = j.log_offset;
    if (j.log) {
      const atBottom = logPre.scrollTop + logPre.clientHeight >=
                       logPre.scrollHeight - 8;
      logPre.textContent += j.log;
      if (atBottom) logPre.scrollTop = logPre.scrollHeight;
    }
    const wasRunning = lastJob && !isTerminal(lastJob);
    lastJob = j;
    updateStatic(j);
    if (isTerminal(j)) {
      updateResults(j);
      if (!done) {
        done = true;
        // 终态后再取一次，拿齐结尾日志
        viewAfter(400, async () => {
          try {
            const last = await poll(logOffset);
            logOffset = last.log_offset;
            if (last.log) {
              const atB = logPre.scrollTop + logPre.clientHeight >=
                          logPre.scrollHeight - 8;
              logPre.textContent += last.log;
              if (atB) logPre.scrollTop = logPre.scrollHeight;
            }
            lastJob = last;
            updateStatic(last);
            updateResults(last);
          } catch { /* 收尾失败就算了 */ }
        });
      }
      return;
    }
    if (!done) viewAfter(700, tick);
  }

  // 副标题（工具名 + 命令名）异步补
  try {
    const j0 = await poll(0);
    lastJob = j0;
    logOffset = j0.log_offset;
    if (j0.log) logPre.textContent = j0.log;
    updateStatic(j0);
    if (isTerminal(j0)) updateResults(j0);
    subEl.textContent = `${toolTitle(j0.tool)} · ${await cmdTitle(j0)}`;
    // 标题加载失败也不影响
    if (!isTerminal(j0)) {
      // 运行中的耗时跳动
      const durTimer = setInterval(() => {
        if (lastJob && !isTerminal(lastJob) && lastJob.started)
          tDur.textContent = fmtDur(lastJob.started, null);
      }, 1000);
      const durCleaner = setInterval(() => {
        if (!document.body.contains(ticket)) {
          clearInterval(durTimer); clearInterval(durCleaner);
        }
      }, 2000);
      viewTimers.push(durTimer, durCleaner);
      viewAfter(700, tick);
    } else {
      done = true;
    }
  } catch (e) {
    if (e instanceof GateError) return;
    main.replaceChildren(el("div", {class: "sheet sheet-pad"}, [
      el("p", {class: "error-text", text: e.message || String(e)}),
    ]));
  }
}

// ------------------------------------------------------------- 历史页
async function renderHistory() {
  await refreshAll();
  const kids = [
    el("div", {class: "page-head"}, [el("h1", {text: "历史任务"})]),
  ];
  const sheet = el("div", {class: "sheet"});
  if (S.jobs.length) sheet.append(histTable(S.jobs, true));
  else sheet.append(el("div", {class: "empty",
                             text: "还没有任务记录。"}));
  kids.push(sheet);
  main.replaceChildren(...kids);
}

// ------------------------------------------------------------- 启动
window.addEventListener("hashchange", () => { render(); });
async function boot() {
  if (!TOKEN) return showGate();
  await refreshAll();
  await render();
  setInterval(async () => {
    await refreshAll();
    // 侧栏徽标更新；首页/历史页停留时也顺带刷新
    const r = currentRoute();
    if (r.name === "home") renderHome();
  }, 5000);
}
boot();

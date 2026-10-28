/* Local UI. The token is fetched from the loopback bootstrap route and kept in
   sessionStorage. It is never put in the URL. */

const TOKEN_KEY = "tgdl.apiToken";
const TABS = ["status", "chats", "scan", "download", "jobs"];

const TASK_STATUS = {
  waiting: "等待",
  active: "下载中",
  paused: "已暂停",
  complete: "完成",
  error: "失败",
  removed: "已移除",
};

const FILE_STATE = {
  listed: "已列出",
  pending: "等待",
  active: "下载中",
  complete: "完成",
  skipped: "跳过",
  failed: "失败",
  partial: "未完成，可续传",
};

const PLAN = {
  download: "将下载",
  resume: "续传",
  finish: "半成品已齐",
  skip: "已存在，将跳过",
};

const KIND = {
  group: "群组",
  channel: "频道",
  user: "私聊",
  chat: "会话",
  "list-chats": "读取群组",
  scan: "扫描",
  download: "下载",
};

const state = {
  token: "",
  targets: [],
  chats: [],
  chatsLoaded: false,
  jobs: [],
  detail: null,
  downloadJob: null,
  ready: false,
};

let jobsWatch = 0;

const loops = { chats: 0, scan: 0, download: 0, jobs: 0 };

function $(id) {
  return document.getElementById(id);
}

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([key, value]) => {
    if (value == null || value === false) return;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  });
  (children || []).forEach((child) => {
    if (child == null || child === false) return;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  });
  return node;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function field(id) {
  const node = $(id);
  return node ? node.value.trim() : "";
}

function notify(text, kind) {
  const node = $("notice");
  if (!text) {
    node.hidden = true;
    node.textContent = "";
    node.className = "notice";
    return;
  }
  node.hidden = false;
  node.className = kind ? `notice ${kind}` : "notice";
  node.textContent = text;
}

function formatBytes(value) {
  if (value == null || value === "") return "—";
  let number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  while (number >= 1024 && index < units.length - 1) {
    number /= 1024;
    index += 1;
  }
  const digits = index === 0 ? 0 : 1;
  return `${number.toFixed(digits)} ${units[index]}`;
}

function formatWhen(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace("T", " ").slice(0, 16);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function label(map, key) {
  if (!key) return "—";
  return map[key] || key;
}

function taskOf(job) {
  if (job && job.task && typeof job.task === "object") return job.task;
  return {
    id: job && job.id,
    status: "waiting",
    done: false,
    ok: false,
    name: (job && job.id) || "任务",
    dir: "",
    files: [],
    totalBytes: job && job.bytesTotal,
    doneBytes: (job && job.bytesWritten) || 0,
    progress: 0,
    speedBps: 0,
    error: job && job.error,
    source: "",
    addedBy: "agent",
    resuming: false,
    etaSec: null,
  };
}

function statusText(task, job) {
  if (task && task.resuming && task.status === "active") return "续传中";
  if (job && job.kind === "scan" && task && task.status === "active") return "扫描中";
  if (job && job.kind === "list-chats" && task && task.status === "active") return "读取中";
  return label(TASK_STATUS, task && task.status);
}

function formatSpeed(bps) {
  const number = Number(bps);
  const value = Number.isFinite(number) && number > 0 ? number : 0;
  if (value < 1024) return `${Math.round(value)} B/s`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB/s`;
  return `${(value / 1024 / 1024).toFixed(2)} MB/s`;
}

function formatPercent(progress) {
  const number = Number(progress);
  if (!Number.isFinite(number)) return "—";
  return `${Math.round(number * 100)}%`;
}

function formatEta(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return "—";
  let left = Math.max(0, Math.round(Number(seconds)));
  if (left < 60) return `${left} 秒`;
  const minutes = Math.floor(left / 60);
  const secs = left % 60;
  if (minutes < 60) return `${minutes} 分 ${secs} 秒`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时 ${minutes % 60} 分`;
}

function pill(text, kind) {
  return el("span", { class: kind ? `pill ${kind}` : "pill", text });
}

async function bootstrap() {
  const response = await fetch("/api/v1/ui/bootstrap", {
    headers: { "X-Tgdl-Ui": "1", Accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    const cached = sessionStorage.getItem(TOKEN_KEY);
    if (cached) return cached;
    throw new Error("无法取得本机令牌，请确认是从 127.0.0.1 打开的页面");
  }
  const body = await response.json();
  if (!body || !body.token) throw new Error("令牌响应不完整");
  sessionStorage.setItem(TOKEN_KEY, body.token);
  return body.token;
}

async function api(path, options) {
  const opts = options || {};
  const headers = {
    Accept: "application/json",
    Authorization: `Bearer ${state.token}`,
  };
  if (opts.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: opts.method || "GET",
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    cache: "no-store",
  });
  let data = {};
  try {
    data = await response.json();
  } catch (_err) {
    data = {};
  }
  if (response.status === 401) {
    sessionStorage.removeItem(TOKEN_KEY);
    throw Object.assign(new Error("令牌失效，请刷新页面"), { status: 401, data });
  }
  return { status: response.status, data };
}

function jobError(result) {
  const data = result.data || {};
  const message = data.error || `请求没有完成（HTTP ${result.status}）`;
  return data.jobId ? `${message}（任务 ${data.jobId}）` : message;
}

async function follow(slot, job, render) {
  const mark = ++loops[slot];
  let current = job;
  render(current);
  while (current && !current.done) {
    await sleep(500);
    if (loops[slot] !== mark) return null;
    const result = await api(`/api/v1/task?id=${encodeURIComponent(current.id)}`);
    if (!result.data.job) throw new Error(jobError(result));
    current = result.data.job;
    render(current);
  }
  return current;
}

function show(tab) {
  if (!TABS.includes(tab)) tab = "status";
  TABS.forEach((name) => {
    const view = $(`view-${name}`);
    const button = $(`tab-${name}`);
    const on = name === tab;
    view.hidden = !on;
    button.classList.toggle("on", on);
    button.setAttribute("aria-selected", on ? "true" : "false");
  });
  if (location.hash !== `#${tab}`) history.replaceState(null, "", `#${tab}`);
  if (tab === "download") renderTargets($("download-targets"));
  if (state.ready && tab === "jobs") refreshJobs().catch(fail);
}

function renderAccount(info) {
  const box = $("account");
  const help = $("login-help");
  box.replaceChildren();
  if (!info) {
    box.append(el("p", { text: "正在检查会话…" }));
    help.hidden = true;
    $("who").textContent = "正在连接本机服务…";
    return;
  }
  if (info.loggedIn) {
    const name = info.displayName || (info.username ? `@${info.username}` : info.userId);
    const who = info.username ? `${name} · @${info.username}` : name;
    $("who").textContent = `已登录 · ${who}`;
    box.append(el("p", { class: "title", text: "已登录" }));
    box.append(el("p", { text: who || "当前账号" }));
    if (info.userId) box.append(el("p", { class: "id", text: `id ${info.userId}` }));
    help.hidden = true;
    return;
  }
  $("who").textContent = "尚未登录";
  box.append(el("p", { class: "title", text: "尚未登录" }));
  box.append(el("p", { text: "读取群组和下载都需要先在终端完成 tgdl login。" }));
  help.hidden = false;
  $("login-reason").textContent = info.reason || "";
}

async function loadAccount() {
  const result = await api("/api/v1/account");
  if (!result.data || result.data.ok !== true) throw new Error(jobError(result));
  renderAccount(result.data);
  return result.data;
}

async function loadVersion() {
  const result = await api("/api/v1/ping");
  if (result.status === 200 && result.data.version) {
    $("version").textContent = `tgdl ${result.data.version}`;
  }
}

function selectedMap() {
  return new Set(state.targets.map((item) => item.target));
}

function renderTargets(root) {
  root.replaceChildren();
  if (!state.targets.length) {
    root.append(el("span", { class: "muted", text: "还没有目标" }));
    return;
  }
  state.targets.forEach((item) => {
    const remove = el("button", { type: "button", text: "移除", "aria-label": `移除 ${item.title}` });
    remove.addEventListener("click", () => {
      state.targets = state.targets.filter((other) => other.target !== item.target);
      renderTargets($("chips"));
      renderTargets($("download-targets"));
      renderChats();
    });
    root.append(el("span", { class: "chip" }, [item.title, remove]));
  });
}

function addTarget(target, title) {
  const value = String(target || "").trim();
  if (!value) return;
  if (state.targets.some((item) => item.target === value)) return;
  state.targets.push({ target: value, title: title || value });
  renderTargets($("chips"));
  renderTargets($("download-targets"));
  renderChats();
}

function renderChats() {
  if (!state.chatsLoaded) return;
  const root = $("chat-list");
  const query = field("chat-search").toLowerCase();
  const rows = state.chats.filter((chat) => {
    if (!query) return true;
    const hay = `${chat.title || ""} ${chat.username || ""} ${chat.id || ""}`.toLowerCase();
    return hay.includes(query);
  });
  root.replaceChildren();
  if (!state.chats.length) {
    root.append(el("p", { class: "empty", text: "没有对话。确认已经登录，或放宽「只看群组和频道」。" }));
    return;
  }
  if (!rows.length) {
    root.append(el("p", { class: "empty", text: "没有符合查找条件的对话。" }));
    return;
  }
  const chosen = selectedMap();
  rows.forEach((chat) => {
    const target = String(chat.id);
    const on = chosen.has(target);
    const title = chat.title || target;
    const user = chat.username ? `@${chat.username}` : "无用户名";
    const button = el("button", { type: "button", class: on ? "chat on" : "chat" }, [
      pill(on ? "已选" : "选用", on ? "complete" : ""),
      el("span", {}, [
        el("span", { class: "title", text: title }),
        document.createElement("br"),
        el("small", { text: `${label(KIND, chat.kind)} · ${user}` }),
      ]),
      el("span", { class: "id", text: target }),
    ]);
    button.addEventListener("click", () => {
      if (on) state.targets = state.targets.filter((item) => item.target !== target);
      else addTarget(target, title);
      if (on) {
        renderTargets($("chips"));
        renderTargets($("download-targets"));
        renderChats();
      }
    });
    root.append(button);
  });
}

function filters(extra) {
  const chats = state.targets.map((item) => item.target);
  if (!chats.length) throw new Error("请先在「群组」里选用一个群，或添加 @用户名 / 链接");
  const body = Object.assign({ chats, wait: false, addedBy: "user" }, extra || {});
  const ext = field("f-ext");
  if (ext) body.ext = ext.split(/[,，\s]+/).filter(Boolean);
  const name = field("f-name");
  if (name) body.name = name;
  const minSize = field("f-min");
  if (minSize) body.minSize = minSize;
  const maxSize = field("f-max");
  if (maxSize) body.maxSize = maxSize;
  const since = field("f-since");
  if (since) body.since = since;
  const until = field("f-until");
  if (until) body.until = until;
  return body;
}

function countsLine(job) {
  const counts = job.counts || {};
  return `匹配 ${counts.matched || 0} · 下载 ${counts.downloaded || 0} · 跳过 ${counts.skipped || 0} · 失败 ${counts.failed || 0} · 未完成 ${counts.partial || 0} · 进行中 ${counts.active || 0}`;
}

function progressBits(job) {
  const task = taskOf(job);
  const wrap = el("div");
  wrap.append(el("p", { class: "title", text: task.name || job.id || "任务" }));
  const head = el("p", {}, [
    pill(statusText(task, job), task.status),
    " ",
    el("span", { class: "id", text: task.id || job.id || "" }),
  ]);
  wrap.append(head);
  const pct = Math.max(0, Math.min(100, Math.round(Number(task.progress || 0) * 100)));
  const bar = el("div", { class: "bar" }, [el("span")]);
  bar.firstChild.style.width = `${pct}%`;
  wrap.append(bar);
  const totalText = task.totalBytes ? formatBytes(task.totalBytes) : "大小未知";
  wrap.append(el("p", { text: `${formatPercent(task.progress)} · ${formatBytes(task.doneBytes)} / ${totalText}` }));
  wrap.append(el("p", { text: `速度 ${formatSpeed(task.speedBps)} · 剩余 ${formatEta(task.etaSec)}` }));
  if (task.source) wrap.append(el("p", { class: "meta", text: `来源 ${task.source}` }));
  if (task.dir) wrap.append(el("p", { class: "meta", text: `目录 ${task.dir}` }));
  wrap.append(el("p", { class: "meta", text: countsLine(job) }));
  if (task.status === "paused") {
    wrap.append(el("p", { text: "已暂停。半成品还在，点「继续」会接着下。" }));
  }
  if (task.error && task.status !== "complete") wrap.append(el("p", { text: task.error }));
  return wrap;
}

function renderOverall() {
  const node = $("overall");
  const byId = new Map(state.jobs.map((job) => [job.id, job]));
  if (state.downloadJob) byId.set(state.downloadJob.id, state.downloadJob);
  const jobs = [...byId.values()];
  const active = jobs.filter((job) => taskOf(job).status === "active");
  if (!active.length) {
    node.hidden = true;
    node.replaceChildren();
    return;
  }
  let done = 0;
  let total = 0;
  let speed = 0;
  let known = true;
  active.forEach((job) => {
    const task = taskOf(job);
    done += Number(task.doneBytes) || 0;
    speed += Number(task.speedBps) || 0;
    if (!task.totalBytes) known = false;
    else total += Number(task.totalBytes) || 0;
  });
  const progress = known && total > 0 ? Math.min(1, done / total) : 0;
  const pct = Math.round(progress * 100);
  node.hidden = false;
  const bar = el("div", { class: "bar" }, [el("span")]);
  bar.firstChild.style.width = `${pct}%`;
  node.replaceChildren(
    el("strong", { text: `总进度 ${active.length} 个任务` }),
    bar,
    el("span", { text: `${formatPercent(progress)} · ${formatBytes(done)} / ${known ? formatBytes(total) : "大小未知"}` }),
    el("span", { text: formatSpeed(speed) }),
  );
}

function fileRows(files) {
  const all = files || [];
  const shown = all.slice(0, 300);
  const table = el("table", {}, [
    el("thead", {}, [
      el("tr", {}, ["文件", "大小", "时间", "群", "状态", "计划"].map((name) => el("th", { text: name }))),
    ]),
  ]);
  const body = el("tbody");
  shown.forEach((file) => {
    const plan = file.plannedAction;
    const stateName = file.state;
    body.append(
      el("tr", {}, [
        el("td", { class: "name", text: file.fileName || "未命名" }),
        el("td", { class: "num", text: formatBytes(file.fileSize) }),
        el("td", { class: "num", text: formatWhen(file.date) }),
        el("td", { text: file.chatTitle || "" }),
        el("td", {}, [pill(label(FILE_STATE, stateName), stateName)]),
        el("td", {}, [plan ? pill(label(PLAN, plan), plan === "resume" ? "partial" : plan === "skip" ? "skipped" : "") : "—"]),
      ]),
    );
  });
  table.append(body);
  const wrap = el("div");
  wrap.append(table);
  if (all.length > shown.length) {
    wrap.append(el("p", { class: "muted", text: `只显示前 ${shown.length} 条，共 ${all.length} 条。` }));
  }
  return wrap;
}

function renderScan(job) {
  const summary = $("scan-summary");
  const files = $("scan-files");
  if (!job) {
    summary.replaceChildren();
    files.replaceChildren();
    return;
  }
  summary.replaceChildren(progressBits(job));
  const resume = (job.files || []).filter((file) => file.plannedAction === "resume").length;
  const skip = (job.files || []).filter((file) => file.plannedAction === "skip" || file.plannedAction === "finish").length;
  if (resume || skip) {
    summary.append(el("p", { text: `其中 ${resume} 个可续传，${skip} 个本地已有、下载时会跳过或直接归位。` }));
  }
  files.replaceChildren(job.files && job.files.length ? fileRows(job.files) : el("p", { class: "empty", text: job.done ? "没有匹配的文件。" : "正在列出文件…" }));
}

function renderDownload(job) {
  const summary = $("download-progress");
  const files = $("download-files");
  state.downloadJob = job || null;
  const task = job ? taskOf(job) : null;
  const pause = $("download-pause");
  const resume = $("download-resume");
  const remove = $("download-remove");
  if (!job || !task) {
    summary.replaceChildren();
    files.replaceChildren();
    pause.hidden = true;
    resume.hidden = true;
    remove.hidden = true;
    renderOverall();
    return;
  }
  summary.replaceChildren(progressBits(job));
  files.replaceChildren(job.files && job.files.length ? fileRows(job.files) : el("p", { class: "empty", text: "还没有文件记录。" }));
  pause.hidden = task.status !== "active";
  resume.hidden = !(task.status === "paused" || task.status === "error");
  remove.hidden = task.status === "removed";
  pause.dataset.id = job.id || "";
  resume.dataset.id = job.id || "";
  remove.dataset.id = job.id || "";
  renderOverall();
}

function controlButtons(job) {
  const task = taskOf(job);
  const row = el("div", { class: "actions" });
  if (task.status === "active") {
    const pause = el("button", { type: "button", text: "暂停" });
    pause.addEventListener("click", (event) => {
      event.stopPropagation();
      pauseJob(job.id).catch(fail);
    });
    row.append(pause);
  }
  if (task.status === "paused" || task.status === "error") {
    const resume = el("button", { type: "button", class: "primary", text: "继续" });
    resume.addEventListener("click", (event) => {
      event.stopPropagation();
      resumeJob(job.id).catch(fail);
    });
    row.append(resume);
  }
  if (task.status !== "removed") {
    const remove = el("button", { type: "button", text: "移除" });
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      removeJob(job.id).catch(fail);
    });
    row.append(remove);
  }
  return row;
}

function renderJobs() {
  const root = $("job-list");
  root.replaceChildren();
  renderOverall();
  if (!state.jobs.length) {
    root.append(el("p", { class: "empty", text: "还没有任务。" }));
    return;
  }
  const table = el("table", { class: "task-table" });
  table.append(el("thead", {}, [
    el("tr", {}, ["名称", "状态", "进度", "已下 / 总大小", "速度", "错误", "操作"].map((name) => el("th", { text: name }))),
  ]));
  const body = el("tbody");
  state.jobs.forEach((job) => {
    const task = taskOf(job);
    const name = el("button", { type: "button", text: task.name || job.id });
    name.addEventListener("click", () => showJob(job.id));
    body.append(el("tr", {}, [
      el("td", { class: "name" }, [name, el("div", { class: "meta", text: task.source || "" })]),
      el("td", {}, [pill(statusText(task, job), task.status)]),
      el("td", { class: "num", text: formatPercent(task.progress) }),
      el("td", { class: "num", text: `${formatBytes(task.doneBytes)} / ${task.totalBytes ? formatBytes(task.totalBytes) : "—"}` }),
      el("td", { class: "num", text: formatSpeed(task.speedBps) }),
      el("td", { class: "err", text: task.error || "" }),
      el("td", {}, [controlButtons(job)]),
    ]));
  });
  table.append(body);
  root.append(table);
}

function showJob(id) {
  const job = state.jobs.find((item) => item.id === id) || state.detail;
  const root = $("job-detail");
  if (!job || job.id !== id) {
    root.replaceChildren();
    return;
  }
  state.detail = job;
  const task = taskOf(job);
  const card = el("article", { class: "card" }, [
    el("h3", { text: task.name || job.id }),
    progressBits(job),
    controlButtons(job),
  ]);
  if (job.files && job.files.length && job.files[0] && typeof job.files[0] === "object" && !Array.isArray(job.files[0])) {
    card.append(fileRows(job.files));
  }
  root.replaceChildren(card);
}

async function refreshJobs() {
  const result = await api("/api/v1/tasks?limit=50");
  if (result.status !== 200) throw new Error(jobError(result));
  state.jobs = result.data.tasks || [];
  renderJobs();
  if (state.detail) {
    const fresh = state.jobs.find((job) => job.id === state.detail.id);
    if (fresh) showJob(fresh.id);
  }
  if (state.jobs.some((job) => taskOf(job).status === "active") && !$("view-jobs").hidden) watchJobs();
}

function watchJobs() {
  const mark = ++jobsWatch;
  const tick = async () => {
    if (jobsWatch !== mark || $("view-jobs").hidden) return;
    await sleep(700);
    if (jobsWatch !== mark || $("view-jobs").hidden) return;
    try {
      await refreshJobs();
    } catch (error) {
      fail(error);
    }
  };
  tick();
}

async function runListed(path, body, slot, render) {
  const result = await api(path, { method: "POST", body });
  const job = result.data && result.data.job;
  if (!job) throw new Error(jobError(result));
  await follow(slot, job, render);
  try {
    await refreshJobs();
  } catch (_err) {
    /* listing tasks is optional after a job finishes */
  }
}

async function loadChats() {
  notify("");
  const limit = field("chat-limit") || "100";
  const groups = $("groups-only").checked ? "true" : "false";
  $("chat-list").replaceChildren(el("p", { class: "empty", text: "正在读取对话…" }));
  const result = await api(`/api/v1/chats?limit=${encodeURIComponent(limit)}&groupsOnly=${groups}&wait=false&addedBy=user`);
  const job = result.data && result.data.job;
  if (!job) throw new Error(jobError(result));
  const done = await follow("chats", job, (current) => {
    $("chat-list").replaceChildren(el("p", { class: "empty", text: current && current.done ? "正在整理列表…" : "正在读取对话…" }));
  });
  state.chatsLoaded = true;
  state.chats = (done && done.resolvedChats) || [];
  renderChats();
  notify(`读到 ${state.chats.length} 个对话`, "");
}

async function startScan() {
  notify("");
  renderScan(null);
  await runListed("/api/v1/scan", filters(), "scan", renderScan);
}

async function startDownload(body) {
  notify("");
  const payload = body ? Object.assign({}, body) : filters();
  if (!body) {
    const workers = field("f-workers");
    if (workers) payload.workers = Number(workers);
    const output = field("f-output");
    if (output) payload.output = output;
  }
  payload.wait = false;
  show("download");
  await runListed("/api/v1/download", payload, "download", renderDownload);
}

async function pauseJob(id) {
  const result = await api("/api/v1/pause", { method: "POST", body: { id } });
  if (!result.data.job) throw new Error(jobError(result));
  renderDownload(result.data.job);
  await refreshJobs();
  notify("已暂停。当前这一小块写完后会停，点「继续」接着下。", "warn");
}

async function resumeJob(id) {
  notify("");
  const stayOnJobs = !$("view-jobs").hidden;
  const result = await api("/api/v1/resume", { method: "POST", body: { id } });
  const job = result.data && result.data.job;
  if (!job) throw new Error(jobError(result));
  if (!stayOnJobs) show("download");
  await follow("download", job, (current) => {
    state.downloadJob = current;
    const index = state.jobs.findIndex((item) => item.id === current.id);
    if (index >= 0) state.jobs[index] = current;
    else state.jobs.unshift(current);
    renderDownload(current);
    if (!$("view-jobs").hidden) renderJobs();
  });
  await refreshJobs().catch(() => {});
}

async function removeJob(id) {
  const deleteFiles = Boolean($("delete-files") && $("delete-files").checked);
  const message = deleteFiles ? "移除这个任务，并删除已记录的文件？" : "移除这个任务？已下载的文件会留在磁盘上。";
  if (!window.confirm(message)) return;
  const result = await api("/api/v1/remove", { method: "POST", body: { id, deleteFiles } });
  if (!result.data.job) throw new Error(jobError(result));
  renderDownload(result.data.job);
  await refreshJobs();
  notify(deleteFiles ? "任务已移除，并尝试删除文件。" : "任务已移除。", "warn");
}

function bind() {
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      notify("");
      show(button.dataset.tab);
    });
  });
  $("load-chats").addEventListener("click", () => loadChats().catch(fail));
  $("chat-search").addEventListener("input", renderChats);
  $("target-add").addEventListener("click", () => {
    addTarget(field("target-input"), field("target-input"));
    $("target-input").value = "";
  });
  $("target-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      $("target-add").click();
    }
  });
  $("scan-go").addEventListener("click", () => startScan().catch(fail));
  $("scan-to-download").addEventListener("click", () => show("download"));
  $("download-go").addEventListener("click", () => startDownload().catch(fail));
  $("download-pause").addEventListener("click", () => {
    const id = $("download-pause").dataset.id;
    if (id) pauseJob(id).catch(fail);
  });
  $("download-resume").addEventListener("click", () => {
    const id = $("download-resume").dataset.id;
    if (id) resumeJob(id).catch(fail);
  });
  $("download-remove").addEventListener("click", () => {
    const id = $("download-remove").dataset.id;
    if (id) removeJob(id).catch(fail);
  });
  $("reload-jobs").addEventListener("click", () => refreshJobs().catch(fail));
}

function fail(error) {
  console.error(error);
  notify(error && error.message ? error.message : "出错了", "bad");
}

async function main() {
  bind();
  renderTargets($("chips"));
  const initial = location.hash.replace("#", "");
  show(TABS.includes(initial) ? initial : "status");
  try {
    state.token = await bootstrap();
    state.ready = true;
    await loadVersion().catch(() => {});
    await loadAccount();
    if (initial !== "jobs") await refreshJobs().catch((error) => console.error(error));
    else await refreshJobs();
  } catch (error) {
    fail(error);
  }
}

main();

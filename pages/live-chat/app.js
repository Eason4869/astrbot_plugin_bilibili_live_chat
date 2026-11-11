const $ = id => document.getElementById(id);
const bridge = window.AstrBotPluginView ?? window.AstrBotPluginPage;
const formFields = [...document.querySelectorAll("[data-field]")];
const tabs = { local: $("tab-local"), bili: $("tab-bili") };
const roomPhases = {
  disabled: "已停止", starting: "正在连接", listening: "监听中", retrying: "重试中",
  waiting_login: "待登录", waiting_config: "待配置", duplicate: "重复房间",
};
const state = {
  snapshot: null, baseline: {}, words: [], busy: false, revision: 0,
  roomRows: new Map(),
  visionProviders: [], speechProviders: [],
  qrToken: "", qrRevision: 0, qrPhase: "", qrDetail: "", qrTimer: 0, qrFailures: 0,
  statusTimer: 0, toastTimer: 0, disposed: false,
  check: null, checkBusy: false, checkTimer: 0, checkEpoch: 0,
};

async function call(endpoint, payload) {
  if (!bridge) throw new Error("请从 AstrBot 管理面板的插件视图打开此页面。");
  const result = payload === undefined
    ? await bridge.apiGet(endpoint)
    : await bridge.apiPost(endpoint, payload);
  if (result?.status === "error") throw new Error(result.message);
  return result?.status === "ok" && "data" in result ? result.data : result;
}

function notice(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3600);
}

function connection(message = "") {
  $("connection").textContent = message;
  $("connection").hidden = !message;
}

function tab(mode, focus = false) {
  for (const [id, button] of Object.entries(tabs)) {
    const selected = mode === id;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    $(id).hidden = !selected;
    if (selected && focus) button.focus();
  }
}

function controls() {
  const snapshot = state.snapshot;
  const locked = state.busy || !snapshot;
  $("reload").disabled = state.busy;
  $("configuration").disabled = locked;
  $("new-room").disabled = locked;
  $("add-room-button").disabled = locked;
  for (const button of $("rooms").querySelectorAll("[data-room-action]")) button.disabled = locked;
  for (const fieldset of $("rooms").querySelectorAll("fieldset")) fieldset.disabled = locked;
  $("new-qr").disabled = locked;
  $("use-local").disabled = locked;
  $("use-bili").disabled = locked || !snapshot?.account.bilibot.configured;
  $("memory").disabled = locked;
  $("open-selfcheck").disabled = !snapshot;
  $("start-selfcheck").disabled = locked || state.checkBusy || state.check?.running;
  $("selfcheck-room").disabled = state.checkBusy || state.check?.running;
}

function localPanel() {
  const account = state.snapshot?.account;
  const local = account?.local;
  const usesShared = account?.source === "bilibot";
  const qrActive = ["waiting", "scanned"].includes(state.qrPhase);
  $("qr").hidden = !qrActive;
  $("use-local").hidden = !usesShared || !local?.configured;
  if (usesShared) {
    $("new-qr").textContent = "切换并生成二维码";
  } else if (state.qrPhase === "expired" || state.qrPhase === "error" || qrActive) {
    $("new-qr").textContent = "重新生成";
  } else {
    $("new-qr").textContent = local?.configured ? "重新扫码" : "生成二维码";
  }
  const labels = {
    loading: ["正在生成二维码", "正在连接 B 站登录服务。"],
    waiting: ["等待扫码", "用 B 站 App 扫码，再在手机上确认。"],
    scanned: ["已扫码", "请在手机上确认登录。"],
    expired: ["二维码已过期", "重新生成后再用 B 站 App 扫码。"],
    error: ["扫码暂未完成", state.qrDetail],
  };
  const [title, hint] = labels[state.qrPhase] || (local?.configured
    ? ["独立账号已配置", "UID " + local.uid]
    : ["连接 B 站账号", "生成二维码后，用 B 站 App 扫码确认。"]);
  if ($("local-title").textContent !== title) $("local-title").textContent = title;
  const caption = qrActive && state.qrDetail ? state.qrDetail : hint;
  if ($("local-caption").textContent !== caption) $("local-caption").textContent = caption;
}

function showRooms(rooms) {
  const list = $("rooms");
  const ids = new Set(rooms.map(room => room.id));
  const checkRoom = $("selfcheck-room");
  if (JSON.stringify([...checkRoom.options].slice(1).map(option => option.value)) !== JSON.stringify([...ids])) {
    const selected = checkRoom.value;
    checkRoom.replaceChildren(new Option("共享默认配置", ""), ...rooms.map(room => new Option("房间 " + room.id, room.id)));
    checkRoom.value = ids.has(selected) ? selected : "";
  }
  const enabled = rooms.filter(room => room.enabled).length;
  $("room-summary").textContent = "已开启 " + enabled + " / " + rooms.length;
  $("rooms-empty").hidden = rooms.length > 0;
  for (const [id, row] of state.roomRows) {
    if (!ids.has(id)) {
      row.remove();
      state.roomRows.delete(id);
    }
  }
  rooms.forEach((room, index) => {
    let row = state.roomRows.get(room.id);
    if (!row) {
      row = $("room-template").content.firstElementChild.cloneNode(true);
      row.dataset.room = room.id;
      state.roomRows.set(room.id, row);
      setMediaForm(row, room.media.settings);
    }
    if (list.children[index] !== row) list.insertBefore(row, list.children[index] ?? null);
    const part = name => row.querySelector('[data-part="' + name + '"]');
    part("id").textContent = room.id;
    const phase = part("phase");
    const label = roomPhases[room.phase] || "正在连接";
    phase.textContent = room.enabled && room.phase !== "listening" ? "已开启 · " + label : label;
    phase.classList.toggle("live", room.phase === "listening");
    const canonical = part("canonical");
    canonical.hidden = !room.canonical || room.canonical === room.id;
    canonical.textContent = canonical.hidden ? "" : "实际房间号 " + room.canonical;
    part("received").textContent = room.received;
    part("replies").textContent = room.replies;
    part("window").textContent = room.remaining > 0 ? room.remaining + " 秒" : "未唤醒";
    const toggle = row.querySelector('[data-room-action="toggle"]');
    toggle.textContent = room.enabled ? "停止监听" : "开始监听";
    toggle.setAttribute("aria-label", toggle.textContent + "房间 " + room.id);
    toggle.classList.toggle("primary", !room.enabled);
    row.querySelector('[data-room-action="remove"]').setAttribute("aria-label", "移除房间 " + room.id);
    const issues = part("issues");
    issues.replaceChildren(...room.issues.map(message => {
      const item = document.createElement("li");
      item.textContent = message;
      return item;
    }));
    issues.hidden = !room.issues.length;
    showMedia(row, room);
  });
}

function setMediaOptions(form, settings) {
  renderOptions(form.elements.stt_provider, state.speechProviders, "使用 AstrBot 默认语音识别", settings.stt_provider);
  renderOptions(form.elements.vision_provider, state.visionProviders, "使用当前对话模型", settings.vision_provider);
}

function setMediaForm(row, settings) {
  const form = row.querySelector('[data-part="media-form"]');
  setMediaOptions(form, settings);
  for (const field of form.querySelectorAll("input[name]")) {
    if (field.type === "checkbox") field.checked = settings[field.name];
    else field.value = settings[field.name] ?? "";
  }
}

function mediaValues(form) {
  return Object.fromEntries([...form.querySelectorAll("[name]")].map(field => [
    field.name,
    field.type === "checkbox" ? field.checked :
      field.type === "number" ? (field.value === "" ? null : field.valueAsNumber) : field.value,
  ]));
}

function showMedia(row, room) {
  const { settings, transcripts, frames, issues } = room.media;
  const active = [settings.audio && "语音", settings.video && "视频"].filter(Boolean);
  const summary = row.querySelector('[data-part="media-summary"]');
  summary.textContent = active.length ? active.join(" · ") + (issues.length ? " · 有异常" : "已启用") : "未启用";
  const info = row.querySelector('[data-part="media-status"]');
  info.textContent = active.length
    ? (room.enabled ? "已转写 " + transcripts + " 段 · 已抽帧 " + frames + " 次" : "开启监听后开始识别。")
    : "附和沿用共享总开关与发送限流。";
  const list = row.querySelector('[data-part="media-issues"]');
  list.replaceChildren(...issues.map(message => {
    const item = document.createElement("li");
    item.textContent = message;
    return item;
  }));
  list.hidden = !issues.length;
}

function showStatus(snapshot) {
  state.snapshot = snapshot;
  const ffmpeg = snapshot.environment?.ffmpeg;
  $("ffmpeg-status").textContent = ffmpeg?.available
    ? "AstrBot 使用 " + ffmpeg.path
    : ffmpeg?.message || "刷新后查看 AstrBot 的 FFmpeg 查找结果。";
  const { rooms, account } = snapshot;
  showRooms(rooms);
  const shared = account.source === "bilibot";
  const selected = shared ? account.bilibot : account.local;
  $("account-state").textContent = selected.configured ? (shared ? "复用中" : "已配置") : "未连接";
  $("account-state").classList.toggle("live", selected.configured);
  $("bilibot-title").textContent = account.bilibot.configured ? "BiliBot 账号已配置" : "未找到可用账号";
  $("bilibot-caption").textContent = account.bilibot.configured
    ? "UID " + account.bilibot.uid
    : "请在同一个 AstrBot 实例启用 BiliBot 并登录。";
  $("use-bili").hidden = shared;
  $("memory").checked = account.memory_enabled;
  $("memory-caption").textContent = account.memory_available
    ? "可与登录来源分别设置。"
    : "BiliBot 记忆接口暂不可用，仍可使用本次直播的对话上下文。";
  localPanel();
  controls();
}

function showCheck(report) {
  state.check = report;
  const scope = report.room ? "房间 " + report.room : "共享默认配置";
  $("selfcheck-phase").textContent = report.running ? "正在检查 · " + scope : report.items.length ? "检查完成 · " + scope : "尚未检查";
  if (report.running) $("selfcheck-room").value = report.room;
  $("selfcheck-results").replaceChildren(...report.items.map(item => {
    const row = document.createElement("article");
    row.className = "selfcheck-result";
    row.dataset.status = item.status;
    const header = document.createElement("header");
    const title = document.createElement("strong");
    title.textContent = item.label;
    const badge = document.createElement("span");
    badge.className = "caption check-state";
    const labels = { pending: "等待检查", running: "检查中", ok: "通过", error: "失败", cancelled: "已取消" };
    badge.textContent = (labels[item.status] || item.status) + (item.elapsed_ms ? " · " + (item.elapsed_ms / 1000).toFixed(1) + " 秒" : "");
    header.append(title, badge);
    const detail = document.createElement("p");
    detail.textContent = item.detail;
    row.append(header, detail);
    return row;
  }));
  controls();
}

function checkError(message = "") {
  $("selfcheck-error").textContent = message;
  $("selfcheck-error").hidden = !message;
}

async function pollCheck(epoch) {
  clearTimeout(state.checkTimer);
  try {
    const report = await call("selfcheck");
    if (epoch !== state.checkEpoch || state.disposed) return;
    showCheck(report);
    checkError();
    if (report.running) state.checkTimer = setTimeout(() => pollCheck(epoch), 1000);
  } catch (error) {
    if (epoch !== state.checkEpoch || state.disposed) return;
    checkError(error.message || "自检结果读取失败，请重新打开环境自检查看。");
    state.checkTimer = setTimeout(() => pollCheck(epoch), 3000);
  }
}

function addWord() {
  const input = $("keyword");
  const value = input.value.trim();
  if (!value) return;
  const normal = value.normalize("NFKC").toLowerCase();
  if (!state.words.some(word => word.normalize("NFKC").toLowerCase() === normal)) {
    if (state.words.length >= 100) {
      notice("最多可添加 100 个唤醒词。");
      return;
    }
    state.words.push(value);
  }
  input.value = "";
  renderWords();
}

function renderWords() {
  const input = $("keyword");
  $("chips").querySelectorAll(".chip").forEach(chip => chip.remove());
  state.words.forEach((word, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chip";
    button.textContent = word + " ×";
    button.setAttribute("aria-label", "删除唤醒词 " + word);
    button.addEventListener("click", () => {
      state.words.splice(index, 1);
      renderWords();
      input.focus();
    });
    input.before(button);
  });
}

function renderOptions(select, options, placeholder, selected) {
  select.replaceChildren(new Option(placeholder, ""));
  for (const item of options) {
    select.append(new Option(item.label, item.id));
  }
  if (selected && ![...select.options].some(option => option.value === selected)) {
    select.append(new Option(selected + "（当前不可用）", selected));
  }
  select.value = selected;
}

function hint() {
  const form = values();
  $("window-hint").textContent = !form.KEYWORD_MODE ? "普通弹幕也可以触发回复。"
    : form.WINDOW_SECONDS > 0 ? "唤醒后可接话 " + form.WINDOW_SECONDS + " 秒。"
    : "只处理带唤醒词的弹幕。";
}

function values() {
  const output = { KEYWORDS: [...state.words] };
  for (const field of formFields) {
    const key = field.dataset.field;
    if (key === "IGNORE_UIDS") {
      output[key] = field.value.split(/[,，\s]+/).filter(Boolean);
    } else if (field.type === "checkbox") {
      output[key] = field.checked;
    } else if (field.type === "number") {
      output[key] = field.valueAsNumber;
    } else {
      output[key] = field.value.trim();
    }
  }
  return output;
}

function setForm(settings) {
  state.words = Array.isArray(settings.KEYWORDS) ? [...settings.KEYWORDS] : [];
  $("keyword").value = "";
  renderWords();
  for (const field of formFields) {
    const value = settings[field.dataset.field];
    if (field.type === "checkbox") field.checked = value === true;
    else field.value = Array.isArray(value) ? value.join(", ") : value ?? "";
  }
  state.baseline = values();
  hint();
}

function edits() {
  return Object.fromEntries(Object.entries(values()).filter(
    ([key, value]) => JSON.stringify(value) !== JSON.stringify(state.baseline[key]),
  ));
}

async function mutate(action) {
  if (state.busy || state.disposed) return;
  state.busy = true;
  state.revision++;
  controls();
  try {
    await action();
  } catch (error) {
    notice(error.message || "操作失败，请稍后重试。");
  } finally {
    state.busy = false;
    state.revision++;
    controls();
  }
}

async function refreshStatus() {
  const revision = state.revision;
  try {
    const snapshot = await call("status");
    if (revision !== state.revision || state.disposed) return;
    showStatus(snapshot);
    connection();
  } catch (error) {
    if (revision === state.revision) connection(error.message || "读取状态失败，请刷新重试。");
  }
}

async function load() {
  await mutate(async () => {
    try {
      const snapshot = await call("state");
      const settings = snapshot.settings;
      state.visionProviders = snapshot.providers;
      state.speechProviders = snapshot.speech_providers ?? [];
      for (const row of state.roomRows.values()) {
        const form = row.querySelector('[data-part="media-form"]');
        setMediaOptions(form, mediaValues(form));
      }
      renderOptions($("provider"), snapshot.providers, "选择聊天模型", settings.MODEL_PROVIDER_ID);
      const personas = snapshot.personas.map(id => ({ id, label: id }));
      renderOptions($("persona"), personas, "不复用人格", settings.ASTRBOT_PERSONA_ID);
      tab(snapshot.account.source === "bilibot" ? "bili" : "local");
      setForm(settings);
      showStatus(snapshot);
      connection();
    } catch (error) {
      connection(error.message || "无法连接插件。");
    }
  });
}

function nextStatus() {
  state.statusTimer = setTimeout(async () => {
    if (state.disposed) return;
    if (state.snapshot && !state.busy) await refreshStatus();
    if (!state.disposed) nextStatus();
  }, 3000);
}

function clearQr(phase = "", detail = "") {
  state.qrRevision++;
  state.qrToken = "";
  state.qrFailures = 0;
  clearTimeout(state.qrTimer);
  $("qr-image").removeAttribute("src");
  state.qrPhase = phase;
  state.qrDetail = detail;
  localPanel();
}

async function setSource(source) {
  if (state.snapshot.account.source === source) return;
  const snapshot = await call("settings", { AUTH_SOURCE: source });
  clearQr();
  showStatus(snapshot);
}

function nextQr(revision) {
  state.qrTimer = setTimeout(() => pollQr(revision), 2000);
}

async function pollQr(revision) {
  if (revision !== state.qrRevision || state.disposed) return;
  try {
    const result = await call("login/poll", { challenge: state.qrToken });
    if (revision !== state.qrRevision || state.disposed) return;
    state.qrFailures = 0;
    state.qrDetail = "";
    if (result.phase === "waiting" || result.phase === "scanned") {
      state.qrPhase = result.phase;
      localPanel();
      nextQr(revision);
    } else if (result.phase === "success") {
      state.revision++;
      clearQr();
      await refreshStatus();
      notice("B 站账号已登录。");
    } else {
      clearQr("expired");
    }
  } catch (error) {
    if (revision !== state.qrRevision || state.disposed) return;
    const message = error.message || "请重新生成二维码。";
    if (message.includes("二维码已失效")) {
      clearQr("expired");
    } else if (++state.qrFailures >= 3 || message.includes("请先登录 AstrBot") || message.includes("插件已停用")) {
      clearQr("error", message);
    } else {
      state.qrDetail = "状态读取暂时失败，正在重试。";
      localPanel();
      nextQr(revision);
    }
  }
}

function generateQr() {
  return mutate(async () => {
    await setSource("local");
    clearQr("loading");
    try {
      const result = await call("login/generate", {});
      if (typeof result.image !== "string" || !result.image.startsWith("data:image/png;base64,") || !result.challenge) {
        throw new Error("未收到有效的二维码。");
      }
      state.qrToken = result.challenge;
      $("qr-image").src = result.image;
      state.qrPhase = "waiting";
      localPanel();
      nextQr(state.qrRevision);
    } catch (error) {
      clearQr("error", error.message || "请重新生成二维码。");
    }
  });
}

for (const [mode, button] of Object.entries(tabs)) {
  button.addEventListener("click", () => tab(mode));
  button.addEventListener("keydown", event => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      tab(event.key === "Home" ? "local" : event.key === "End" ? "bili" : mode === "local" ? "bili" : "local", true);
    }
  });
}

$("keyword").addEventListener("keydown", event => {
  if (event.key !== "Enter" || event.isComposing) return;
  event.preventDefault();
  addWord();
});
$("add-word").addEventListener("click", addWord);
$("settings").addEventListener("input", hint);
$("settings").addEventListener("submit", event => {
  event.preventDefault();
  addWord();
  const changes = edits();
  if (!Object.keys(changes).length) {
    notice("设置没有变动。");
    return;
  }
  mutate(async () => {
    const hadEnabled = state.snapshot.rooms.some(room => room.enabled);
    const snapshot = await call("settings", changes);
    setForm(snapshot.settings);
    showStatus(snapshot);
    notice(hadEnabled ? "设置已保存，已开启的房间将按新设置继续监听。" : "设置已保存。");
  });
});
$("add-room").addEventListener("submit", event => {
  event.preventDefault();
  const input = $("new-room");
  const room = input.value.trim();
  mutate(async () => {
    showStatus(await call("rooms", { room, action: "add" }));
    input.value = "";
    notice("直播间已添加。");
  });
});
$("rooms").addEventListener("click", event => {
  const button = event.target.closest("[data-room-action]");
  if (!button) return;
  const id = button.closest("[data-room]").dataset.room;
  const room = state.snapshot.rooms.find(item => item.id === id);
  if (!room) return;
  if (button.dataset.roomAction === "remove") {
    mutate(async () => {
      showStatus(await call("rooms", { room: id, action: "remove" }));
      notice("直播间已移除。");
    });
    return;
  }
  if (!room.enabled && (Object.keys(edits()).length || $("keyword").value.trim())) {
    notice("请先保存对话设置。");
    return;
  }
  mutate(async () => {
    showStatus(await call("listen", { room: id, running: !room.enabled }));
    notice(room.enabled ? "监听已停止。" : "已开启监听。");
  });
});
$("rooms").addEventListener("submit", event => {
  const form = event.target;
  if (form.dataset.part !== "media-form") return;
  event.preventDefault();
  const row = form.closest("[data-room]");
  const room = row.dataset.room;
  const settings = mediaValues(form);
  mutate(async () => {
    const snapshot = await call("rooms/media", { room, settings });
    showStatus(snapshot);
    setMediaForm(row, snapshot.rooms.find(item => item.id === room).media.settings);
    notice("已保存该房间的音视频设置。");
  });
});
$("new-qr").addEventListener("click", generateQr);
$("use-local").addEventListener("click", () => mutate(() => setSource("local")));
$("use-bili").addEventListener("click", () => mutate(() => setSource("bilibot")));
$("memory").addEventListener("change", event => {
  const enabled = event.currentTarget.checked;
  mutate(async () => {
    try {
      showStatus(await call("settings", { BILIBOT_MEMORY_ENABLED: enabled }));
    } catch (error) {
      $("memory").checked = state.snapshot.account.memory_enabled;
      throw error;
    }
  });
});
$("reload").addEventListener("click", () => {
  if (state.snapshot && (Object.keys(edits()).length || $("keyword").value.trim())) {
    void refreshStatus();
    notice("已刷新运行状态，保留未保存的设置。");
  } else {
    void load();
  }
});
$("open-selfcheck").addEventListener("click", () => {
  $("selfcheck").hidden = !$("selfcheck").hidden;
  state.checkEpoch++;
  clearTimeout(state.checkTimer);
  if (!$("selfcheck").hidden) {
    void pollCheck(state.checkEpoch);
  }
});
$("start-selfcheck").addEventListener("click", async () => {
  if (state.checkBusy || state.check?.running) return;
  if (Object.keys(edits()).length || $("keyword").value.trim()) {
    notice("请先保存共享设置，再检查已保存的模型配置。");
    return;
  }
  const room = $("selfcheck-room").value;
  const row = state.roomRows.get(room);
  if (row) {
    const form = row.querySelector('[data-part="media-form"]');
    const saved = state.snapshot.rooms.find(item => item.id === room).media.settings;
    const values = mediaValues(form);
    if (Object.keys(values).some(key => values[key] !== saved[key])) {
      notice("请先保存该房间的音视频设置。");
      return;
    }
  }
  state.checkBusy = true;
  state.checkEpoch++;
  controls();
  try {
    const report = await call("selfcheck/start", { room });
    if (state.disposed) return;
    showCheck(report);
    checkError();
    if (!$("selfcheck").hidden) void pollCheck(state.checkEpoch);
  } catch (error) {
    checkError(error.message || "无法启动自检。");
  } finally {
    state.checkBusy = false;
    controls();
  }
});
window.addEventListener("pagehide", () => {
  state.disposed = true;
  state.revision++;
  state.qrRevision++;
  state.checkEpoch++;
  clearTimeout(state.checkTimer);
  clearTimeout(state.statusTimer);
  clearTimeout(state.qrTimer);
});
window.addEventListener("pageshow", event => {
  if (event.persisted) {
    state.disposed = false;
    if (state.qrToken) nextQr(state.qrRevision);
    nextStatus();
    void refreshStatus();
    if (!$("selfcheck").hidden) void pollCheck(state.checkEpoch);
  }
});

void load();
nextStatus();

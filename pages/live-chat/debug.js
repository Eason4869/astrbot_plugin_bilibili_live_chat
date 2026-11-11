const statuses = {
  queued: "排队中", judging: "附和判断中", generating: "生成中", waiting: "等待发送", sending: "发送中", sent: "已发送", preview: "预览完成",
  skipped: "已跳过", replaced: "被新消息替代", expired: "已过期", silent: "保持安静", invalid: "回复未通过校验",
  error: "处理失败", cancelled: "已取消",
};
const sources = { voice: "语音", vision: "画面", danmaku: "弹幕" };
const stages = {
  collection: "语音采集 / 抽帧等待", transcription_queue: "转写前排队", transcription: "语音转写",
  queue: "生成前排队（含冷却）", decision: "附和判断", model: "回复生成（含看图）", rate_limit: "生成后限流等待",
  send: "发送接口", total: "本机总耗时",
};
const duration = value => Number.isFinite(value) ? (value < 1000 ? Math.round(value) + " ms" : (value / 1000).toFixed(2) + " 秒") : "—";
const part = (row, name) => row.querySelector('[data-part="' + name + '"]');

function timings(node, values = {}) {
  const cells = [];
  for (const [stage, label] of Object.entries(stages)) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = duration(values[stage]);
    cells.push(dt, dd);
  }
  node.replaceChildren(...cells);
}

export function createRoomDebug({ call, mutate, showStatus, notice, busy }) {
  let active = null;
  let epoch = 0;
  let timer = 0;
  let disposed = false;
  const views = new Map();

  function cancel() {
    epoch++;
    clearTimeout(timer);
  }

  function render(view, report) {
    const { row } = view;
    if (view.session !== report.session) {
      view.session = report.session;
      part(row, "debug-events").replaceChildren();
      view.events.clear();
    }
    const frame = report.frame;
    part(row, "debug-frame").hidden = !frame;
    if (frame) {
      if (typeof frame.image === "string" && frame.image.startsWith("data:image/jpeg;base64,")) {
        part(row, "debug-image").src = frame.image;
      }
      view.frameRevision = frame.revision;
      part(row, "debug-frame-age").textContent = "最近画面 · " + duration(frame.age_ms) + " 前抽取";
    } else {
      view.frameRevision = "";
      part(row, "debug-image").removeAttribute("src");
    }
    const events = report.events;
    const transcript = [...events].reverse().find(event => event.source === "voice" && event.text);
    part(row, "debug-transcript").textContent = transcript?.text || "尚未收到语音转写。";
    const stats = report.statistics?.[report.preview ? "preview" : "sent"];
    part(row, "debug-stats-title").textContent = (report.preview ? "预览" : "实际发送") + "平均耗时 · 最近 " + (stats?.count || 0) + " 条完成记录";
    timings(part(row, "debug-averages"), stats?.averages);
    part(row, "debug-empty").hidden = events.length > 0;
    const list = part(row, "debug-events");
    const ids = new Set(events.map(event => event.id));
    for (const [id, item] of view.events) {
      if (!ids.has(id)) {
        item.remove();
        view.events.delete(id);
      }
    }
    [...events].reverse().forEach((event, index) => {
      let item = view.events.get(event.id);
      if (!item) {
        item = document.createElement("li");
        const details = document.createElement("details");
        details.open = !list.children.length;
        details.append(document.createElement("summary"));
        for (const cls of ["debug-input", "debug-reason", "debug-output"]) {
          const text = document.createElement("p");
          text.className = cls;
          details.append(text);
        }
        const dl = document.createElement("dl");
        dl.className = "latency-grid";
        details.append(dl);
        item.append(details);
        view.events.set(event.id, item);
      }
      if (list.children[index] !== item) list.insertBefore(item, list.children[index] ?? null);
      item.dataset.status = event.status;
      const time = new Date(event.at).toLocaleTimeString();
      item.querySelector("summary").textContent = time + " · " + (sources[event.source] || "输入") + " · " + (statuses[event.status] || event.status);
      item.querySelector(".debug-input").textContent = (event.name ? event.name + "：" : "") + (event.text || "（无识别文本）");
      item.querySelector(".debug-reason").textContent = (event.keyword ? "命中「" + event.keyword + "」 · " : "") + [event.decision, event.reason].filter(Boolean).join(" · ");
      const output = item.querySelector(".debug-output");
      output.hidden = !event.reply;
      output.textContent = (event.status === "sent" ? "已发送：" : "生成回复：") + event.reply;
      timings(item.querySelector("dl"), event.timings);
    });
    part(row, "debug-state").textContent = report.running ? "每 2 秒刷新 · " + (report.preview ? "只预览" : "实际发送模式") : "房间尚未运行 · 开始监听后更新";
  }

  async function poll(generation) {
    const view = active;
    if (!view || generation !== epoch || disposed || view.paused || document.hidden) return;
    try {
      if (!busy()) {
        const report = await call("debug", { room: view.room, frame_revision: view.frameRevision });
        if (generation !== epoch || disposed || active !== view || view.paused) return;
        render(view, report);
        part(view.row, "debug-error").hidden = true;
      }
    } catch (error) {
      if (generation !== epoch || disposed || active !== view) return;
      const alert = part(view.row, "debug-error");
      alert.hidden = false;
      alert.textContent = error.message || "无法读取调试记录，稍后重试。";
    } finally {
      if (generation === epoch && !disposed && active === view && !view.paused && !document.hidden) {
        timer = setTimeout(() => poll(generation), 2000);
      }
    }
  }

  return {
    attach(row, room) {
      const view = { row, room, events: new Map(), session: "", frameRevision: "", paused: false };
      views.set(room, view);
      row.querySelector("[data-preview]").addEventListener("change", event => {
        const input = event.currentTarget;
        const preview = input.checked;
        void mutate(async () => {
          cancel();
          try {
            showStatus(await call("rooms/preview", { room, preview }));
            notice(preview ? "本房间已切换为只预览，不会发送弹幕。" : "本房间已切换为实际发送模式。");
          } catch (error) {
            input.indeterminate = true;
            input.dataset.uncertain = "true";
            input.disabled = true;
            part(row, "preview-state").textContent = "状态待确认";
            const alert = part(row, "preview-error");
            alert.hidden = false;
            alert.textContent = "切换结果暂未确认，房间可能已切换为实际发送。请刷新运行状态。";
            try { showStatus(await call("status")); } catch { /* Keep mode visibly unknown until an authoritative refresh succeeds. */ }
            throw error;
          } finally {
            if (active && !active.paused) void poll(epoch);
          }
        });
      });
      part(row, "debug").addEventListener("toggle", () => {
        if (part(row, "debug").open) {
          cancel();
          if (active && active !== view) part(active.row, "debug").open = false;
          active = view;
          view.paused = false;
          part(row, "debug-pause").textContent = "暂停刷新";
          void poll(epoch);
        } else if (active === view) {
          cancel();
          active = null;
        }
      });
      part(row, "debug-pause").addEventListener("click", () => {
        view.paused = !view.paused;
        cancel();
        part(row, "debug-pause").textContent = view.paused ? "继续刷新" : "暂停刷新";
        if (view.paused) part(row, "debug-state").textContent = "已暂停页面刷新，后台处理继续运行。";
        else void poll(epoch);
      });
    },
    update(row, room) {
      const input = row.querySelector("[data-preview]");
      input.checked = Boolean(room.preview);
      input.indeterminate = false;
      delete input.dataset.uncertain;
      part(row, "preview-error").hidden = true;
      part(row, "preview-state").textContent = room.preview ? "开启 · 不发送" : "关闭 · 实际发送";
      part(row, "preview-state").classList.toggle("is-preview", Boolean(room.preview));
      part(row, "debug-summary").textContent = room.preview ? "已预览 " + (room.previews || 0) + " 条" : "输入、回复与分阶段耗时";
    },
    remove(room) {
      if (active?.room === room) {
        cancel();
        active = null;
      }
      views.delete(room);
    },
    suspend() { disposed = true; cancel(); },
    resume() { disposed = false; cancel(); if (active && !active.paused) void poll(epoch); },
    visibility() { cancel(); if (!document.hidden && !disposed && active && !active.paused) void poll(epoch); },
  };
}

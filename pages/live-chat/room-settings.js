const fields = [
  ["LISTEN_MODE", "弹幕监听方式", "listen"],
  ["MODEL_PROVIDER_ID", "聊天模型", "model"], ["ASTRBOT_PERSONA_ID", "AstrBot 人格", "persona"],
  ["KEYWORDS", "唤醒词（每行一个）", "list"], ["VOICE_KEYWORD_ALIASES", "语音唤醒别名（每行一个）", "list"],
  ["KEYWORD_MODE", "关键词唤醒模式", "bool"], ["CROSS_SOURCE_DEDUP_SECONDS", "语音与弹幕去重窗口（秒）", "number", 0, 30],
  ["FOLLOWUP_SCOPE", "窗口内接话对象", "scope"],
  ["VOICE_FOLLOWUP_ENABLED", "语音连续接话与附和", "bool"],
  ["VISION_ECHO_ENABLED", "画面变化主动附和", "bool"],
  ["ECHO_ENABLED", "本房间低频附和（受共享总开关限制）", "bool"], ["ECHO_DECISION_ENABLED", "智能附和判断", "bool"],
  ["ECHO_DECISION_PROVIDER_ID", "AstrBot 判断模型 ID（留空默认）", "text"],
  ["ECHO_DECISION_THRESHOLD", "附和判断阈值", "number", 0.5, 1],
  ["PERSONA_PROMPT", "直播专用人格提示词", "prompt"], ["REPLY_INSTRUCTION", "正式回复附加提示词", "prompt"],
  ["ECHO_INSTRUCTION", "附和附加提示词", "prompt"],
];
const part = (row, name) => row.querySelector('[data-part="' + name + '"]');
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

export function createRoomSettings({ call, mutate, showStatus, notice, setMediaForm, mediaDirty }) {
  const views = new Map();
  let shared = {}, rooms = [], providers = [], personas = [];
  function value(field, kind) {
    if (kind === "list") return [...new Set(field.value.split(/\r?\n/).map(word => word.trim()).filter(Boolean))];
    if (kind === "bool") return field.value === "true";
    if (kind === "number") return field.valueAsNumber;
    return field.value.trim();
  }
  function setValue(field, kind, selected) {
    if (kind === "listen") field.replaceChildren(new Option("轮询", "poll"), new Option("长连接优先（失败轮询回退）", "auto"));
    if (kind === "scope") field.replaceChildren(new Option("仅已唤醒的观众", "speaker"), new Option("全房间观众", "room"));
    if (kind === "model" || kind === "persona") {
      field.replaceChildren(new Option(kind === "model" ? "选择聊天模型" : "不复用人格", ""));
      for (const option of kind === "model" ? providers : personas) field.append(new Option(option.label, option.id));
      if (selected && ![...field.options].some(option => option.value === selected)) field.append(new Option(selected + "（当前不可用）", selected));
    }
    field.value = kind === "list" ? (Array.isArray(selected) ? selected : []).join("\n") : selected ?? "";
  }
  function read(view, includeInherited = false) {
    const result = {};
    for (const [key, , kind] of fields) {
      const { input, inherit } = view.fields.get(key);
      if (inherit.checked) { if (includeInherited) result[key] = null; }
      else result[key] = value(input, kind);
    }
    return result;
  }
  function dirty(view) { return !same(read(view), view.saved); }
  function fill(view, dialogue) {
    view.latest = dialogue;
    for (const [key, , kind] of fields) {
      const { input, inherit } = view.fields.get(key);
      inherit.checked = !Object.hasOwn(dialogue.overrides, key);
      input.disabled = inherit.checked;
      setValue(input, kind, inherit.checked ? shared[key] : dialogue.overrides[key]);
    }
    view.saved = read(view);
  }
  return {
    configure(snapshot, models, availablePersonas) {
      shared = snapshot.settings; rooms = snapshot.rooms; providers = models; personas = availablePersonas;
    },
    attach(row, room) {
      const view = { row, room: room.id, fields: new Map(), saved: {}, latest: room.dialogue };
      views.set(room.id, view);
      const container = part(row, "dialogue-fields");
      container.classList.remove("fields");
      const groups = new Map();
      for (const [id, label] of [["trigger", "唤醒与输入"], ["model", "模型与提示词"], ["echo", "低频附和"]]) {
        const details = document.createElement("details");
        details.className = "override-group";
        details.open = id === "trigger";
        const summary = document.createElement("summary");
        summary.textContent = label;
        const fields = document.createElement("div");
        fields.className = "fields";
        details.append(summary, fields); container.append(details); groups.set(id, fields);
      }
      for (const [key, label, kind, min, max] of fields) {
        const wrapper = document.createElement("div");
        wrapper.className = "override-field" + (kind === "prompt" ? " field-wide" : "");
        const toggleLabel = document.createElement("label");
        toggleLabel.className = "inherit-option";
        const inherit = document.createElement("input");
        inherit.type = "checkbox"; inherit.dataset.inherit = key;
        inherit.setAttribute("aria-label", label + "：继承共享");
        toggleLabel.append(inherit, document.createTextNode("继承共享"));
        const fieldLabel = document.createElement("label");
        fieldLabel.className = "field";
        const caption = document.createElement("span"); caption.textContent = label;
        const input = document.createElement(["model", "persona", "bool", "listen", "scope"].includes(kind) ? "select" : ["prompt", "list"].includes(kind) ? "textarea" : "input");
        input.name = key;
        if (kind === "bool") input.append(new Option("开启", "true"), new Option("关闭", "false"));
        if (kind === "number") { input.type = "number"; input.min = min; input.max = max; input.step = "any"; input.required = true; }
        if (["prompt", "list"].includes(kind)) input.rows = 2;
        if (kind === "text") { input.type = "text"; input.maxLength = 256; }
        if (kind === "prompt") input.maxLength = key === "PERSONA_PROMPT" ? 4000 : 2000;
        const group = key.startsWith("ECHO_") ? "echo" : ["model", "persona", "prompt"].includes(kind) ? "model" : "trigger";
        fieldLabel.append(caption, input); wrapper.append(toggleLabel, fieldLabel); groups.get(group).append(wrapper);
        view.fields.set(key, { input, inherit });
        inherit.addEventListener("change", () => {
          input.disabled = inherit.checked;
          if (inherit.checked) setValue(input, kind, shared[key]);
        });
      }
      fill(view, room.dialogue);
      part(row, "dialogue-form").addEventListener("invalid", event => {
        for (let parent = event.target.parentElement; parent; parent = parent.parentElement) {
          if (parent.tagName === "DETAILS") parent.open = true;
        }
      }, true);
      part(row, "dialogue-form").addEventListener("submit", event => {
        event.preventDefault();
        const settings = read(view, true);
        if (Object.values(settings).some(value => typeof value === "number" && !Number.isFinite(value))) { notice("请填写有效的房间配置数值。"); return; }
        void mutate(async () => {
          const snapshot = await call("rooms/dialogue", { room: view.room, settings });
          fill(view, snapshot.rooms.find(item => item.id === view.room).dialogue);
          showStatus(snapshot); notice("本房间对话配置已保存。");
        });
      });
      part(row, "dialogue-restore").addEventListener("click", () => fill(view, view.latest));
      part(row, "copy-settings").addEventListener("click", () => {
        const source = part(row, "copy-source").value;
        if (!source) return;
        const latest = rooms.find(item => item.id === view.room);
        if (dirty(view) || mediaDirty(row, latest.media.settings)) { notice("请先保存或恢复本房间未保存的配置，再复制其他房间。"); return; }
        void mutate(async () => {
          const snapshot = await call("rooms/copy", { source, destination: view.room });
          const updated = snapshot.rooms.find(item => item.id === view.room);
          fill(view, updated.dialogue); setMediaForm(row, updated.media.settings);
          showStatus(snapshot); notice("已复制房间 " + source + " 的对话和音视频配置。");
        });
      });
    },
    update(row, room) {
      const view = views.get(room.id);
      if (!view) return;
      view.latest = room.dialogue;
      if (!dirty(view)) fill(view, room.dialogue);
      const count = Object.keys(room.dialogue.overrides).length;
      part(row, "dialogue-summary").textContent = count ? count + " 项独立配置" : "继承共享设置";
      const select = part(row, "copy-source"), selected = select.value;
      select.replaceChildren(new Option("选择来源房间", ""), ...rooms.filter(item => item.id !== room.id).map(item => new Option("房间 " + item.id, item.id)));
      select.value = selected;
    },
    remove(room) { views.delete(room); },
  };
}

// Optional browser regression check: BROWSER_BIN=/path/to/chrome node tests/webui_smoke.mjs
// Uses only Node built-ins and a local mock bridge; never connects to Bilibili.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFile, mkdtemp, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const schema = JSON.parse(await readFile(path.join(root, "_conf_schema.json"), "utf8"));
const settings = Object.fromEntries(Object.entries(schema).map(([key, value]) => [key, value.default]));
settings.MODEL_PROVIDER_ID = "chat";
settings.KEYWORDS = ["小猫"];
settings.WINDOW_SECONDS = 90;
settings.ECHO_ENABLED = true;
const media = { audio: true, video: true, stt_provider: "", vision_provider: "", audio_segment: 6, frame_interval: 5, echo_interval: null, echo_probability: null, scene_threshold: 24, scene_confirm_frames: 1, ignored_regions: [] };
const fixture = {
  builtin_prompt: '先判断当前话是否在对你说。不要为了活跃气氛而插话。没有新增信息时保持安静。',
  settings, account: { source: "local", local: { configured: true, uid: "42" }, bilibot: { configured: false }, memory_enabled: false, memory_available: false },
  environment: { ffmpeg: { available: true, path: "mock-ffmpeg" } },
  providers: [{ id: "chat", label: "Test chat" }], speech_providers: [], personas: [],
  rooms: ["12", "13"].map(id => ({ id, canonical: id, enabled: true, running: true, phase: "listening", received: 3, replies: 1, previews: 0, remaining: 80, issues: [], preview: false, listener: { configured: "poll", source: "poll", phase: "polling", reason: "", retry_in: 0 }, dialogue: { overrides: {}, effective: settings }, media: { settings: media, transcripts: 2, frames: 4, issues: [] } })),
};
const injection = `<script>
window.__calls=[];window.__debugDelay=0;window.__fixture=${JSON.stringify(fixture)};
window.__setListener=room=>{const mode=room.dialogue.effective.LISTEN_MODE;room.listener={configured:mode,source:mode==='auto'?'websocket':'poll',phase:mode==='auto'?'connected':'polling',reason:'',retry_in:0,heartbeat_age_ms:mode==='auto'?0:null};};
window.AstrBotPluginView={
 apiGet:async endpoint=>{if(window.__statusFails)throw new Error('mock status unavailable');return {status:'ok',data:window.__fixture};},
 apiPost:async(endpoint,payload)=>{
  window.__calls.push({endpoint,payload});
  if(endpoint==='settings'){
   if(window.__saveFails)throw new Error('mock save failed');
   Object.assign(window.__fixture.settings,payload);
   for(const room of window.__fixture.rooms)room.dialogue.effective={...window.__fixture.settings,...room.dialogue.overrides};
   return {status:'ok',data:window.__fixture};
  }
  if(endpoint==='debug'){
   const room=window.__fixture.rooms.find(room=>room.id===payload.room);
   const data={room:room.id,session:'mock:'+room.id,running:true,preview:room.preview,frame:null,events:[{
    id:1,at:Date.now(),source:'voice',name:'直播语音',text:'<img src=x onerror="window.__xss=1"> 小猫你好',
    keyword:'小猫',status:room.preview?'preview':'sent',reply:'你好呀',reason:room.preview?'仅预览，不发送':'发送成功',
    timings:{collection:1000,transcription:200,queue:3,model:100,rate_limit:0,send:room.preview?null:30,total:1333}
   }],statistics:{sent:{count:room.preview?0:1,averages:{total:1333}},preview:{count:room.preview?1:0,averages:{total:1303}}}};
   await new Promise(resolve=>setTimeout(resolve,window.__debugDelay));return {status:'ok',data};
  }
  if(endpoint==='rooms/preview'){
   window.__fixture.rooms.find(room=>room.id===payload.room).preview=payload.preview;
   if(window.__losePreviewResponse)throw new Error('mock response lost after commit');
   return {status:'ok',data:window.__fixture};
  }
  if(endpoint==='rooms/dialogue'){
   const room=window.__fixture.rooms.find(room=>room.id===payload.room);
   room.dialogue.overrides=Object.fromEntries(Object.entries({...room.dialogue.overrides,...payload.settings}).filter(([key,value])=>value!==null));
   room.dialogue.effective={...window.__fixture.settings,...room.dialogue.overrides};
   window.__setListener(room);
   return {status:'ok',data:window.__fixture};
  }
  if(endpoint==='rooms/copy'){
   const source=window.__fixture.rooms.find(room=>room.id===payload.source);
   const target=window.__fixture.rooms.find(room=>room.id===payload.destination);
   target.dialogue=structuredClone(source.dialogue);target.media.settings=structuredClone(source.media.settings);
   window.__setListener(target);
   return {status:'ok',data:window.__fixture};
  }
  return {status:'ok',data:window.__fixture};
 }
};</script>`;
const pageRoot = path.join(root, "pages", "live-chat");
const server = createServer(async (request, response) => {
  const filename = path.basename(new URL(request.url, "http://localhost").pathname) || "index.html";
  try {
    let data = await readFile(path.join(pageRoot, filename));
    if (filename === "index.html") data = Buffer.from(data.toString().replace("<head>", "<head>" + injection));
    response.setHeader("Content-Type", filename.endsWith(".js") ? "text/javascript" : filename.endsWith(".css") ? "text/css" : filename.endsWith(".png") ? "image/png" : "text/html; charset=utf-8");
    response.end(data);
  } catch { response.writeHead(404).end(); }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const url = `http://127.0.0.1:${server.address().port}/index.html`;
const profile = await mkdtemp(path.join(tmpdir(), "bilibot-webui-"));
const executable = process.env.BROWSER_BIN || (process.platform === "win32" ? "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" : "/usr/bin/google-chrome");
const browser = spawn(executable, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-debugging-port=0", "--user-data-dir=" + profile, url], { windowsHide: true, stdio: "ignore" });
let socket;
let port;
const errors = [];
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(check, label, timeout = 15000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (await check()) return;
    await pause(80);
  }
  throw new Error("Timed out: " + label);
}
try {
  browser.on("error", error => errors.push(error.message));
  await until(async () => {
    try { port = (await readFile(path.join(profile, "DevToolsActivePort"), "utf8")).split("\n")[0]; return true; } catch { return false; }
  }, "browser start");
  let target;
  await until(async () => {
    const pages = await fetch(`http://127.0.0.1:${port}/json/list`).then(response => response.json());
    target = pages.find(page => page.url === url);
    return target;
  }, "browser tab");
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let sequence = 0;
  const pending = new Map();
  socket.onmessage = event => {
    const data = JSON.parse(event.data);
    if (data.method === "Runtime.exceptionThrown") errors.push(data.params.exceptionDetails.text);
    const callback = pending.get(data.id);
    if (callback) { pending.delete(data.id); data.error ? callback.reject(data.error) : callback.resolve(data.result); }
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async expression => {
    const data = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (data.exceptionDetails) throw new Error(JSON.stringify(data.exceptionDetails));
    return data.result.value;
  };
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await until(() => evaluate("document.querySelectorAll('[data-room]').length===2"), "room rendering");
  if (process.env.DESKTOP_SCREENSHOT_PATH) {
    const screenshot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    await writeFile(process.env.DESKTOP_SCREENSHOT_PATH, Buffer.from(screenshot.data, "base64"));
  }
  await evaluate("document.querySelector('[data-view-target=strategy]').click()");
  assert.equal(await evaluate("document.querySelector('[data-view=strategy]').hidden"), false);
  assert.equal(await evaluate("document.querySelector('[data-view=rooms]').hidden"), true);
  const settingsCalls = await evaluate("window.__calls.filter(call=>call.endpoint==='settings').length");
  await evaluate("document.querySelector('[data-preset=balanced]').click()");
  assert.equal(await evaluate("document.querySelector('[data-field=WINDOW_SECONDS]').value"), "30");
  assert.equal(await evaluate("document.querySelector('[data-field=FOLLOWUP_SCOPE]').value"), "speaker");
  assert.equal(await evaluate("document.querySelector('[data-field=VOICE_FOLLOWUP_ENABLED]').checked"), false);
  assert.equal(await evaluate("window.__calls.filter(call=>call.endpoint==='settings').length"), settingsCalls, "preset must not save immediately");
  assert.match(await evaluate("document.getElementById('save-state').textContent"), /未保存/);
  await evaluate("document.getElementById('reload').click()");
  await pause(300);
  assert.equal(await evaluate("document.querySelector('[data-field=WINDOW_SECONDS]').value"), "30", "refresh must preserve shared edits");
  await evaluate("window.__saveFails=true;document.getElementById('save-settings').click()");
  await until(() => evaluate("document.getElementById('toast').textContent.includes('mock save failed')"), "save failure");
  assert.equal(await evaluate("document.querySelector('[data-field=WINDOW_SECONDS]').value"), "30", "failed save preserves edits");
  await evaluate("window.__saveFails=false;document.getElementById('save-settings').click()");
  await until(() => evaluate("window.__fixture.settings.WINDOW_SECONDS===30"), "save shared strategy");
  await until(() => evaluate("document.getElementById('save-state').textContent.includes('已保存')"), "saved state");
  await evaluate("document.querySelector('[data-preset=quiet]').click();document.getElementById('restore-settings').click()");
  assert.equal(await evaluate("document.querySelector('[data-field=WINDOW_SECONDS]').value"), "30");
  await evaluate("document.querySelector('[data-view-target=models]').click()");
  assert.match(await evaluate("document.getElementById('builtin-prompt').textContent"), /保持安静/);
  await evaluate("document.querySelector('[data-field=PERSONA_PROMPT]').value='像猫咪一样说话';document.querySelector('[data-field=PERSONA_PROMPT]').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[data-view-target=strategy]').click()");
  assert.match(await evaluate("document.getElementById('save-state').textContent"), /未保存/);
  if (process.env.STRATEGY_SCREENSHOT_PATH) {
    await evaluate("document.getElementById('toast').hidden=true;window.scrollTo(0,0);document.activeElement.blur()");
    const screenshot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    await writeFile(process.env.STRATEGY_SCREENSHOT_PATH, Buffer.from(screenshot.data, "base64"));
  }
  await evaluate("document.getElementById('restore-settings').click();document.querySelector('[data-view-target=rooms]').click()");
  await evaluate("document.querySelector('[data-room=\"12\"] [data-part=debug]').open=true");
  await until(() => evaluate("document.querySelectorAll('[data-room=\"12\"] .debug-events li').length===1"), "debug rendering");
  assert.equal(await evaluate("Boolean(window.__xss || document.querySelector('.debug-events img'))"), false, "untrusted input must stay text");
  assert.match(await evaluate("document.querySelector('[data-room=\"12\"] [data-part=debug-transcript]').textContent"), /小猫/);
  await evaluate("document.querySelector('[data-room=\"12\"] [data-preview]').click()");
  await until(() => evaluate("document.querySelector('[data-room=\"12\"] [data-part=preview-state]').textContent.includes('开启')"), "preview save");
  assert.equal(await evaluate("window.__fixture.rooms[1].preview"), false, "preview settings must stay room-local");
  await until(() => evaluate("document.querySelector('[data-room=\"12\"] .debug-events li').dataset.status==='preview'"), "preview event");
  await evaluate("document.querySelector('[data-room=\"12\"] [data-part=debug-pause]').click()");
  const calls = await evaluate("window.__calls.filter(call=>call.endpoint==='debug').length");
  await pause(2300);
  assert.equal(await evaluate("window.__calls.filter(call=>call.endpoint==='debug').length"), calls, "paused debug must not poll");
  await evaluate("window.__debugDelay=500;document.querySelector('[data-room=\"12\"] [data-part=debug-pause]').click()");
  await pause(60);
  await evaluate("document.querySelector('[data-room=\"13\"] [data-part=debug]').open=true");
  await until(() => evaluate("document.querySelectorAll('[data-room=\"13\"] .debug-events li').length===1"), "room switch");
  assert.equal(await evaluate("document.querySelector('[data-room=\"12\"] [data-part=debug]').open"), false);
  assert.equal(await evaluate("document.querySelector('[data-room=\"13\"] .debug-events li').dataset.status"), "sent", "late previous-room replies must not leak");
  await evaluate("window.__losePreviewResponse=true;window.__statusFails=true;document.querySelector('[data-room=\"12\"] [data-preview]').click()");
  await until(() => evaluate("window.__fixture.rooms[0].preview===false"), "mode change reaches server");
  await pause(250);
  assert.equal(await evaluate("document.querySelector('[data-room=\"12\"] [data-preview]').indeterminate"), true, "unknown mode must never claim preview is active");
  assert.equal(await evaluate("document.querySelector('[data-room=\"12\"] [data-preview]').disabled"), true);
  await evaluate("window.__statusFails=false;window.__losePreviewResponse=false;document.getElementById('reload').click()");
  await until(() => evaluate("!document.querySelector('[data-room=\"12\"] [data-preview]').indeterminate"), "authoritative mode recovery");
  assert.equal(await evaluate("document.querySelector('[data-room=\"12\"] [data-preview]').checked"), false);
  await evaluate(`{
    const row=document.querySelector('[data-room="12"]');
    row.querySelector('.dialogue-settings').open=true;
    row.querySelector('[data-inherit="KEYWORDS"]').click();
    row.querySelector('[name="KEYWORDS"]').value='房间专用词';
    row.querySelector('[data-inherit="LISTEN_MODE"]').click();
    row.querySelector('[name="LISTEN_MODE"]').value='auto';
    row.querySelector('[data-part="dialogue-form"]').requestSubmit();
  }`);
  await until(() => evaluate("window.__fixture.rooms[0].dialogue.overrides.KEYWORDS?.[0]==='房间专用词'"), "room override save");
  assert.equal(await evaluate("window.__fixture.rooms[1].dialogue.overrides.KEYWORDS===undefined"), true);
  await until(() => evaluate("document.querySelector('[data-room=\"12\"] [data-part=listener]').textContent.includes('长连接已认证')"), "websocket source status");
  await evaluate(`{
    const row=document.querySelector('[data-room="12"]');
    row.querySelector('[data-inherit="ECHO_DECISION_THRESHOLD"]').click();
    const input=row.querySelector('[name="ECHO_DECISION_THRESHOLD"]');
    input.value='';input.closest('details').open=false;
    row.querySelector('[data-part="dialogue-form"]').requestSubmit();
  }`);
  assert.equal(await evaluate("document.querySelector('[data-room=\"12\"] [name=ECHO_DECISION_THRESHOLD]').closest('details').open"), true, "room validation must reveal hidden invalid input");
  await evaluate("document.querySelector('[data-room=\"12\"] [data-part=dialogue-restore]').click()");
  assert.equal(await evaluate("window.__fixture.rooms[1].listener.configured"), "poll");
  await evaluate("Object.assign(window.__fixture.rooms[0].listener,{source:'poll',phase:'fallback',reason:'模拟回退原因',retry_in:60,heartbeat_age_ms:null});document.getElementById('reload').click()");
  await until(() => evaluate("document.querySelector('[data-room=\"12\"] [data-part=listener]').textContent.includes('模拟回退原因')"), "fallback reason status");
  assert.match(await evaluate("document.querySelector('[data-room=\"12\"] [data-part=listener]').textContent"), /60 秒后/);
  await evaluate("window.__setListener(window.__fixture.rooms[0]);document.getElementById('reload').click()");
  await until(() => evaluate("!document.querySelector('[data-room=\"12\"] [data-part=listener]').textContent.includes('模拟回退原因')"), "recovered source clears stale reason");
  await evaluate(`{
    const row=document.querySelector('[data-room="13"]');
    row.querySelector('[data-part="copy-source"]').value='12';
    row.querySelector('[data-part="copy-settings"]').click();
  }`);
  await until(() => evaluate("window.__fixture.rooms[1].dialogue.overrides.KEYWORDS?.[0]==='房间专用词'"), "copy saved room settings");
  await evaluate(`{
    const row=document.querySelector('[data-room="12"]');
    row.querySelector('[name="KEYWORDS"]').value='尚未保存';
    document.getElementById('reload').click();
  }`);
  await pause(300);
  assert.equal(await evaluate("document.querySelector('[data-room=\"12\"] [name=KEYWORDS]').value"), "尚未保存", "status refresh preserves room edits");
  await evaluate(`{
    const row=document.querySelector('[data-room="12"]');
    row.querySelector('[data-part="dialogue-restore"]').click();
    row.querySelector('[data-inherit="KEYWORDS"]').click();
    row.querySelector('[data-part="dialogue-form"]').requestSubmit();
  }`);
  await until(() => evaluate("window.__fixture.rooms[0].dialogue.overrides.KEYWORDS===undefined"), "restore shared inheritance");
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await pause(200);
  assert.equal(await evaluate("document.documentElement.scrollWidth <= window.innerWidth"), true, "mobile layout must not overflow");
  if (process.env.SCREENSHOT_PATH) {
    await evaluate("document.getElementById('toast').hidden=true;window.scrollTo(0,0);document.activeElement.blur()");
    const screenshot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    await writeFile(process.env.SCREENSHOT_PATH, Buffer.from(screenshot.data, "base64"));
  }
  assert.deepEqual(errors, []);
  for (const view of ['strategy', 'models', 'account', 'rooms']) {
    await evaluate(`document.querySelector('[data-view-target=${view}]').click()`);
    assert.equal(await evaluate("document.documentElement.scrollWidth <= window.innerWidth"), true, view + " mobile layout must not overflow");
    if (view === 'strategy' && process.env.MOBILE_STRATEGY_SCREENSHOT_PATH) {
      const screenshot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
      await writeFile(process.env.MOBILE_STRATEGY_SCREENSHOT_PATH, Buffer.from(screenshot.data, "base64"));
    }
  }
  console.log("Browser smoke passed: navigation, presets, shared save/restore/failure, preview, room overrides/copy, listener recovery, safe rendering and all mobile views.");
} finally {
  socket?.close();
  browser.kill();
  server.closeAllConnections();
  server.close();
}

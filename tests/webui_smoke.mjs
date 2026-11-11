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
const media = { audio: true, video: true, stt_provider: "", vision_provider: "", audio_segment: 6, frame_interval: 5, echo_interval: null, echo_probability: null, scene_threshold: 24, scene_confirm_frames: 1, ignored_regions: [] };
const fixture = {
  settings, account: { source: "local", local: { configured: true, uid: "42" }, bilibot: { configured: false }, memory_enabled: false, memory_available: false },
  environment: { ffmpeg: { available: true, path: "mock-ffmpeg" } },
  providers: [{ id: "chat", label: "Test chat" }], speech_providers: [], personas: [],
  rooms: ["12", "13"].map(id => ({ id, canonical: id, enabled: true, running: true, phase: "listening", received: 3, replies: 1, previews: 0, remaining: 80, issues: [], preview: false, dialogue: { overrides: {}, effective: settings }, media: { settings: media, transcripts: 2, frames: 4, issues: [] } })),
};
const injection = `<script>
window.__calls=[];window.__debugDelay=0;window.__fixture=${JSON.stringify(fixture)};
window.AstrBotPluginView={
 apiGet:async endpoint=>{if(window.__statusFails)throw new Error('mock status unavailable');return {status:'ok',data:window.__fixture};},
 apiPost:async(endpoint,payload)=>{
  window.__calls.push({endpoint,payload});
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
   return {status:'ok',data:window.__fixture};
  }
  if(endpoint==='rooms/copy'){
   const source=window.__fixture.rooms.find(room=>room.id===payload.source);
   const target=window.__fixture.rooms.find(room=>room.id===payload.destination);
   target.dialogue=structuredClone(source.dialogue);target.media.settings=structuredClone(source.media.settings);
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
  await until(() => evaluate("document.querySelectorAll('[data-room]').length===2"), "room rendering");
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
    row.querySelector('[data-part="dialogue-form"]').requestSubmit();
  }`);
  await until(() => evaluate("window.__fixture.rooms[0].dialogue.overrides.KEYWORDS?.[0]==='房间专用词'"), "room override save");
  assert.equal(await evaluate("window.__fixture.rooms[1].dialogue.overrides.KEYWORDS===undefined"), true);
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
    const screenshot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    await writeFile(process.env.SCREENSHOT_PATH, Buffer.from(screenshot.data, "base64"));
  }
  assert.deepEqual(errors, []);
  console.log("Browser smoke passed: preview persistence, room isolation, safe rendering, polling pause, stale responses, mobile layout.");
} finally {
  socket?.close();
  browser.kill();
  server.closeAllConnections();
  server.close();
}

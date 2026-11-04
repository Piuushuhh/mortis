const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

async function main() {
  const nativeMessages = [];
  const timerCallbacks = [];
  let connectCount = 0;
  let disconnectCalls = 0;
  let serviceMessageListener;
  let nativeMessageListener;
  let nativeDisconnectListener;
  let storedArmedTab = null;
  const port = {
    onMessage: { addListener(listener) { nativeMessageListener = listener; } },
    onDisconnect: { addListener(listener) { nativeDisconnectListener = listener; } },
    postMessage(message) { nativeMessages.push(message); },
    disconnect() { disconnectCalls += 1; },
  };
  const chrome = {
    runtime: {
      id: "test-extension-id",
      lastError: null,
      connectNative() { connectCount += 1; return port; },
      sendMessage() { return Promise.resolve(); },
      onMessage: { addListener(listener) { serviceMessageListener = listener; } },
      onStartup: { addListener() {} },
    },
    storage: { session: {
      async get() { return storedArmedTab ? { armedTab: storedArmedTab } : {}; },
      async set() {},
      async remove() {},
    } },
    tabs: {
      async get() { return { url: "https://example.test/page" }; },
      onRemoved: { addListener() {} },
      onUpdated: { addListener() {} },
    },
    scripting: {
      executeScript() { return new Promise(() => {}); },
    },
  };
  const context = {
    chrome,
    console: { info() {}, warn() {}, error() {} },
    setTimeout(callback) { timerCallbacks.push(callback); return timerCallbacks.length; },
    clearTimeout() {},
    setInterval() { return 1; },
    clearInterval() {},
    Date,
    Math,
    URL,
  };
  vm.runInNewContext(
    fs.readFileSync("browser_extension/service_worker.js", "utf8"), context,
    { filename: "service_worker.js" },
  );

  let status;
  serviceMessageListener(
    { type: "get_status" },
    { id: "test-extension-id" },
    (response) => { status = response; },
  );
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(connectCount, 1, "status request opens one native port");
  assert.equal(status.connecting, true, "popup sees a connecting state before handshake");

  await nativeMessageListener({ type: "host_connected" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(nativeMessages[0].type, "bridge_ready");
  assert.equal(disconnectCalls, 0, "one-time popup request does not close native port");

  storedArmedTab = { tabId: 7, url: "https://example.test/page", title: "Test" };
  nativeMessageListener({
    type: "capture_active_tab", request_id: "stalled-capture",
  });
  await new Promise((resolve) => setImmediate(resolve));
  const captureTimeout = timerCallbacks.at(-1);
  assert.equal(typeof captureTimeout, "function", "capture installs a browser-response timeout");
  captureTimeout();
  await new Promise((resolve) => setImmediate(resolve));
  const timedOut = nativeMessages.at(-1);
  assert.equal(timedOut.type, "capture_result");
  assert.equal(timedOut.ok, false);
  assert.match(timedOut.error, /timed out after 25 seconds/);

  await nativeMessageListener({ type: "capture_active_tab", request_id: "overlap" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(nativeMessages.at(-1).ok, false);
  assert.match(nativeMessages.at(-1).error, /already in progress/,
    "a second capture is rejected while the underlying browser request remains stalled");

  chrome.runtime.lastError = { message: "Native host has exited." };
  nativeDisconnectListener();
  chrome.runtime.lastError = null;
  assert.equal(timerCallbacks.length, 2, "capture timeout and disconnect retry are scheduled");
  assert.equal(disconnectCalls, 0, "disconnect handler does not call Port.disconnect");
  console.log("Native extension lifecycle mock passed: connect, handshake, bounded stalled capture, overlap guard, disconnect, retry.");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

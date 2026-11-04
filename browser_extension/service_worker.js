const NATIVE_HOST = "com.mortis.page_bridge";
const MAX_TEXT_CHARS = 120000;
const MAX_LINKS = 120;
let nativePort = null;
let connected = false;
let connecting = false;
let connectionError = "";
let armedTab = null;
let reconnectTimer = null;
let reconnectAttempt = 0;
let heartbeatTimer = null;
let lastHeartbeatAck = 0;
let captureInFlight = false;

function logLifecycle(event, details = {}) {
  console.info("[Mortis native]", event, {
    time: new Date().toISOString(),
    ...details,
  });
}

async function restoreArmedTab() {
  const saved = await chrome.storage.session.get("armedTab");
  armedTab = saved.armedTab || null;
}

function publishStatus() {
  chrome.runtime.sendMessage({ type: "status_changed", status: getStatus() }).catch(() => {});
}

function getStatus() {
  return {
    connected,
    connecting,
    error: connectionError,
    armed: armedTab,
    notice: "Page contents are read only when you arm and ask Mortis to eat a page.",
  };
}

function postNative(message) {
  if (!nativePort || !connected) return false;
  try {
    nativePort.postMessage(message);
    logLifecycle("message sent", { type: message.type });
    return true;
  } catch (error) {
    connected = false;
    connectionError = String(error).slice(0, 200);
    logLifecycle("postMessage failed", { type: message.type, error: connectionError });
    publishStatus();
    return false;
  }
}

function scheduleReconnect(reason) {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  const delay = Math.min(1000 * (2 ** reconnectAttempt), 30000);
  reconnectAttempt = Math.min(reconnectAttempt + 1, 5);
  logLifecycle("reconnect scheduled", { reason, delay });
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    ensureNative("disconnect-retry");
  }, delay);
}

function stopHeartbeat() {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  heartbeatTimer = null;
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    if (!connected) return;
    const nonce = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    postNative({ type: "heartbeat", nonce });
    logLifecycle("heartbeat sent", { nonce });
  }, 20000);
}

function ensureNative(reason = "request") {
  if (nativePort) return true;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  connecting = true;
  connectionError = "";
  logLifecycle("connectNative called", { host: NATIVE_HOST, reason });
  try {
    const port = chrome.runtime.connectNative(NATIVE_HOST);
    nativePort = port;
    port.onMessage.addListener((message) => {
      onNativeMessage(message, port).catch((error) => {
        connectionError = `Native message handler failed: ${String(error).slice(0, 180)}`;
        logLifecycle("native message handler failed", { error: connectionError });
        publishStatus();
      });
    });
    port.onDisconnect.addListener(() => {
      // Read lastError synchronously while Chrome exposes it in this event.
      const lastError = chrome.runtime.lastError;
      logLifecycle("onDisconnect", {
        error: lastError ? lastError.message : "",
        portWasCurrent: nativePort === port,
      });
      if (nativePort !== port) return;
      nativePort = null;
      connected = false;
      connecting = false;
      stopHeartbeat();
      if (!connectionError) {
        connectionError = lastError ? lastError.message : "Native host disconnected.";
      }
      publishStatus();
      // Reconnect promptly from the disconnect event. A delayed timer alone can
      // be lost if Chromium suspends the MV3 worker after the port disappears.
      scheduleReconnect(connectionError);
    });
    return true;
  } catch (error) {
    nativePort = null;
    connected = false;
    connecting = false;
    connectionError = String(error).slice(0, 200);
    logLifecycle("connectNative threw", { error: connectionError });
    scheduleReconnect(connectionError);
    return false;
  }
}

async function onNativeMessage(message, port) {
  logLifecycle("message received", {
    type: message && message.type ? message.type : "<invalid>",
    portWasCurrent: nativePort === port,
  });
  if (!message || typeof message !== "object" || typeof message.type !== "string") {
    connectionError = "Mortis native host sent an invalid message.";
    logLifecycle("invalid host message");
    publishStatus();
    return;
  }
  if (port !== nativePort) return;
  if (message.type === "host_connected") {
    connected = true;
    connecting = false;
    connectionError = "";
    reconnectAttempt = 0;
    await restoreArmedTab();
    postNative({ type: "bridge_ready", armed: armedTab });
    startHeartbeat();
    publishStatus();
    return;
  }
  if (message.type === "host_error") {
    connected = false;
    connecting = false;
    connectionError = message.message || "Native host could not connect.";
    logLifecycle("host_error", { error: connectionError });
    publishStatus();
    return;
  }
  if (message.type === "heartbeat_ack") {
    lastHeartbeatAck = Date.now();
    logLifecycle("heartbeat acknowledged", { nonce: message.nonce, at: lastHeartbeatAck });
    return;
  }
  if (message.type === "get_status") {
    postNative({ type: "bridge_ready", armed: armedTab });
    return;
  }
  if (message.type === "disarm") {
    armedTab = null;
    await chrome.storage.session.remove("armedTab");
    postNative({ type: "disarmed" });
    publishStatus();
    return;
  }
  if (message.type === "capture_active_tab") {
    await captureArmedTab(message.request_id);
  }
}

function extractPageSnapshot() {
  const maxTextChars = 120000;
  const text = document.body ? document.body.innerText : "";
  const links = Array.from(document.querySelectorAll("a[href]"))
    .map((anchor) => anchor.href)
    .filter((href) => href.startsWith("http://") || href.startsWith("https://"))
    .slice(0, 120)
    .map((href) => href.slice(0, 512));
  return {
    url: location.href,
    title: document.title || location.href,
    text: text.slice(0, maxTextChars),
    selected_text: String(window.getSelection() || "").slice(0, 12000),
    links,
  };
}

async function captureArmedTab(requestId) {
  if (captureInFlight) {
    postNative({ type: "capture_result", request_id: requestId, ok: false,
      error: "A browser capture is already in progress." });
    return;
  }
  captureInFlight = true;
  let captureSettled = false;
  let timeoutId;
  const captureOperation = (async () => {
    await restoreArmedTab();
    if (!armedTab || !Number.isInteger(armedTab.tabId)) {
      throw new Error("No browser tab is armed. Open the extension and choose Arm this tab.");
    }
    const tab = await chrome.tabs.get(armedTab.tabId);
    if (!tab.url || !/^https?:\/\//i.test(tab.url)) {
      throw new Error("This is a restricted browser page. Arm a normal http(s) webpage instead.");
    }
    const results = await chrome.scripting.executeScript({
      target: { tabId: armedTab.tabId },
      func: extractPageSnapshot,
    });
    const page = results && results[0] && results[0].result;
    if (!page || !page.url || !page.text) {
      throw new Error("The page did not expose readable text to the extension.");
    }
    if (new URL(page.url).origin !== new URL(armedTab.url).origin) {
      armedTab = null;
      await chrome.storage.session.remove("armedTab");
      throw new Error("The armed tab changed sites. Arm the current page again.");
    }
    return page;
  })();
  // If executeScript itself stalls, report a bounded failure. Keep the in-flight
  // guard until the underlying browser operation settles so requests cannot overlap.
  captureOperation.then(
    () => { captureSettled = true; captureInFlight = false; },
    () => { captureSettled = true; captureInFlight = false; },
  );
  const timeout = new Promise((_, reject) => {
    timeoutId = setTimeout(
      () => reject(new Error("Browser page capture timed out after 25 seconds.")),
      25000,
    );
  });
  try {
    const page = await Promise.race([captureOperation, timeout]);
    postNative({ type: "capture_result", request_id: requestId, ok: true, page });
  } catch (error) {
    postNative({ type: "capture_result", request_id: requestId, ok: false,
      error: String(error && error.message ? error.message : error).slice(0, 360) });
  } finally {
    clearTimeout(timeoutId);
    if (captureSettled) captureInFlight = false;
  }
}

async function armCurrentTab() {
  ensureNative("arm-tab");
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab || !Number.isInteger(tab.id) || !tab.url || !/^https?:\/\//i.test(tab.url)) {
    return { ...getStatus(), error: "This page is restricted. Open a normal http(s) webpage and try again." };
  }
  armedTab = { tabId: tab.id, url: tab.url.slice(0, 4096), title: (tab.title || tab.url).slice(0, 500) };
  await chrome.storage.session.set({ armedTab });
  postNative({ type: "armed", page: armedTab });
  const notice = connected
    ? "This tab is armed for Mortis."
    : "This tab is armed in the extension. Capture will start when Mortis reconnects.";
  publishStatus();
  return { ...getStatus(), notice };
}

async function disarmTab() {
  armedTab = null;
  await chrome.storage.session.remove("armedTab");
  postNative({ type: "disarmed" });
  publishStatus();
  return getStatus();
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || !message || typeof message.type !== "string") return false;
  logLifecycle("extension request", { type: message.type });
  if (message.type === "get_status") {
    ensureNative("popup-status");
    restoreArmedTab().then(() => sendResponse(getStatus()));
    return true;
  }
  if (message.type === "arm_tab") {
    armCurrentTab().then(sendResponse).catch((error) =>
      sendResponse({ ...getStatus(), error: String(error).slice(0, 240) }));
    return true;
  }
  if (message.type === "disarm_tab") {
    disarmTab().then(sendResponse);
    return true;
  }
  return false;
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  await restoreArmedTab();
  if (armedTab && armedTab.tabId === tabId) disarmTab();
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (!changeInfo.url) return;
  await restoreArmedTab();
  if (!armedTab || armedTab.tabId !== tabId) return;
  try {
    if (new URL(changeInfo.url).origin !== new URL(armedTab.url).origin) {
      await disarmTab();
      return;
    }
  } catch (_error) {
    await disarmTab();
    return;
  }
  armedTab = { tabId, url: changeInfo.url.slice(0, 4096), title: (tab.title || changeInfo.url).slice(0, 500) };
  await chrome.storage.session.set({ armedTab });
  postNative({ type: "armed", page: armedTab });
  publishStatus();
});

chrome.runtime.onStartup.addListener(() => ensureNative("browser-startup"));

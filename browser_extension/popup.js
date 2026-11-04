const connection = document.getElementById("connection");
const armed = document.getElementById("armed");
const message = document.getElementById("message");

function showStatus(status) {
  connection.dataset.state = status.connected ? "connected" : status.connecting ? "connecting" : "disconnected";
  if (status.connected) {
    connection.textContent = "Connected to the Mortis desktop pet.";
  } else if (status.connecting) {
    connection.textContent = "Connecting to Mortis desktop…";
  } else {
    connection.textContent = "Mortis desktop not connected.";
  }
  if (status.armed) {
    armed.textContent = `Armed: ${status.armed.title || status.armed.url}\n${status.armed.url}`;
  } else {
    armed.textContent = "No page armed. Click Arm this tab to select the current page.";
  }
  message.textContent = status.error || status.notice || "";
}

chrome.runtime.onMessage.addListener((incoming) => {
  if (incoming && incoming.type === "status_changed") showStatus(incoming.status);
});

chrome.runtime.sendMessage({ type: "get_status" }, (response) => {
  if (chrome.runtime.lastError) {
    console.warn("[Mortis popup] status request failed:", chrome.runtime.lastError.message);
    message.textContent = chrome.runtime.lastError.message;
    return;
  }
  showStatus(response || { connected: false, connecting: false, error: "No response from Mortis extension." });
});

document.getElementById("arm").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "arm_tab" }, (response) => {
    if (chrome.runtime.lastError) {
      console.warn("[Mortis popup] arm request failed:", chrome.runtime.lastError.message);
      message.textContent = chrome.runtime.lastError.message;
      return;
    }
    showStatus(response || { connected: false, connecting: false, error: "Could not arm this tab." });
  });
});

document.getElementById("disarm").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "disarm_tab" }, (response) => {
    if (chrome.runtime.lastError) {
      console.warn("[Mortis popup] disarm request failed:", chrome.runtime.lastError.message);
      message.textContent = chrome.runtime.lastError.message;
      return;
    }
    showStatus(response || { connected: false });
  });
});

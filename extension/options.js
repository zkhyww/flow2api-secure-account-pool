const DEFAULT_SETTINGS = {
  serverUrl: "ws://127.0.0.1:8000/captcha_ws",
  pluginSession: "",
  instanceId: "",
  routeKey: "",
  clientLabel: "",
  refreshIntervalMinutes: "120",
  autoImportEnabled: true,
  autoImportIntervalMinutes: "30",
  lastAutoImportAt: "",
  lastAutoImportStatus: "",
  lastAutoImportErrorClass: ""
};

const $ = (id) => document.getElementById(id);

function setStatus(message, isError = false) {
  const status = $("status");
  status.textContent = message;
  status.style.color = isError ? "#b91c1c" : "#065f46";
}

function isValidWsUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "ws:" || url.protocol === "wss:";
  } catch (_error) { return false; }
}

function loadSettings() {
  chrome.storage.local.get(DEFAULT_SETTINGS, (stored) => {
    $("serverUrl").value = stored.serverUrl || DEFAULT_SETTINGS.serverUrl;
    $("routeIdentity").textContent = stored.routeKey
      ? `${stored.clientLabel || "worker"} / ${stored.routeKey}`
      : "尚未配对";
    $("refreshIntervalMinutes").value = stored.refreshIntervalMinutes || "120";
    $("autoImportEnabled").checked = stored.autoImportEnabled !== false;
    $("autoImportIntervalMinutes").value = stored.autoImportIntervalMinutes || "30";
    $("lastAutoImportStatus").textContent = stored.lastAutoImportAt
      ? `最近导入：${stored.lastAutoImportStatus || "unknown"} ${new Date(stored.lastAutoImportAt).toLocaleString("zh-CN", { hour12: false })}`
      : "最近导入：尚未运行";
  });
}

async function pairExtension() {
  const serverUrl = $("serverUrl").value.trim();
  const pairingHandle = $("pairingHandle").value.trim();
  if (!isValidWsUrl(serverUrl) || !pairingHandle) {
    setStatus("请填写有效的服务地址和一次性配对码。", true);
    return;
  }
  const response = await chrome.runtime.sendMessage({ type: "flow2api_pair", pairingHandle, serverUrl });
  $("pairingHandle").value = "";
  if (!response || response.success !== true) {
    setStatus("配对失败或配对码已过期。", true);
    return;
  }
  setStatus("配对成功。");
  loadSettings();
}

function openLocalManagement() {
  chrome.tabs.create({ url: "http://127.0.0.1:8000/manage", active: true });
  setStatus("已打开管理页，将自动识别并连接插件。");
}

function saveImportSettings() {
  const settings = {
    refreshIntervalMinutes: String($("refreshIntervalMinutes").value || "120").trim(),
    autoImportEnabled: $("autoImportEnabled").checked,
    autoImportIntervalMinutes: String($("autoImportIntervalMinutes").value || "30").trim()
  };
  chrome.storage.local.set(settings, () => setStatus("导入设置已保存。"));
}

async function importCurrentAccount() {
  $("importBtn").disabled = true;
  try {
    const response = await chrome.runtime.sendMessage({ type: "flow2api_import_current_account" });
    if (!response || response.success !== true) throw new Error("import_failed");
    const payload = response.payload || {};
    setStatus(`导入完成：新增 ${payload.added || 0}，更新 ${payload.updated || 0}。`);
    loadSettings();
  } catch (_error) {
    setStatus("导入失败，请确认当前 Profile 已登录 Flow。", true);
  } finally {
    $("importBtn").disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  loadSettings();
  $("autoConnectBtn").addEventListener("click", openLocalManagement);
  $("pairBtn").addEventListener("click", pairExtension);
  $("saveBtn").addEventListener("click", saveImportSettings);
  $("importBtn").addEventListener("click", importCurrentAccount);
});

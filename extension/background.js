let ws = null;
let reconnectTimeout = null;
let heartbeatInterval = null;
let tokenQueue = Promise.resolve();
let importQueue = Promise.resolve();

const CAPABILITY_MARKER = "yingce-flow2api-worker-v1";
const ACCOUNT_IMPORT_ALARM = "flow2api-auto-import-account";
const LABS_SESSION_COOKIE = "__Secure-next-auth.session-token";
const GOOGLE_COOKIE_NAMES = [
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID",
    "__Secure-3PAPISID", "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDCC", "__Secure-3PSIDCC"
];
const GOOGLE_AUTH_COOKIE_GROUPS = [
    ["SID", "SAPISID"],
    ["__Secure-1PSID", "__Secure-1PAPISID"],
    ["__Secure-3PSID", "__Secure-3PAPISID"]
];
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

function createInstanceId() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
        return globalThis.crypto.randomUUID();
    }
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}

function ensureInstanceSettings(stored) {
    const instanceId = (stored.instanceId || createInstanceId()).trim();
    const settings = { instanceId };
    if (!stored.instanceId) chrome.storage.local.set(settings);
    return settings;
}

function getSettings() {
    return new Promise((resolve) => {
        chrome.storage.local.get(DEFAULT_SETTINGS, (stored) => {
            const identity = ensureInstanceSettings(stored);
            resolve({
                serverUrl: (stored.serverUrl || DEFAULT_SETTINGS.serverUrl).trim(),
                pluginSession: (stored.pluginSession || "").trim(),
                instanceId: identity.instanceId,
                routeKey: (stored.routeKey || "").trim(),
                clientLabel: (stored.clientLabel || "").trim(),
                refreshIntervalMinutes: String(stored.refreshIntervalMinutes || "120").trim(),
                autoImportEnabled: stored.autoImportEnabled !== false,
                autoImportIntervalMinutes: String(stored.autoImportIntervalMinutes || "30").trim()
            });
        });
    });
}

function getBackendBaseUrl(serverUrl) {
    const url = new URL(serverUrl || DEFAULT_SETTINGS.serverUrl);
    if (url.protocol === "ws:") url.protocol = "http:";
    else if (url.protocol === "wss:") url.protocol = "https:";
    else throw new Error("invalid_server_scheme");
    url.pathname = "";
    url.search = "";
    url.hash = "";
    return url.toString().replace(/\/$/, "");
}

async function exchangePairingHandle(pairingHandle, serverUrl) {
    const settings = await getSettings();
    const effectiveServerUrl = (serverUrl || settings.serverUrl).trim();
    const response = await fetch(`${getBackendBaseUrl(effectiveServerUrl)}/api/plugin/pair/exchange`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pairing_handle: String(pairingHandle || "").trim() })
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload || payload.capability_marker !== CAPABILITY_MARKER) {
        throw new Error("pairing_exchange_failed");
    }
    const publicSettings = {
        serverUrl: effectiveServerUrl,
        pluginSession: String(payload.plugin_session || ""),
        instanceId: String(payload.instance_id || settings.instanceId),
        routeKey: String(payload.route_key || ""),
        clientLabel: String(payload.client_label || "")
    };
    await new Promise((resolve, reject) => chrome.storage.local.set(publicSettings, () => {
        if (chrome.runtime.lastError) reject(new Error("storage_failed"));
        else resolve();
    }));
    return publicSettings;
}

async function consumeBootstrap() {
    try {
        const response = await fetch(chrome.runtime.getURL("flow2api-bootstrap.json"), { cache: "no-store" });
        if (!response.ok) return false;
        const bootstrap = await response.json();
        if (!bootstrap || bootstrap.capability_marker !== CAPABILITY_MARKER || !bootstrap.pairing_handle) return false;
        await exchangePairingHandle(bootstrap.pairing_handle, bootstrap.server_url);
        return true;
    } catch (_error) {
        return false;
    }
}

function closeSocket() {
    const socket = ws;
    ws = null;
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    heartbeatInterval = null;
    if (reconnectTimeout) clearTimeout(reconnectTimeout);
    reconnectTimeout = null;
    if (socket) {
        try { socket.close(); } catch (_error) { /* no sensitive diagnostics */ }
    }
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function waitForTabReady(tabId, timeoutMs = 12000) {
    return new Promise((resolve) => {
        let settled = false;
        const finish = () => {
            if (settled) return;
            settled = true;
            chrome.tabs.onUpdated.removeListener(onUpdated);
            clearTimeout(timer);
            resolve();
        };
        const onUpdated = (updatedTabId, changeInfo) => {
            if (updatedTabId === tabId && changeInfo.status === "complete") finish();
        };
        const timer = setTimeout(finish, timeoutMs);
        chrome.tabs.onUpdated.addListener(onUpdated);
        chrome.tabs.get(tabId, (tab) => {
            if (chrome.runtime.lastError || (tab && tab.status === "complete")) finish();
        });
    });
}

async function connectWS() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    let settings = await getSettings();
    if (!settings.pluginSession) {
        await consumeBootstrap();
        settings = await getSettings();
    }
    if (!settings.pluginSession) return;

    const url = new URL(settings.serverUrl || DEFAULT_SETTINGS.serverUrl);
    url.search = "";
    const socket = new WebSocket(url.toString(), ["flow2api-plugin", `flow2api-session.${settings.pluginSession}`]);
    ws = socket;

    socket.onopen = () => {
        if (ws !== socket) return;
        console.log("[Flow2API] stage=connected status=ready");
        socket.send(JSON.stringify({ type: "register", capability_marker: CAPABILITY_MARKER }));
        if (heartbeatInterval) clearInterval(heartbeatInterval);
        heartbeatInterval = setInterval(() => {
            if (ws === socket && socket.readyState === WebSocket.OPEN) {
                socket.send(JSON.stringify({ type: "ping" }));
            }
        }, 20000);
    };

    socket.onmessage = (event) => {
        let data;
        try { data = JSON.parse(event.data); } catch (_error) { return; }
        if (data.type === "get_token") {
            tokenQueue = tokenQueue.then(() => handleGetToken(data, socket)).catch(() => {
                console.error("[Flow2API] stage=captcha status=failed error_class=queue_error");
            });
        }
    };

    socket.onclose = () => {
        if (ws === socket) {
            ws = null;
            if (heartbeatInterval) clearInterval(heartbeatInterval);
            heartbeatInterval = null;
            if (reconnectTimeout) clearTimeout(reconnectTimeout);
            reconnectTimeout = setTimeout(connectWS, 2000);
        }
    };
    socket.onerror = () => console.warn("[Flow2API] stage=socket status=failed error_class=connection_error");
}

function projectFlowUrl(projectId) {
    const normalized = String(projectId || "").trim();
    return normalized
        ? `https://labs.google/fx/tools/flow/project/${encodeURIComponent(normalized)}`
        : "https://labs.google/fx/tools/flow";
}

async function handleGetToken(data, socket) {
    let newTabId = null;
    try {
        const targetUrl = projectFlowUrl(data.project_id);
        const matchingTabs = await chrome.tabs.query({ url: `${targetUrl}*` });
        const targetTab = matchingTabs.find(tab => tab.id) || await chrome.tabs.create({ url: targetUrl, active: false });
        newTabId = matchingTabs.length ? null : targetTab.id;
        await waitForTabReady(targetTab.id);
        await sleep(newTabId ? 1200 : 300);

        const scriptTimeoutMs = data.action === "VIDEO_GENERATION" ? 30000 : 20000;
        const results = await chrome.scripting.executeScript({
            target: { tabId: targetTab.id },
            world: "MAIN",
            func: async (action, timeoutMs) => new Promise((resolve, reject) => {
                let settled = false;
                const finish = (fn, value) => { if (!settled) { settled = true; fn(value); } };
                try {
                    const run = () => grecaptcha.enterprise.ready(() => {
                        grecaptcha.enterprise.execute("6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV", { action })
                            .then(token => {
                                const brands = navigator.userAgentData && Array.isArray(navigator.userAgentData.brands)
                                    ? navigator.userAgentData.brands
                                        .map(brand => `"${String(brand.brand).replace(/"/g, "")}";v="${String(brand.version).replace(/"/g, "")}"`)
                                        .join(", ")
                                    : "";
                                const languages = Array.isArray(navigator.languages) && navigator.languages.length
                                    ? navigator.languages
                                    : [navigator.language || ""];
                                finish(resolve, {
                                    token,
                                    fingerprint: {
                                        user_agent: String(navigator.userAgent || ""),
                                        accept_language: languages.filter(Boolean).join(","),
                                        sec_ch_ua: brands,
                                        sec_ch_ua_mobile: navigator.userAgentData && navigator.userAgentData.mobile ? "?1" : "?0",
                                        sec_ch_ua_platform: `"${String((navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "").replace(/"/g, "")}"`
                                    }
                                });
                            })
                            .catch(() => finish(reject, "captcha_failed"));
                    });
                    if (typeof grecaptcha !== "undefined" && grecaptcha.enterprise) run();
                    else {
                        const script = document.createElement("script");
                        script.src = "https://www.google.com/recaptcha/enterprise.js?render=6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV";
                        script.onload = run;
                        script.onerror = () => finish(reject, "script_load_failed");
                        document.head.appendChild(script);
                    }
                    setTimeout(() => finish(reject, "timeout"), timeoutMs);
                } catch (_error) { finish(reject, "execution_failed"); }
            }),
            args: [data.action || "IMAGE_GENERATION", scriptTimeoutMs]
        });
        const solveBundle = results && results[0] && results[0].result;
        const token = solveBundle && solveBundle.token;
        const fingerprint = solveBundle && solveBundle.fingerprint;
        if (!token || !fingerprint) throw new Error("captcha_failed");
        if (ws !== socket || socket.readyState !== WebSocket.OPEN) throw new Error("socket_owner_changed");
        socket.send(JSON.stringify({ req_id: data.req_id, status: "success", token, fingerprint }));
    } catch (error) {
        if (ws === socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({
                req_id: data.req_id,
                status: "error",
                error: error && error.message === "timeout" ? "timeout" : "captcha_failed"
            }));
        }
    } finally {
        if (newTabId) {
            try { await chrome.tabs.remove(newTabId); } catch (_error) { /* no-op */ }
        }
    }
}

function getCookie(details) {
    return new Promise((resolve, reject) => chrome.cookies.get(details, (cookie) => {
        if (chrome.runtime.lastError) reject(new Error("cookie_read_failed"));
        else resolve(cookie || null);
    }));
}

function getCookies(details) {
    return new Promise((resolve, reject) => chrome.cookies.getAll(details, (cookies) => {
        if (chrome.runtime.lastError) reject(new Error("cookie_read_failed"));
        else resolve(cookies || []);
    }));
}

async function getLabsSessionToken() {
    for (const url of ["https://labs.google/fx", "https://labs.google/fx/tools/flow", "https://labs.google/"]) {
        const cookie = await getCookie({ url, name: LABS_SESSION_COOKIE });
        if (cookie && cookie.value) return cookie.value;
    }
    return "";
}

async function getGoogleCookies() {
    const cookieMap = new Map();
    for (const query of [
        { domain: "google.com" },
        { url: "https://accounts.google.com/" },
        { url: "https://www.google.com/" },
        { url: "https://labs.google/" }
    ]) {
        for (const cookie of await getCookies(query)) {
            if (!GOOGLE_COOKIE_NAMES.includes(cookie.name) || !cookie.value) continue;
            const existing = cookieMap.get(cookie.name);
            if (!existing || (cookie.expirationDate || 0) >= (existing.expirationDate || 0)) {
                cookieMap.set(cookie.name, {
                    name: cookie.name,
                    value: cookie.value,
                    domain: cookie.domain || "",
                    path: cookie.path || "/",
                    expirationDate: cookie.expirationDate || null
                });
            }
        }
    }
    return Array.from(cookieMap.values());
}

async function importCurrentAccount(reason = "manual") {
    const settings = await getSettings();
    if (!settings.pluginSession) throw new Error("not_paired");
    const sessionToken = await getLabsSessionToken();
    if (!sessionToken) throw new Error("not_logged_in");
    const googleCookies = await getGoogleCookies();
    const names = new Set(googleCookies.map(cookie => cookie.name));
    if (!GOOGLE_AUTH_COOKIE_GROUPS.some(group => group.every(name => names.has(name)))) {
        throw new Error("incomplete_login_state");
    }

    const response = await fetch(`${getBackendBaseUrl(settings.serverUrl)}/api/plugin/import-current-account`, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${settings.pluginSession}`
        },
        body: JSON.stringify({
            session_token: sessionToken,
            google_cookies: JSON.stringify(googleCookies),
            refresh_interval_minutes: parseInt(settings.refreshIntervalMinutes, 10) || 120
        })
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload || payload.success !== true) throw new Error("import_failed");
    chrome.storage.local.set({
        lastAutoImportAt: new Date().toISOString(),
        lastAutoImportStatus: "success",
        lastAutoImportErrorClass: ""
    });
    console.log(`[Flow2API] stage=account_import status=success reason=${reason} added=${payload.added || 0} updated=${payload.updated || 0}`);
    return { added: payload.added || 0, updated: payload.updated || 0, action: payload.action || "updated" };
}

function enqueueImport(reason) {
    importQueue = importQueue.then(
        () => importCurrentAccount(reason),
        () => importCurrentAccount(reason)
    );
    const currentImport = importQueue;
    importQueue = currentImport.catch(() => undefined);
    return currentImport.catch((error) => {
        const errorClass = error && error.message ? error.message : "import_failed";
        chrome.storage.local.set({
            lastAutoImportAt: new Date().toISOString(),
            lastAutoImportStatus: "failed",
            lastAutoImportErrorClass: errorClass
        });
        console.warn(`[Flow2API] stage=account_import status=failed error_class=${errorClass}`);
        throw error;
    });
}

async function configureAccountImportAlarm() {
    const settings = await getSettings();
    await chrome.alarms.clear(ACCOUNT_IMPORT_ALARM);
    if (!settings.autoImportEnabled) return;
    const interval = Math.max(5, parseInt(settings.autoImportIntervalMinutes, 10) || 30);
    chrome.alarms.create(ACCOUNT_IMPORT_ALARM, { delayInMinutes: 1, periodInMinutes: interval });
}

async function injectLocalConnectIntoOpenTabs() {
    const tabs = await chrome.tabs.query({
        url: ["http://127.0.0.1/*", "http://localhost/*"]
    });
    await Promise.all(tabs.filter(tab => tab.id).map(async (tab) => {
        try {
            await chrome.scripting.executeScript({
                target: { tabId: tab.id },
                files: ["local-connect.js"]
            });
        } catch (_error) {
            // A tab can disappear or navigate between query and injection.
        }
    }));
}

chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== "local") return;
    if (changes.serverUrl || changes.pluginSession) {
        closeSocket();
        connectWS();
    }
    if (changes.autoImportEnabled || changes.autoImportIntervalMinutes || changes.refreshIntervalMinutes) {
        configureAccountImportAlarm();
    }
});

chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === ACCOUNT_IMPORT_ALARM) enqueueImport("auto").catch(() => {});
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message) return false;
    if (message.type === "flow2api_pair") {
        if (
            message.relay_marker !== undefined
            && (message.relay_marker !== "flow2api_local_connect_v1" || !String(message.request_id || "").trim())
        ) {
            sendResponse({ success: false, error_class: "invalid_connect_request" });
            return false;
        }
        exchangePairingHandle(message.pairingHandle, message.serverUrl)
            .then(async () => {
                closeSocket();
                await connectWS();
                try {
                    const binding = await enqueueImport("pairing");
                    sendResponse({ success: true, account_bound: true, action: binding.action || "updated" });
                } catch (error) {
                    sendResponse({
                        success: false,
                        account_bound: false,
                        error_class: error && error.message ? error.message : "account_binding_failed"
                    });
                }
            })
            .catch(() => sendResponse({ success: false, account_bound: false, error_class: "pairing_failed" }));
        return true;
    }
    if (message.type === "flow2api_public_identity") {
        getSettings()
            .then(settings => sendResponse({
                success: true,
                instanceId: settings.instanceId,
                paired: Boolean(settings.pluginSession)
            }))
            .catch(() => sendResponse({ success: false }));
        return true;
    }
    if (message.type === "flow2api_import_current_account") {
        enqueueImport("manual")
            .then(payload => sendResponse({ success: true, payload }))
            .catch(error => sendResponse({ success: false, error_class: error.message || "import_failed" }));
        return true;
    }
    return false;
});

injectLocalConnectIntoOpenTabs().finally(() => consumeBootstrap()).finally(() => {
    connectWS();
    configureAccountImportAlarm();
});

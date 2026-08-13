(() => {
    if (globalThis.__flow2apiLocalConnectInstalled) return;
    globalThis.__flow2apiLocalConnectInstalled = true;

    const REQUEST_MARKER = "flow2api_local_connect_v1";
    const RESULT_MARKER = "flow2api_local_connect_result_v1";
    const PROBE_MARKER = "flow2api_local_connect_probe_v1";
    const READY_MARKER = "flow2api_local_connect_ready_v1";

    function postReady(requestId = "") {
        chrome.runtime.sendMessage({ type: "flow2api_public_identity" }, (response) => {
            window.postMessage({
                marker: READY_MARKER,
                requestId,
                instanceId: response && response.success === true ? String(response.instanceId || "") : "",
                paired: Boolean(response && response.paired)
            }, window.location.origin);
        });
    }

    function postResult(requestId, success, errorClass = "", accountBound = false) {
        window.postMessage({
            marker: RESULT_MARKER,
            requestId,
            success: success === true,
            account_bound: accountBound === true,
            error_class: success ? "" : (errorClass || "pairing_failed")
        }, window.location.origin);
    }

    window.addEventListener("message", (event) => {
        if (event.source !== window || event.origin !== window.location.origin) return;
        const message = event.data;
        if (message && message.marker === PROBE_MARKER) {
            postReady(String(message.requestId || ""));
            return;
        }
        if (!message || message.marker !== REQUEST_MARKER) return;
        const requestId = String(message.requestId || "").trim();
        const pairingHandle = String(message.pairingHandle || "").trim();
        if (!requestId || !pairingHandle) {
            postResult(requestId, false, "invalid_connect_request");
            return;
        }
        chrome.runtime.sendMessage({
            type: "flow2api_pair",
            relay_marker: REQUEST_MARKER,
            request_id: requestId,
            pairingHandle
        }, (response) => {
            if (chrome.runtime.lastError || !response) {
                postResult(requestId, false, "extension_upgrade_required");
                return;
            }
            postResult(
                requestId,
                response.success === true && response.account_bound === true,
                String(response.error_class || "account_binding_failed"),
                response.account_bound === true
            );
        });
    });

    postReady();
})();

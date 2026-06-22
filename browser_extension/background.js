const WS_URL = "ws://127.0.0.1:8765";

let socket = null;
let isConnected = false;
let currentToken = null;
const CONTEXT_MENU_ID = "download_with_viddownloader";

function notify(message) {
    chrome.notifications.create({
        type: "basic",
        iconUrl: "icon.ico",
        title: "VidDownloader",
        message
    });
}

function loadToken() {
    return new Promise((resolve) => {
        chrome.storage.local.get(["vid_token"], (result) => {
            const token = result.vid_token || "";
            currentToken = token || null;
            resolve(currentToken);
        });
    });
}

function connectWebSocket() {
    socket = new WebSocket(WS_URL);

    socket.onopen = () => {
        isConnected = true;
    };

    socket.onclose = () => {
        isConnected = false;
        setTimeout(connectWebSocket, 5000);
    };

    socket.onerror = () => {
        isConnected = false;
    };
}

function sendViaOneShotSocket(payload) {
    return new Promise((resolve, reject) => {
        let done = false;
        const ws = new WebSocket(WS_URL);
        const timeoutId = setTimeout(() => {
            if (done) return;
            done = true;
            try { ws.close(); } catch (e) {}
            reject(new Error("timeout"));
        }, 3000);

        ws.onopen = () => {
            if (done) return;
            try {
                ws.send(JSON.stringify(payload));
                done = true;
                clearTimeout(timeoutId);
                try { ws.close(); } catch (e) {}
                resolve();
            } catch (err) {
                done = true;
                clearTimeout(timeoutId);
                reject(err);
            }
        };

        ws.onerror = () => {
            if (done) return;
            done = true;
            clearTimeout(timeoutId);
            reject(new Error("ws_error"));
        };
    });
}

function ensureContextMenu() {
    chrome.contextMenus.removeAll(() => {
        chrome.contextMenus.create({
            id: CONTEXT_MENU_ID,
            title: "Download with VidDownloader",
            contexts: ["link", "video", "page"]
        }, () => {
            const err = chrome.runtime.lastError;
            if (err) {
                console.warn("Failed to create context menu:", err.message);
            }
        });
    });
}

chrome.runtime.onInstalled.addListener(() => {
    ensureContextMenu();
});

chrome.runtime.onStartup.addListener(() => {
    ensureContextMenu();
});

chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.vid_token) {
        currentToken = changes.vid_token.newValue || null;
    }
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    if (info.menuItemId !== CONTEXT_MENU_ID) {
        return;
    }

    const safeTab = tab || {};
    const targetUrl = info.linkUrl || info.srcUrl || info.pageUrl || safeTab.url || "";
    if (!targetUrl) {
        notify("No valid URL found.");
        return;
    }

    if (!currentToken) {
        await loadToken();
    }

    if (!currentToken) {
        chrome.runtime.openOptionsPage();
        notify("Please set the desktop app token in extension options first.");
        return;
    }

    const payload = {
        token: currentToken,
        url: targetUrl,
        title: safeTab.title || targetUrl,
        page_url: safeTab.url || info.pageUrl || "",
        thumbnail: "",
        format: "MP4",
        quality: "1080p",
        subtitle: "None",
        auto_download: true,
        bandwidth_limit_kbps: 0,
        post_action: "none",
        post_download_script: "",
        schedule_repeat: "none"
    };

    try {
        if (isConnected && socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify(payload));
            notify("Link sent to desktop app.");
            return;
        }
        await sendViaOneShotSocket(payload);
        notify("Link sent to desktop app.");
    } catch (err) {
        notify("Cannot reach desktop app. Make sure VidDownloader is running, then try again.");
    }
});

(async () => {
    ensureContextMenu();
    await loadToken();
    connectWebSocket();
})();

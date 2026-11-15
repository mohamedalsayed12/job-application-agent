const DEFAULT_API = "http://127.0.0.1:8000";

async function settings() {
  const stored = await chrome.storage.sync.get(["apiBase", "token"]);
  return {
    apiBase: (stored.apiBase || DEFAULT_API).replace(/\/$/, ""),
    token: stored.token || "",
  };
}

async function fetchReady() {
  const { apiBase, token } = await settings();
  if (!token) return [];
  const response = await fetch(`${apiBase}/extension/applications/ready`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) return [];
  return response.json();
}

async function reportStatus(id, status, error) {
  const { apiBase, token } = await settings();
  await fetch(`${apiBase}/extension/applications/${id}/status`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ status, error: error || null }),
  });
}

function urlsMatch(a, b) {
  try {
    const left = new URL(a);
    const right = new URL(b);
    return left.origin === right.origin && left.pathname.replace(/\/$/, "") === right.pathname.replace(/\/$/, "");
  } catch {
    return a === b;
  }
}

async function fillActiveTab() {
  const { apiBase, token } = await settings();
  if (!token) return { ok: false, error: "Set an extension token in the popup." };
  const ready = await fetchReady();
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !tab.url) return { ok: false, error: "No active tab." };
  const match = ready.find((row) => urlsMatch(row.apply_url, tab.url));
  if (!match) return { ok: false, error: "No ready fill plan for this page." };
  const plan = match.fill_plan || { steps: [] };
  for (const step of plan.steps || []) {
    if (step.action === "click" || step.action === "submit") {
      return { ok: false, error: "Refusing a plan that tries to click Submit." };
    }
  }
  const result = await chrome.tabs.sendMessage(tab.id, {
    type: "jaa-fill",
    plan,
    apiBase,
    token,
  });
  if (result?.ok) {
    await reportStatus(match.id, "filled_awaiting_review");
    await chrome.action.setBadgeText({ text: "!" });
    await chrome.action.setBadgeBackgroundColor({ color: "#0a7" });
    try {
      chrome.notifications.create({
        type: "basic",
        title: "Application ready to review",
        message: "Fields are filled. Review the form and submit yourself.",
      });
    } catch (_) {
      /* notifications optional */
    }
    return { ok: true };
  }
  await reportStatus(match.id, "failed", result?.error || "Fill failed");
  return { ok: false, error: result?.error || "Fill failed" };
}

chrome.alarms.create("jaa-poll", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== "jaa-poll") return;
  const ready = await fetchReady();
  await chrome.action.setBadgeText({ text: ready.length ? String(ready.length) : "" });
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "jaa-fill-active") {
    fillActiveTab().then(sendResponse);
    return true;
  }
  return false;
});

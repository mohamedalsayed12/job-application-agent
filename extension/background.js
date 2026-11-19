const DEFAULT_API = "http://127.0.0.1:8000";

async function settings() {
  const stored = await chrome.storage.local.get(["apiBase", "token"]);
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

// Fetched here, not in the content script: a page-origin fetch with a token header
// needs a CORS preflight the app does not answer, while host_permissions exempt this one.
async function fetchCv(applicationId) {
  const { apiBase, token } = await settings();
  const response = await fetch(`${apiBase}/extension/cv?application_id=${encodeURIComponent(applicationId)}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new Error("Could not download CV");
  const bytes = new Uint8Array(await response.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  const docx = (response.headers.get("Content-Disposition") || "").includes(".docx");
  return {
    name: docx ? "cv.docx" : "cv.pdf",
    type: response.headers.get("Content-Type") || "application/pdf",
    base64: btoa(binary),
  };
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
  const { token } = await settings();
  if (!token) return { ok: false, error: "Set an extension token in the popup." };
  const ready = await fetchReady();
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !tab.url) return { ok: false, error: "No active tab." };
  const match = ready.find((row) => urlsMatch(row.apply_url, tab.url));
  if (!match) return { ok: false, error: "No ready fill plan for this page." };
  const plan = match.fill_plan || { steps: [] };
  const allowed = new Set(["fill", "select", "check", "upload_cv"]);
  for (const step of plan.steps || []) {
    if (!allowed.has(step.action)) {
      return { ok: false, error: "Refusing a plan with a disallowed action." };
    }
  }
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "jaa-ping" });
  } catch (_) {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  }
  let cv = null;
  if ((plan.steps || []).some((step) => step.action === "upload_cv")) {
    try {
      cv = await fetchCv(match.id);
    } catch (err) {
      await reportStatus(match.id, "failed", String(err));
      return { ok: false, error: String(err) };
    }
  }
  const result = await chrome.tabs.sendMessage(tab.id, { type: "jaa-fill", plan, cv });
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

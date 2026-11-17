const DEFAULT_API = "http://127.0.0.1:8000";
const ALLOWED = new Set(["fill", "select", "check", "upload_cv"]);

async function settings() {
  const stored = await chrome.storage.local.get(["apiBase", "token"]);
  return {
    apiBase: (stored.apiBase || DEFAULT_API).replace(/\/$/, ""),
    token: stored.token || "",
  };
}

async function api(path, options) {
  const { apiBase, token } = await settings();
  const response = await fetch(`${apiBase}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(options && options.headers),
    },
  });
  return response;
}

async function fetchReady() {
  const { token } = await settings();
  if (!token) return [];
  const response = await api("/extension/applications/ready");
  if (!response.ok) return [];
  return response.json();
}

async function reportStatus(id, status, error) {
  await api(`/extension/applications/${id}/status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status, error: error || null }),
  });
}

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

async function fetchCv(applicationId) {
  const response = await api(`/extension/cv?application_id=${encodeURIComponent(applicationId)}`);
  if (!response.ok) return null;
  const type = response.headers.get("Content-Type") || "application/pdf";
  const name = (response.headers.get("Content-Disposition") || "").includes(".docx") ? "cv.docx" : "cv.pdf";
  return { base64: toBase64(await response.arrayBuffer()), type, name };
}

async function inject(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: "jaa-ping" });
  } catch (_) {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
  }
}

async function fillActiveTab(applicationId) {
  const { token } = await settings();
  if (!token) return { ok: false, error: "Set an extension token in the popup." };
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return { ok: false, error: "No active tab." };
  const ready = await fetchReady();
  const match = applicationId
    ? ready.find((row) => row.id === applicationId)
    : ready.length === 1
      ? ready[0]
      : null;
  if (!match) {
    return { ok: false, error: ready.length ? "Choose which application to fill." : "No application is ready to fill." };
  }
  await inject(tab.id);
  let highlighted = "";
  // shortcut: two passes, so one Next click. Upgrade when a form has more than two steps.
  for (let pass = 0; pass < 2; pass += 1) {
    const scan = await chrome.tabs.sendMessage(tab.id, { type: "jaa-scan" });
    if (!scan?.fields?.length) {
      if (pass === 0) return { ok: false, error: "No form fields on this page." };
      break;
    }
    const planned = await api(`/extension/applications/${match.id}/plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fields: scan.fields }),
    });
    if (!planned.ok) return { ok: false, error: "Could not build a fill plan." };
    const body = await planned.json();
    if (body.status === "needs_input") {
      await chrome.tabs.sendMessage(tab.id, { type: "jaa-notice", text: body.notice });
      await reportStatus(match.id, "needs_input");
      return { ok: false, needsInput: true, error: body.notice };
    }
    const steps = body.plan?.steps || [];
    for (const step of steps) {
      if (!ALLOWED.has(step.action)) return { ok: false, error: "Refusing a plan with a disallowed action." };
    }
    const cv = steps.some((step) => step.action === "upload_cv") ? await fetchCv(match.id) : null;
    if (steps.some((step) => step.action === "upload_cv") && !cv) {
      return { ok: false, error: "Could not download the CV." };
    }
    const filled = await chrome.tabs.sendMessage(tab.id, {
      type: "jaa-fill",
      steps,
      cv,
      notice: body.notice,
      final: pass === 1 || !scan.hasNext,
    });
    if (!filled?.ok) {
      await reportStatus(match.id, "failed", filled?.error || "Fill failed");
      return { ok: false, error: filled?.error || "Fill failed" };
    }
    highlighted = filled.highlighted || highlighted;
    if (!filled.advanced) break;
  }
  await reportStatus(match.id, "filled_awaiting_review");
  await chrome.action.setBadgeText({ text: "!" });
  await chrome.action.setBadgeBackgroundColor({ color: "#0a7" });
  try {
    chrome.notifications.create({
      type: "basic",
      title: "Application ready to review",
      message: "Everything is filled in. Review the form and click Submit yourself.",
    });
  } catch (_) {
    /* notifications optional */
  }
  return { ok: true, highlighted };
}

chrome.alarms.create("jaa-poll", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== "jaa-poll") return;
  const ready = await fetchReady();
  await chrome.action.setBadgeText({ text: ready.length ? String(ready.length) : "" });
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "jaa-fill-active") {
    fillActiveTab(message.applicationId).then(sendResponse);
    return true;
  }
  return false;
});

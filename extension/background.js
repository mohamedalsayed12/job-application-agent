const DEFAULT_API = "http://127.0.0.1:8000";
const ALLOWED_ACTIONS = new Set(["fill", "select", "check", "upload_cv"]);

async function settings() {
  const stored = await chrome.storage.local.get(["apiBase", "token"]);
  return {
    apiBase: (stored.apiBase || DEFAULT_API).replace(/\/$/, ""),
    token: stored.token || "",
  };
}

async function api(path, options = {}) {
  const { apiBase, token } = await settings();
  const response = await fetch(`${apiBase}${path}`, {
    ...options,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) throw new Error(`Job Agent answered ${response.status}`);
  return response.json();
}

async function openApplications() {
  const { token } = await settings();
  if (!token) return [];
  try {
    return await api("/extension/applications/ready");
  } catch (_) {
    return [];
  }
}

async function reportStatus(id, status, error) {
  await api(`/extension/applications/${id}/status`, {
    method: "POST",
    body: JSON.stringify({ status, error: error || null }),
  });
}

function samePage(a, b) {
  try {
    const left = new URL(a);
    const right = new URL(b);
    return left.origin === right.origin && left.pathname.replace(/\/$/, "") === right.pathname.replace(/\/$/, "");
  } catch {
    return a === b;
  }
}

/** The open application this tab most likely belongs to: same page, else same site, else none. */
function guessApplication(apps, tabUrl) {
  const exact = apps.find((row) => samePage(row.page_url || "", tabUrl) || samePage(row.apply_url, tabUrl));
  if (exact) return exact.id;
  try {
    const host = new URL(tabUrl).hostname;
    const site = apps.find((row) => [row.page_url, row.apply_url].some((u) => u && new URL(u).hostname === host));
    return site ? site.id : null;
  } catch {
    return null;
  }
}

/** Reads the form in every frame of the tab and keeps the frame with the most fields. */
async function scanTab(tabId) {
  const injected = await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: ["content.js"] });
  let best = null;
  for (const { frameId } of injected) {
    try {
      const scan = await chrome.tabs.sendMessage(tabId, { type: "jaa-scan" }, { frameId });
      if (scan?.ok && (!best || scan.fields.length > best.fields.length)) best = { frameId, ...scan };
    } catch (_) {
      /* a frame that cannot answer has no form we can fill */
    }
  }
  return best;
}

/** The CV for this application (the tailored one when ready), as base64 for the page script. */
async function downloadCv(applicationId) {
  const { apiBase, token } = await settings();
  const response = await fetch(`${apiBase}/extension/cv?application_id=${encodeURIComponent(applicationId)}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) return null;
  const bytes = new Uint8Array(await response.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  const docx = (response.headers.get("Content-Disposition") || "").includes(".docx");
  return {
    base64: btoa(binary),
    name: docx ? "cv.docx" : "cv.pdf",
    type: response.headers.get("Content-Type") || (docx ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document" : "application/pdf"),
  };
}

async function fillActiveTab(applicationId) {
  const { token } = await settings();
  if (!token) return { ok: false, error: "Set an extension token first." };
  if (!applicationId) return { ok: false, error: "Choose which job this form is for." };
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !tab.url) return { ok: false, error: "No active tab." };

  const scan = await scanTab(tab.id);
  if (!scan || scan.fields.length === 0) {
    return { ok: false, error: "No form fields on this page. Click through to the employer's application form, then try again." };
  }
  let plan;
  try {
    plan = await api(`/extension/applications/${applicationId}/form`, {
      method: "POST",
      body: JSON.stringify({ page_url: scan.url, fields: scan.fields }),
    });
  } catch (err) {
    return { ok: false, error: String(err.message || err) };
  }
  if ((plan.steps || []).some((step) => !ALLOWED_ACTIONS.has(step.action))) {
    return { ok: false, error: "Refusing a plan with a disallowed action." };
  }
  const cv = (plan.steps || []).some((step) => step.action === "upload_cv") ? await downloadCv(applicationId) : null;
  const result = await chrome.tabs.sendMessage(tab.id, { type: "jaa-fill", plan, cv }, { frameId: scan.frameId });
  if (!result?.ok) {
    // Leave the application open so the person can try again on the same page.
    return { ok: false, error: result?.error || "Fill failed" };
  }
  const missing = plan.missing || [];
  if (missing.length === 0) await reportStatus(applicationId, "filled_awaiting_review");
  await chrome.action.setBadgeText({ text: missing.length ? String(missing.length) : "" });
  try {
    await chrome.notifications.create({
      type: "basic",
      iconUrl: "icon.png",
      title: missing.length ? "Some questions need you" : "Application ready to review",
      message: missing.length
        ? `Filled ${result.filled} fields. ${missing.length} question(s) have no saved answer: answer them on the form.`
        : `Filled ${result.filled} fields. Check the form and press Submit yourself.`,
    });
  } catch (_) {
    /* notifications are optional */
  }
  return { ok: true, filled: result.filled, skipped: result.skipped || [], missing };
}

chrome.alarms.create("jaa-poll", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== "jaa-poll") return;
  const apps = await openApplications();
  await chrome.action.setBadgeText({ text: apps.length ? String(apps.length) : "" });
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "jaa-list") {
    (async () => {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const apps = await openApplications();
      sendResponse({ apps, guess: tab?.url ? guessApplication(apps, tab.url) : null });
    })();
    return true;
  }
  if (message?.type === "jaa-fill-active") {
    fillActiveTab(message.applicationId).then(sendResponse);
    return true;
  }
  return false;
});

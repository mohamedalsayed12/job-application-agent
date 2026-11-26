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
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  return response;
}

async function openApplications() {
  const { token } = await settings();
  if (!token) return [];
  try {
    const response = await api("/extension/applications/ready");
    if (!response.ok) return [];
    return response.json();
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

  let highlighted = "";
  let filledAny = false;
  let waitingForNext = false;
  let lastMissing = [];
  let lastFilled = 0;
  let lastSkipped = [];

  // Two passes so one Next click can be followed; upgrade when a form has more than two steps.
  for (let pass = 0; pass < 2; pass += 1) {
    const scan = await scanTab(tab.id);
    if (!scan || !scan.fields?.length) {
      if (pass === 0) {
        return { ok: false, error: "No form fields on this page. Click through to the employer's application form, then try again." };
      }
      waitingForNext = true;
      break;
    }
    let body;
    try {
      const response = await api(`/extension/applications/${applicationId}/form`, {
        method: "POST",
        body: JSON.stringify({ page_url: scan.url, fields: scan.fields }),
      });
      if (!response.ok) return { ok: false, error: `Job Agent answered ${response.status}` };
      body = await response.json();
    } catch (err) {
      return { ok: false, error: String(err.message || err) };
    }
    if (body.status === "needs_input") {
      await chrome.tabs.sendMessage(tab.id, { type: "jaa-notice", text: body.notice }, { frameId: scan.frameId });
      return { ok: false, needsInput: true, error: body.notice, missing: (body.missing || []).map((m) => m.label || m) };
    }
    const steps = body.plan?.steps || body.steps || [];
    if (steps.some((step) => !ALLOWED_ACTIONS.has(step.action))) {
      return { ok: false, error: "Refusing a plan with a disallowed action." };
    }
    const cv = steps.some((step) => step.action === "upload_cv") ? await downloadCv(applicationId) : null;
    if (steps.some((step) => step.action === "upload_cv") && !cv) {
      return { ok: false, error: "Could not download the CV." };
    }
    const filled = await chrome.tabs.sendMessage(
      tab.id,
      {
        type: "jaa-fill",
        steps,
        plan: body.plan || body,
        cv,
        notice: body.notice,
        final: pass === 1 || !scan.hasNext,
      },
      { frameId: scan.frameId },
    );
    if (!filled?.ok) {
      await reportStatus(applicationId, "failed", filled?.error || "Fill failed");
      return { ok: false, error: filled?.error || "Fill failed" };
    }
    highlighted = filled.highlighted || highlighted;
    lastFilled = filled.filled || lastFilled;
    lastSkipped = filled.skipped || lastSkipped;
    lastMissing = body.missing || [];
    filledAny = true;
    waitingForNext = Boolean(filled.advanced);
    if (!filled.advanced) break;
  }

  if (!filledAny) return { ok: false, error: "No form fields on this page." };
  if (waitingForNext) return { ok: false, error: "The next step is not filled yet. Click Fill again." };
  if (!lastMissing.length) await reportStatus(applicationId, "filled_awaiting_review");
  await chrome.action.setBadgeText({ text: lastMissing.length ? String(lastMissing.length) : "!" });
  await chrome.action.setBadgeBackgroundColor({ color: "#0a7" });
  try {
    await chrome.notifications.create({
      type: "basic",
      iconUrl: "icon.png",
      title: lastMissing.length ? "Some questions need you" : "Application ready to review",
      message: lastMissing.length
        ? `Filled ${lastFilled} fields. ${lastMissing.length} question(s) have no saved answer.`
        : `Filled ${lastFilled} fields. Check the form and press Submit yourself.`,
    });
  } catch (_) {
    /* notifications are optional */
  }
  return {
    ok: true,
    filled: lastFilled,
    skipped: lastSkipped,
    missing: lastMissing.map((m) => m.label || m),
    highlighted,
  };
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

const DEFAULT_API = "http://127.0.0.1:8000";
const ALLOWED_ACTIONS = new Set(["fill", "select", "check", "upload_cv"]);
// A tab opened for an application stays armed this long: every form page it reaches is filled.
const ARMED_FOR_MS = 60 * 60 * 1000;
// Fewer fields than this is a search box or a login, not an application form.
const MIN_FORM_FIELDS = 3;
// Open applications are asked for at most this often while the person browses.
const APPS_CACHE_MS = 60 * 1000;

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

/** Open applications, remembered for a minute so ordinary browsing does not call Job Agent on every page. */
async function cachedApplications() {
  const { apps } = await chrome.storage.session.get("apps");
  if (apps && apps.at > Date.now() - APPS_CACHE_MS) return apps.rows;
  const rows = await openApplications();
  await chrome.storage.session.set({ apps: { at: Date.now(), rows } });
  return rows;
}

/** An application form asks for a CV or contact details; a job board's search box does not. */
function looksLikeApplication(fields) {
  return fields.length >= MIN_FORM_FIELDS && fields.some(
    (field) => field.type === "file" || /e-?mail|first name|last name|full name|surname|phone|resume|\bcv\b/i.test(field.label),
  );
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

// Armed tabs live in session storage: the service worker can be stopped between page loads.
async function armedTabs() {
  return (await chrome.storage.session.get("armed")).armed || {};
}

async function arm(tabId, applicationId) {
  const armed = await armedTabs();
  armed[tabId] = { applicationId, until: Date.now() + ARMED_FOR_MS };
  await chrome.storage.session.set({ armed });
}

async function armedFor(tabId) {
  const armed = await armedTabs();
  const entry = armed[tabId];
  if (!entry) return null;
  if (entry.until < Date.now()) {
    delete armed[tabId];
    await chrome.storage.session.set({ armed });
    return null;
  }
  return entry.applicationId;
}

async function disarm(tabId) {
  const armed = await armedTabs();
  delete armed[tabId];
  await chrome.storage.session.set({ armed });
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
  const disposition = response.headers.get("Content-Disposition") || "";
  const docx = disposition.includes(".docx");
  // The server names the file after the person (Sam-Taylor-CV.pdf); employers see this name.
  const named = disposition.match(/filename="([^"]+)"/);
  return {
    base64: btoa(binary),
    name: named ? named[1] : docx ? "CV.docx" : "CV.pdf",
    type: response.headers.get("Content-Type") || (docx ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document" : "application/pdf"),
  };
}

async function notify(title, message) {
  try {
    await chrome.notifications.create({ type: "basic", iconUrl: "icon.png", title, message });
  } catch (_) {
    /* notifications are optional */
  }
}

// One fill at a time per tab: a page that changes while it is being filled is read again afterwards.
const busy = new Set();

/**
 * Reads the form in the tab, asks Job Agent for the plan (saved answers, earlier answers, checked AI
 * drafts) and fills it. auto: started by the extension itself, so only empty fields are filled and a
 * page that is not a form is passed over quietly.
 */
async function fillTab(tabId, applicationId, { auto = false } = {}) {
  const { token } = await settings();
  if (!token) return { ok: false, error: "Set an extension token first." };
  if (!applicationId) return { ok: false, error: "Choose which job this form is for." };
  if (busy.has(tabId)) return { ok: false, error: "Already filling this tab." };
  busy.add(tabId);
  try {
    const scan = await scanTab(tabId);
    if (!scan || (auto ? !looksLikeApplication(scan.fields) : scan.fields.length === 0)) {
      return { ok: false, quiet: auto, error: "No form fields on this page. Click through to the employer's application form, then try again." };
    }
    await chrome.action.setBadgeText({ text: "…", tabId });
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
    const result = await chrome.tabs.sendMessage(tabId, { type: "jaa-fill", plan, cv, onlyEmpty: auto }, { frameId: scan.frameId });
    if (!result?.ok) {
      // Leave the application open so the person can try again on the same page.
      return { ok: false, error: result?.error || "Fill failed" };
    }
    const missing = (plan.missing || []).map((m) => (typeof m === "string" ? m : m.label || m));
    if (missing.length === 0) await reportStatus(applicationId, "filled_awaiting_review");
    await chrome.action.setBadgeText({ text: missing.length ? String(missing.length) : "✓", tabId });
    if (result.filled > 0 || missing.length) {
      const drafted = result.drafted ? ` ${result.drafted} answer(s) were drafted for you and are outlined in orange: read them.` : "";
      await notify(
        missing.length ? "Some questions need you" : "Form filled: ready for your review",
        missing.length
          ? `Filled ${result.filled} field(s).${drafted} ${missing.length} question(s) have no answer yet: answer them on the form or in Job Agent.`
          : `Filled ${result.filled} field(s).${drafted} Check the form, go on to the next page if there is one, and press Submit yourself.`,
      );
    }
    return { ok: true, filled: result.filled, drafted: result.drafted || 0, skipped: result.skipped || [], missing };
  } finally {
    busy.delete(tabId);
  }
}

async function fillActiveTab(applicationId) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !tab.url) return { ok: false, error: "No active tab." };
  if (applicationId) await arm(tab.id, applicationId);
  return fillTab(tab.id, applicationId);
}

// "Open & fill" sends the tab to the job's apply page. Arriving there arms the tab, and every form page
// it then reaches (the employer's site, the next step of the form) is filled without a click.
chrome.tabs.onUpdated.addListener(async (tabId, change, tab) => {
  if (change.status !== "complete" || !tab.url?.startsWith("http")) return;
  let applicationId = await armedFor(tabId);
  if (!applicationId) {
    const apps = await cachedApplications();
    const match = apps.find((row) => samePage(row.apply_url, tab.url) || samePage(row.page_url || "", tab.url));
    if (!match) return;
    applicationId = match.id;
    await arm(tabId, applicationId);
  }
  await fillTab(tabId, applicationId, { auto: true });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  disarm(tabId);
});

chrome.alarms.create("jaa-poll", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== "jaa-poll") return;
  const apps = await openApplications();
  await chrome.action.setBadgeText({ text: apps.length ? String(apps.length) : "" });
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "jaa-list") {
    (async () => {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const apps = await openApplications();
      sendResponse({ apps, guess: (tab?.id && (await armedFor(tab.id))) || (tab?.url ? guessApplication(apps, tab.url) : null) });
    })();
    return true;
  }
  if (message?.type === "jaa-fill-active") {
    fillActiveTab(message.applicationId).then(sendResponse);
    return true;
  }
  if (message?.type === "jaa-page-changed" && sender.tab?.id) {
    // New questions appeared in an armed tab (a multi-step form moved on): fill the empty ones.
    (async () => {
      const applicationId = await armedFor(sender.tab.id);
      if (applicationId) await fillTab(sender.tab.id, applicationId, { auto: true });
    })();
    return false;
  }
  return false;
});

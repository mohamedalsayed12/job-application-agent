/* Fills the open apply page from a fill plan. Never clicks Submit. */
const ALLOWED_ACTIONS = new Set(["fill", "select", "check", "upload_cv"]);

function isSubmitControl(el) {
  if (!el) return true;
  const tag = el.tagName || "";
  const typeAttr = el.getAttribute?.("type") || "";
  const text = (el.innerText || el.value || "").trim();
  const name = el.getAttribute?.("name") || "";
  const idAttr = el.id || "";
  if (String(typeAttr).toLowerCase() === "submit") return true;
  const hay = `${text} ${name} ${idAttr}`;
  return /\b(submit|apply now|send application|complete application|finish application)\b/i.test(hay)
    && /^(BUTTON|INPUT|A)$/i.test(tag);
}

function findField(step) {
  const name = step.name;
  if (name) {
    const byName = document.querySelector(`[name="${CSS.escape(name)}"]`);
    if (byName) return byName;
    const byId = document.getElementById(name);
    if (byId) return byId;
  }
  const label = (step.label || "").toLowerCase();
  if (!label) return null;
  for (const lab of document.querySelectorAll("label")) {
    if ((lab.textContent || "").toLowerCase().includes(label)) {
      const forId = lab.getAttribute("for");
      if (forId) {
        const el = document.getElementById(forId);
        if (el) return el;
      }
      const nested = lab.querySelector("input, textarea, select");
      if (nested) return nested;
    }
  }
  return null;
}

function setValue(el, value) {
  if (isSubmitControl(el)) return;
  const tag = el.tagName.toLowerCase();
  if (tag === "select") {
    const want = value.toLowerCase();
    const match = [...el.options].find((o) => o.text.trim().toLowerCase() === want);
    if (match) {
      el.value = match.value;
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return;
  }
  if (el.type === "checkbox" || el.type === "radio") {
    const want = /^(yes|true|1)$/i.test(value);
    if (el.type === "radio") {
      const group = document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`);
      for (const radio of group) {
        if (isSubmitControl(radio)) continue;
        const label = (radio.labels?.[0]?.textContent || radio.value || "").toLowerCase();
        const wantText = value.toLowerCase();
        if (label === wantText || label.startsWith(wantText + " ") || label.endsWith(" " + wantText)) {
          radio.checked = true;
          radio.dispatchEvent(new Event("change", { bubbles: true }));
          return;
        }
        if (want && /^(yes|true)$/i.test(label.trim())) {
          radio.checked = true;
          radio.dispatchEvent(new Event("change", { bubbles: true }));
          return;
        }
      }
    } else {
      el.checked = want;
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return;
  }
  el.focus();
  el.value = value;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function uploadCv(el, cv) {
  if (isSubmitControl(el) || el.type !== "file") return;
  if (!cv) throw new Error("CV missing from fill plan");
  const binary = atob(cv.base64);
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  const file = new File([bytes], cv.name, { type: cv.type });
  const transfer = new DataTransfer();
  transfer.items.add(file);
  el.files = transfer.files;
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

async function applyPlan(plan, cv) {
  for (const step of plan.steps || []) {
    if (!ALLOWED_ACTIONS.has(step.action)) continue;
    const el = findField(step);
    if (!el || isSubmitControl(el)) continue;
    if (step.action === "upload_cv") {
      uploadCv(el, cv);
    } else {
      setValue(el, String(step.value || ""));
    }
  }
  for (const el of document.querySelectorAll('button, input[type=submit], a')) {
    if (isSubmitControl(el)) {
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      break;
    }
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "jaa-ping") {
    sendResponse({ ok: true });
    return false;
  }
  if (message?.type !== "jaa-fill") return;
  (async () => {
    try {
      await applyPlan(message.plan, message.cv);
      sendResponse({ ok: true });
    } catch (err) {
      sendResponse({ ok: false, error: String(err) });
    }
  })();
  return true;
});

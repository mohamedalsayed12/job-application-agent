/* Fills the open apply page from a fill plan. Never clicks Submit. */
const ALLOWED_ACTIONS = new Set(["fill", "select", "check", "upload_cv"]);
const SUBMIT_TEXT = /\b(submit|apply now|send application|complete application|finish application)\b/i;
const fields = new Map();

function ownText(el) {
  return [...el.childNodes]
    .filter((node) => node.nodeType === Node.TEXT_NODE)
    .map((node) => node.textContent)
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

function isSubmitControl(el) {
  if (!el || el.nodeType !== 1 || el.id === "jaa-fill-notice") return false;
  const type = (el.getAttribute("type") || "").toLowerCase();
  if (type === "submit") return true;
  const tag = el.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return false;
  if (!/^(BUTTON|A|DIV|SPAN)$/.test(tag)) return false;
  return SUBMIT_TEXT.test(ownText(el));
}

function labelFor(el) {
  const labelled = el.labels && el.labels[0] && el.labels[0].innerText;
  if (labelled && labelled.trim()) return labelled.trim();
  const aria = el.getAttribute("aria-label");
  if (aria) return aria.trim();
  const byId = el.getAttribute("aria-labelledby");
  if (byId) {
    const node = document.getElementById(byId);
    if (node && node.innerText.trim()) return node.innerText.trim();
  }
  return (el.getAttribute("placeholder") || "").trim();
}

function fieldType(el) {
  const tag = el.tagName;
  if (tag === "TEXTAREA") return "textarea";
  if (tag === "SELECT") return "select";
  const type = (el.getAttribute("type") || "text").toLowerCase();
  if (["hidden", "submit", "button", "reset", "image", "password"].includes(type)) return null;
  if (type === "radio") return "radio";
  if (type === "checkbox") return "checkbox";
  if (type === "file") return "file";
  if (["email", "tel", "url", "number", "date"].includes(type)) return type;
  return "text";
}

function optionsFor(el) {
  if (el.tagName === "SELECT") {
    return [...el.options].map((option) => option.text.trim()).filter(Boolean);
  }
  if ((el.getAttribute("type") || "").toLowerCase() !== "radio" || !el.name) return [];
  return [...document.querySelectorAll(`input[type="radio"][name="${CSS.escape(el.name)}"]`)]
    .map((radio) => labelFor(radio) || radio.value)
    .filter(Boolean);
}

function findNext() {
  for (const el of document.querySelectorAll("button, div, span")) {
    if (isSubmitControl(el)) continue;
    if (el.tagName === "BUTTON" && (el.getAttribute("type") || "submit").toLowerCase() !== "button") continue;
    const text = (el.innerText || "").replace(/\s+/g, " ").trim();
    if (/^(next|continue)$/i.test(text)) return el;
  }
  return null;
}

function scan() {
  fields.clear();
  const found = [];
  const seenRadio = new Set();
  let index = 0;
  for (const el of document.querySelectorAll("input, textarea, select")) {
    if (isSubmitControl(el)) continue;
    const type = fieldType(el);
    if (!type) continue;
    if (type === "radio") {
      if (!el.name || seenRadio.has(el.name)) continue;
      seenRadio.add(el.name);
    }
    const id = `f${index}`;
    index += 1;
    fields.set(id, el);
    found.push({
      id,
      label: labelFor(el),
      name: el.name || "",
      type,
      required: Boolean(el.required),
      options: optionsFor(el),
    });
  }
  return { fields: found, hasNext: Boolean(findNext()) };
}

function setText(el, value) {
  const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value");
  if (setter && setter.set) setter.set.call(el, value);
  else el.value = value;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function selectValue(el, value) {
  const want = String(value || "").trim().toLowerCase();
  if (el.tagName === "SELECT") {
    const match = [...el.options].find((option) => option.text.trim().toLowerCase() === want);
    if (!match) return;
    el.value = match.value;
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return;
  }
  if ((el.getAttribute("type") || "").toLowerCase() !== "radio" || !el.name) return;
  for (const radio of document.querySelectorAll(`input[type="radio"][name="${CSS.escape(el.name)}"]`)) {
    const label = (labelFor(radio) || radio.value || "").trim().toLowerCase();
    if (label === want) {
      radio.checked = true;
      radio.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
  }
}

function checkValue(el, value) {
  const yes = /^(yes|true|1)$/i.test(String(value || "").trim());
  el.checked = yes;
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function uploadCv(el, cv) {
  if (!cv || el.type !== "file" || isSubmitControl(el)) return;
  const binary = atob(cv.base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  const file = new File([bytes], cv.name, { type: cv.type || "application/pdf" });
  const transfer = new DataTransfer();
  transfer.items.add(file);
  el.files = transfer.files;
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function applySteps(steps, cv) {
  for (const step of steps || []) {
    if (!ALLOWED_ACTIONS.has(step.action)) continue;
    const el = fields.get(step.field_id);
    if (!el || isSubmitControl(el)) continue;
    if (step.action === "upload_cv") uploadCv(el, cv);
    else if (step.action === "select") selectValue(el, step.value);
    else if (step.action === "check") checkValue(el, step.value);
    else setText(el, String(step.value || ""));
  }
}

function showNotice(text) {
  let el = document.getElementById("jaa-fill-notice");
  if (!el) {
    el = document.createElement("div");
    el.id = "jaa-fill-notice";
    el.setAttribute("role", "status");
    el.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#0a7;color:#fff;padding:12px 16px;font:16px system-ui";
    (document.body || document.documentElement).appendChild(el);
  }
  el.textContent = text;
}

function highlightSubmit() {
  for (const el of document.querySelectorAll("button, input, a, div, span")) {
    if (!isSubmitControl(el)) continue;
    el.scrollIntoView({ block: "center" });
    el.style.setProperty("outline", "3px solid #0a7");
    el.style.setProperty("outline-offset", "3px");
    el.setAttribute("data-jaa-highlight", "submit");
    return el.id || el.tagName;
  }
  return "";
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "jaa-ping") {
    sendResponse({ ok: true });
    return false;
  }
  if (message?.type === "jaa-scan") {
    sendResponse(scan());
    return false;
  }
  if (message?.type === "jaa-notice") {
    showNotice(message.text || "");
    sendResponse({ ok: true });
    return false;
  }
  if (message?.type !== "jaa-fill") return;
  try {
    applySteps(message.steps, message.cv);
    const next = !message.final && findNext();
    if (next) {
      next.click();
      sendResponse({ ok: true, advanced: true });
      return;
    }
    const highlighted = highlightSubmit();
    showNotice(message.notice || "Everything is filled in. Review the form and click Submit yourself.");
    sendResponse({ ok: true, advanced: false, highlighted });
  } catch (err) {
    sendResponse({ ok: false, error: String(err) });
  }
  return false;
});

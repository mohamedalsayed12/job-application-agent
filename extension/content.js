/* Reads the open apply form and fills it from a fill plan. Never clicks Submit or Next (D70/D75).

   Custom dropdowns (Workday, Greenhouse, Lever, Ashby) are opened and the matching option is clicked.
   Answers the AI drafted are outlined so the person reads them first. After a fill, new questions that
   appear (the next page of a multi-step form) are reported to the background worker, which fills them
   too, without touching anything already answered. */
(() => {
  if (window.__jaaContentLoaded) return;
  window.__jaaContentLoaded = true;

  const ALLOWED_ACTIONS = new Set(["fill", "select", "check", "upload_cv"]);
  const SKIP_TYPES = new Set(["hidden", "submit", "button", "reset", "image", "password"]);
  const SUBMIT_TEXT = /\b(submit|apply now|send application|complete application|finish application)\b/i;
  const MAX_TEXT = 300;
  const DRAFT_OUTLINE = "2px solid #e8a317";
  const PLACEHOLDER = /^(select|choose|please select|please choose|--|select one|select\.\.\.)?$/i;
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
    if (type === "submit" || type === "image") return true;
    if (el.tagName === "BUTTON") {
      if (type !== "button" && type !== "reset") return true;
      return SUBMIT_TEXT.test(ownText(el));
    }
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") return false;
    if (!/^(A|DIV|SPAN)$/.test(el.tagName)) return false;
    return SUBMIT_TEXT.test(ownText(el) || el.innerText || "");
  }

  function clean(text) {
    return String(text || "").replace(/\s+/g, " ").replace(/\s*\*\s*$/, "").trim().slice(0, MAX_TEXT);
  }

  function textOf(ids) {
    return ids
      .split(/\s+/)
      .map((id) => document.getElementById(id)?.innerText || "")
      .join(" ");
  }

  function questionFor(el) {
    if (el.type === "radio" || (el.type === "checkbox" && el.name && document.querySelectorAll(`input[type=checkbox][name="${CSS.escape(el.name)}"]`).length > 1)) {
      const group = el.closest("fieldset, [role=radiogroup], [role=group]");
      const legend = group?.querySelector("legend")?.innerText
        || (group?.getAttribute("aria-labelledby") && textOf(group.getAttribute("aria-labelledby")))
        || group?.getAttribute("aria-label");
      if (legend) return clean(legend);
    }
    const labelled = el.getAttribute("aria-labelledby");
    const candidates = [
      el.labels?.[0]?.innerText,
      labelled && textOf(labelled),
      el.getAttribute("aria-label"),
      el.closest("label")?.innerText,
      el.getAttribute("placeholder"),
      el.closest("div, li, p")?.querySelector("label, legend, .label, [class*=label]")?.innerText,
      el.getAttribute("title"),
    ];
    for (const candidate of candidates) {
      const text = clean(candidate);
      if (text) return text;
    }
    return "";
  }

  function optionLabel(el) {
    return clean(el.labels?.[0]?.innerText || el.getAttribute("aria-label") || el.value);
  }

  function isRequired(el, label) {
    return Boolean(el.required || el.getAttribute("aria-required") === "true" || /\*\s*$/.test(el.labels?.[0]?.innerText || "") || /\*/.test(label));
  }

  function visible(el) {
    if (el.type === "file") return true;
    const style = window.getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden" && (el.offsetParent !== null || style.position === "fixed");
  }

  function isCombo(el) {
    return el.getAttribute?.("role") === "combobox" || el.getAttribute?.("aria-haspopup") === "listbox";
  }

  function fieldType(el) {
    const tag = el.tagName;
    if (tag === "TEXTAREA") return "textarea";
    if (tag === "SELECT") return "select";
    if (isCombo(el)) return "combobox";
    const type = (el.getAttribute("type") || "text").toLowerCase();
    if (SKIP_TYPES.has(type)) return null;
    if (type === "radio") return "radio";
    if (type === "checkbox") return "checkbox";
    if (type === "file") return "file";
    if (["email", "tel", "url", "number", "date", "month"].includes(type)) return type;
    return "text";
  }

  function optionsFor(el, type) {
    if (type === "select") {
      return [...el.options].map((option) => clean(option.text)).filter((t) => t && !/^(select|choose|please select|--)/i.test(t));
    }
    if (type === "radio" && el.name) {
      return [...document.querySelectorAll(`input[type="radio"][name="${CSS.escape(el.name)}"]`)]
        .map(optionLabel)
        .filter(Boolean);
    }
    return [];
  }

  /** Every field a person would answer, with a stable id for the fill plan (D70 scan + D66 ids). */
  function scanForm() {
    fields.clear();
    const found = [];
    const seenRadio = new Set();
    let index = 0;
    for (const el of document.querySelectorAll("input, textarea, select")) {
      if (isSubmitControl(el) || el.disabled || !visible(el)) continue;
      const type = fieldType(el);
      if (!type) continue;
      if (type === "radio") {
        if (!el.name || seenRadio.has(el.name)) continue;
        seenRadio.add(el.name);
      }
      const id = `f${index}`;
      index += 1;
      fields.set(id, el);
      const label = questionFor(el);
      found.push({
        id,
        label,
        name: el.name || "",
        type,
        required: type === "radio"
          ? [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)].some((r) => isRequired(r, label))
          : isRequired(el, label),
        options: optionsFor(el, type),
      });
    }
    for (const el of document.querySelectorAll("[role=combobox], [aria-haspopup=listbox]")) {
      if (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) || el.disabled || isSubmitControl(el) || !visible(el)) continue;
      const label = questionFor(el);
      if (!label) continue;
      const id = `f${index}`;
      index += 1;
      fields.set(id, el);
      found.push({ id, label, name: el.id || el.getAttribute("name") || id, type: "combobox", required: isRequired(el, label), options: [] });
    }
    return { ok: true, url: location.href, fields: found };
  }

  function hasAnswer(el) {
    if (el.tagName === "SELECT") return el.selectedIndex > 0 || Boolean(el.value && !PLACEHOLDER.test(clean(el.options[el.selectedIndex]?.text)));
    if (el.type === "radio") return [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)].some((r) => r.checked);
    if (el.type === "checkbox") return el.checked;
    if (el.type === "file") return el.files?.length > 0;
    if (/^(INPUT|TEXTAREA)$/.test(el.tagName)) return Boolean(el.value && el.value.trim());
    return !PLACEHOLDER.test(clean(el.innerText));
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const norm = (text) => clean(text).toLowerCase();

  function listOptions(el) {
    const id = el.getAttribute("aria-controls") || el.getAttribute("aria-owns");
    const scope = (id && document.getElementById(id)) || document;
    return [...scope.querySelectorAll("[role=option], [data-automation-id=promptOption]")].filter(
      (option) => option.offsetParent !== null && !isSubmitControl(option),
    );
  }

  function setNative(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
  }

  async function pickFromList(el, value) {
    const want = norm(value);
    if (el.tagName === "INPUT") {
      el.focus();
      setNative(el, value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      el.click();
    }
    for (let attempt = 0; attempt < 15; attempt += 1) {
      await sleep(100);
      const options = listOptions(el);
      const hit = options.find((o) => norm(o.innerText) === want) || options.find((o) => norm(o.innerText).startsWith(want));
      if (hit) {
        hit.click();
        return true;
      }
    }
    if (el.tagName === "INPUT") {
      setNative(el, "");
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    return false;
  }

  function selectValue(el, value) {
    const want = clean(value).toLowerCase();
    if (el.tagName === "SELECT") {
      const match = [...el.options].find((option) => clean(option.text).toLowerCase() === want);
      if (!match) return false;
      el.value = match.value;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    if ((el.getAttribute("type") || "").toLowerCase() !== "radio" || !el.name) return false;
    for (const radio of document.querySelectorAll(`input[type="radio"][name="${CSS.escape(el.name)}"]`)) {
      if (isSubmitControl(radio)) continue;
      const label = (optionLabel(radio) || radio.value || "").toLowerCase();
      if (label === want) {
        radio.click();
        return radio.checked;
      }
    }
    return false;
  }

  function checkValue(el, value) {
    const on = /^(yes|true|1)$/i.test(String(value || "").trim());
    if (el.checked !== on) el.click();
    return true;
  }

  function setText(el, value) {
    el.focus();
    setNative(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.blur();
    return true;
  }

  function uploadCv(el, cv) {
    if (!cv || el.type !== "file" || isSubmitControl(el)) return false;
    const binary = atob(cv.base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    const file = new File([bytes], cv.name, { type: cv.type || "application/pdf" });
    const transfer = new DataTransfer();
    transfer.items.add(file);
    el.files = transfer.files;
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  function markDraft(el, note) {
    const target = el.type === "radio" ? el.closest("fieldset, [role=radiogroup]") || el : el;
    target.style.outline = DRAFT_OUTLINE;
    target.style.outlineOffset = "2px";
    target.title = note || "Drafted by Job Agent. Read it before you submit.";
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

  async function applySteps(steps, cv, onlyEmpty) {
    let filled = 0;
    let drafted = 0;
    const skipped = [];
    for (const step of steps || []) {
      if (!ALLOWED_ACTIONS.has(step.action)) continue;
      const el = fields.get(step.field_id);
      if (!el || isSubmitControl(el)) {
        skipped.push(step.field_id || step.label || "");
        continue;
      }
      if (onlyEmpty && hasAnswer(el)) continue;
      let done = false;
      try {
        if (step.action === "upload_cv") done = uploadCv(el, cv);
        else if (isCombo(el)) done = await pickFromList(el, step.value);
        else if (step.action === "select") done = selectValue(el, step.value);
        else if (step.action === "check") done = checkValue(el, step.value);
        else done = setText(el, String(step.value || ""));
      } catch (_) {
        done = false;
      }
      if (done) {
        filled += 1;
        if (step.source === "ai") {
          drafted += 1;
          markDraft(el, step.note);
        }
      } else skipped.push(step.field_id || step.label || "");
    }
    return { filled, drafted, skipped };
  }

  function fieldKeys() {
    return new Set(scanForm().fields.map((field) => `${field.name}|${field.label}`));
  }

  let known = null;
  let observer = null;
  let timer = null;
  function watchForNewQuestions() {
    known = fieldKeys();
    if (observer) return;
    observer = new MutationObserver(() => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const now = fieldKeys();
        const added = [...now].filter((key) => !known.has(key));
        known = now;
        if (added.length) chrome.runtime.sendMessage({ type: "jaa-page-changed", added: added.length }).catch(() => {});
      }, 1500);
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "jaa-ping") {
      sendResponse({ ok: true });
      return false;
    }
    if (message?.type === "jaa-scan") {
      sendResponse(scanForm());
      return false;
    }
    if (message?.type === "jaa-notice") {
      showNotice(message.text || "");
      sendResponse({ ok: true });
      return false;
    }
    if (message?.type !== "jaa-fill") return false;
    applySteps(message.steps || message.plan?.steps, message.cv, Boolean(message.onlyEmpty))
      .then((result) => {
        const highlighted = highlightSubmit();
        showNotice(message.notice || "Everything is filled in. Review the form and click Submit yourself.");
        watchForNewQuestions();
        sendResponse({ ok: true, advanced: false, highlighted, ...result });
      })
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  });
})();

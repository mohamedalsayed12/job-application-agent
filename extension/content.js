/* Reads the open apply form and fills it from a fill plan. Never clicks Submit.

   Custom dropdowns (Workday, Greenhouse, Lever, Ashby) are opened and the matching option is clicked.
   Answers the AI drafted are outlined so the person reads them first. After a fill, new questions that
   appear (the next page of a multi-step form) are reported to the background worker, which fills them
   too, without touching anything already answered. */
(() => {
  if (window.__jaaContentLoaded) return;
  window.__jaaContentLoaded = true;

  const ALLOWED_ACTIONS = new Set(["fill", "select", "check", "upload_cv"]);
  const SKIP_TYPES = new Set(["hidden", "submit", "button", "reset", "image", "password"]);
  const MAX_TEXT = 300;
  const DRAFT_OUTLINE = "2px solid #e8a317";
  const PLACEHOLDER = /^(select|choose|please select|please choose|--|select one|select\.\.\.)?$/i;

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
    // A radio or checkbox group's question is its fieldset legend or group label, not one option's label.
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
    if (el.type === "file") return true; // Upload inputs are often hidden behind a styled button.
    const style = window.getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden" && (el.offsetParent !== null || style.position === "fixed");
  }

  function isCombo(el) {
    return el.getAttribute?.("role") === "combobox" || el.getAttribute?.("aria-haspopup") === "listbox";
  }

  let counter = 0;
  function keyFor(el) {
    if (el.dataset.jaaKey) return el.dataset.jaaKey;
    const key = el.type === "radio" && el.name ? el.name : el.name || el.id || `jaa-${++counter}`;
    el.dataset.jaaKey = key;
    return key;
  }

  /** Every field a person would answer, as label, key, type, required flag and options. */
  function scanForm() {
    const fields = [];
    const seenGroups = new Set();
    for (const el of document.querySelectorAll("input, textarea, select")) {
      const type = (el.tagName === "INPUT" ? el.type || "text" : el.tagName).toLowerCase();
      if (SKIP_TYPES.has(type) || el.disabled || isSubmitControl(el) || !visible(el)) continue;
      if (type === "radio") {
        if (!el.name || seenGroups.has(el.name)) continue;
        seenGroups.add(el.name);
        const group = [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)];
        group.forEach((radio) => { radio.dataset.jaaKey = el.name; });
        const label = questionFor(el);
        fields.push({ label, name: el.name, type: "radio", required: group.some((r) => isRequired(r, label)), options: group.map(optionLabel).filter(Boolean) });
        continue;
      }
      const label = questionFor(el);
      const options = type.startsWith("select")
        ? [...el.options].map((o) => clean(o.text)).filter((t) => t && !/^(select|choose|please select|--)/i.test(t))
        : [];
      // A typing box that opens a list (react-select, Ashby) is filled like a dropdown.
      fields.push({ label, name: keyFor(el), type: isCombo(el) ? "combobox" : type, required: isRequired(el, label), options });
    }
    // Dropdowns built from buttons or divs: their options exist only once opened.
    for (const el of document.querySelectorAll("[role=combobox], [aria-haspopup=listbox]")) {
      if (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) || el.disabled || isSubmitControl(el) || !visible(el)) continue;
      const label = questionFor(el);
      if (!label) continue;
      fields.push({ label, name: keyFor(el), type: "combobox", required: isRequired(el, label), options: [] });
    }
    return fields;
  }

  /** Whether the field holds an answer now. A refill leaves anything already answered alone. */
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

  /** Opens a custom dropdown (or types into a searchable one) and clicks the option that matches. */
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
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    if (el.tagName === "INPUT") {
      setNative(el, "");
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    return false;
  }

  function findField(step) {
    const name = step.name;
    if (name) {
      const tagged = document.querySelector(`[data-jaa-key="${CSS.escape(name)}"]`);
      if (tagged) return tagged;
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

  function setNative(el, value) {
    // React and similar frameworks track the value themselves; the prototype setter keeps them in step.
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
  }

  async function setValue(el, value) {
    if (isSubmitControl(el)) return false;
    if (isCombo(el)) return pickFromList(el, value);
    const tag = el.tagName.toLowerCase();
    const want = clean(value).toLowerCase();
    if (tag === "select") {
      const match = [...el.options].find((o) => clean(o.text).toLowerCase() === want);
      if (!match) return false;
      el.value = match.value;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    if (el.type === "radio") {
      const group = document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`);
      for (const radio of group) {
        if (isSubmitControl(radio) || optionLabel(radio).toLowerCase() !== want) continue;
        radio.click();
        return radio.checked;
      }
      return false;
    }
    if (el.type === "checkbox") {
      const on = /^(yes|true|1)$/i.test(value);
      if (el.checked !== on) el.click();
      return true;
    }
    el.focus();
    setNative(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.blur();
    return true;
  }

  function attachCv(el, cv) {
    // The background worker downloads the CV: a page script may not call Job Agent (cross-site).
    if (isSubmitControl(el) || el.type !== "file" || !cv?.base64) return false;
    const bytes = Uint8Array.from(atob(cv.base64), (c) => c.charCodeAt(0));
    const file = new File([bytes], cv.name, { type: cv.type });
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

  /** Fills the plan. With onlyEmpty (an automatic refill), fields that already hold an answer are left alone. */
  async function applyPlan(plan, cv, onlyEmpty) {
    let filled = 0;
    let drafted = 0;
    const skipped = [];
    for (const step of plan.steps || []) {
      if (!ALLOWED_ACTIONS.has(step.action)) continue;
      const el = findField(step);
      if (!el || isSubmitControl(el)) {
        skipped.push(step.label);
        continue;
      }
      if (onlyEmpty && hasAnswer(el)) continue;
      let done = false;
      try {
        done = step.action === "upload_cv" ? attachCv(el, cv) : await setValue(el, String(step.value || ""));
      } catch (_) {
        done = false; // One awkward field must not stop the rest of the form.
      }
      if (done) {
        filled += 1;
        if (step.source === "ai") {
          drafted += 1;
          markDraft(el, step.note);
        }
      } else skipped.push(step.label);
    }
    for (const el of document.querySelectorAll("button, input[type=submit], a")) {
      if (isSubmitControl(el)) {
        el.scrollIntoView({ block: "center", behavior: "smooth" });
        break;
      }
    }
    watchForNewQuestions();
    return { filled, drafted, skipped };
  }

  function fieldKeys() {
    return new Set(scanForm().map((field) => `${field.name}|${field.label}`));
  }

  /** After a fill, questions that appear later (the next step of the form) are reported once they settle. */
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
      sendResponse({ ok: true, url: location.href, fields: scanForm() });
      return false;
    }
    if (message?.type !== "jaa-fill") return false;
    applyPlan(message.plan, message.cv, Boolean(message.onlyEmpty))
      .then((result) => sendResponse({ ok: true, ...result }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  });
})();

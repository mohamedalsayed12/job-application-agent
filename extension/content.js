/* Reads the open apply form and fills it from a fill plan. Never clicks Submit. */
(() => {
  if (window.__jaaContentLoaded) return;
  window.__jaaContentLoaded = true;

  const ALLOWED_ACTIONS = new Set(["fill", "select", "check", "upload_cv"]);
  const SKIP_TYPES = new Set(["hidden", "submit", "button", "reset", "image", "password"]);
  const MAX_TEXT = 300;

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
      fields.push({ label, name: keyFor(el), type, required: isRequired(el, label), options });
    }
    return fields;
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

  function setValue(el, value) {
    if (isSubmitControl(el)) return false;
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

  function applyPlan(plan, cv) {
    let filled = 0;
    const skipped = [];
    for (const step of plan.steps || []) {
      if (!ALLOWED_ACTIONS.has(step.action)) continue;
      const el = findField(step);
      if (!el || isSubmitControl(el)) {
        skipped.push(step.label);
        continue;
      }
      let done = false;
      try {
        done = step.action === "upload_cv" ? attachCv(el, cv) : setValue(el, String(step.value || ""));
      } catch (_) {
        done = false; // One awkward field must not stop the rest of the form.
      }
      if (done) filled += 1;
      else skipped.push(step.label);
    }
    for (const el of document.querySelectorAll("button, input[type=submit], a")) {
      if (isSubmitControl(el)) {
        el.scrollIntoView({ block: "center", behavior: "smooth" });
        break;
      }
    }
    return { filled, skipped };
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
    try {
      sendResponse({ ok: true, ...applyPlan(message.plan, message.cv) });
    } catch (err) {
      sendResponse({ ok: false, error: String(err) });
    }
    return false;
  });
})();

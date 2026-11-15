/** Allowed fill-plan actions. Keep in sync with app/applications/fill_safety.py */
const ALLOWED_ACTIONS = new Set(["fill", "select", "check", "upload_cv"]);

function assertPlanActions(plan) {
  for (const step of plan?.steps || []) {
    if (!ALLOWED_ACTIONS.has(step.action)) {
      throw new Error(`refusing plan action ${step.action}`);
    }
  }
}

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

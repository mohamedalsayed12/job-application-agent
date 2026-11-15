/** Shared with tests: never activate submit controls. */
export function isSubmitControl({ tag = "", typeAttr = "", text = "", name = "", idAttr = "" } = {}) {
  if (String(typeAttr).toLowerCase() === "submit") return true;
  const hay = `${text} ${name} ${idAttr}`;
  return /submit|apply now|send application|complete application|finish application/i.test(hay)
    && /^(button|input|a)$/i.test(tag);
}

export function assertPlanHasNoSubmitClicks(plan) {
  for (const step of plan?.steps || []) {
    if (step.action === "click" || step.action === "submit") {
      throw new Error("fill plans must not click or submit");
    }
  }
}

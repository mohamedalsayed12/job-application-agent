const apiBase = document.getElementById("apiBase");
const token = document.getElementById("token");
const application = document.getElementById("application");
const status = document.getElementById("status");

async function loadApplications() {
  const { apps, guess } = await chrome.runtime.sendMessage({ type: "jaa-list" });
  application.replaceChildren();
  if (!apps.length) {
    application.append(new Option("No prepared applications. Press Prepare apply on a job first.", ""));
    return;
  }
  for (const row of apps) {
    const label = `${row.title}${row.company ? ` at ${row.company}` : ""}`;
    application.append(new Option(label, row.id, false, row.id === guess));
  }
}

chrome.storage.local.get(["apiBase", "token"]).then((stored) => {
  apiBase.value = stored.apiBase || "http://127.0.0.1:8000";
  token.value = stored.token || "";
  loadApplications();
});

document.getElementById("save").addEventListener("click", async () => {
  await chrome.storage.local.set({ apiBase: apiBase.value.trim(), token: token.value.trim() });
  status.textContent = "Saved.";
  loadApplications();
});

document.getElementById("fill").addEventListener("click", async () => {
  status.textContent = "Reading the form…";
  const result = await chrome.runtime.sendMessage({ type: "jaa-fill-active", applicationId: application.value });
  if (!result?.ok) {
    status.textContent = result?.error || "Failed";
    return;
  }
  const lines = [`Filled ${result.filled} field(s). Review the form and press Submit yourself.`];
  if (result.missing?.length) lines.push(`No saved answer for: ${result.missing.join("; ")}`);
  if (result.skipped?.length) lines.push(`Could not fill: ${result.skipped.join("; ")}`);
  status.textContent = lines.join("\n");
});

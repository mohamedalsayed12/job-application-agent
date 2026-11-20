const apiBase = document.getElementById("apiBase");
const token = document.getElementById("token");
const status = document.getElementById("status");

async function loadReady() {
  const stored = await chrome.storage.local.get(["apiBase", "token"]);
  const base = (stored.apiBase || "http://127.0.0.1:8000").replace(/\/$/, "");
  const select = document.getElementById("application");
  select.replaceChildren();
  if (!stored.token) return;
  const response = await fetch(`${base}/extension/applications/ready`, {
    headers: { Authorization: `Bearer ${stored.token}` },
  });
  if (!response.ok) return;
  for (const row of await response.json()) {
    const option = document.createElement("option");
    option.value = row.id;
    option.textContent = row.apply_url;
    select.appendChild(option);
  }
}

chrome.storage.local.get(["apiBase", "token"]).then((stored) => {
  apiBase.value = stored.apiBase || "http://127.0.0.1:8000";
  token.value = stored.token || "";
  return loadReady();
});

document.getElementById("save").addEventListener("click", async () => {
  await chrome.storage.local.set({
    apiBase: apiBase.value.trim(),
    token: token.value.trim(),
  });
  status.textContent = "Saved.";
  await loadReady();
});

document.getElementById("fill").addEventListener("click", async () => {
  status.textContent = "Filling…";
  const applicationId = document.getElementById("application").value;
  const result = await chrome.runtime.sendMessage({ type: "jaa-fill-active", applicationId });
  status.textContent = result?.ok
    ? "Everything is filled in. Review the form and click Submit yourself."
    : (result?.error || "Failed");
});

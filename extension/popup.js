const apiBase = document.getElementById("apiBase");
const token = document.getElementById("token");
const status = document.getElementById("status");

chrome.storage.local.get(["apiBase", "token"]).then((stored) => {
  apiBase.value = stored.apiBase || "http://127.0.0.1:8000";
  token.value = stored.token || "";
});

document.getElementById("save").addEventListener("click", async () => {
  await chrome.storage.local.set({
    apiBase: apiBase.value.trim(),
    token: token.value.trim(),
  });
  status.textContent = "Saved.";
});

document.getElementById("fill").addEventListener("click", async () => {
  status.textContent = "Filling…";
  const result = await chrome.runtime.sendMessage({ type: "jaa-fill-active" });
  status.textContent = result?.ok ? "Filled. Review and submit yourself." : (result?.error || "Failed");
});

// Types a drafted connection note on a LinkedIn profile (D77). It opens Connect and "Add a note" and puts the
// note in the box. It never presses Send: the person reads the note and sends it. When LinkedIn's page is not
// as expected, the note is copied to the clipboard and a banner says what to click.
(() => {
  if (window.__jaaLinkedin) return;
  window.__jaaLinkedin = true;

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function waitFor(find, timeoutMs = 6000) {
    const until = Date.now() + timeoutMs;
    while (Date.now() < until) {
      const found = find();
      if (found) return found;
      await wait(200);
    }
    return null;
  }

  const visible = (el) => !!el && el.offsetParent !== null;

  function byLabel(selector, pattern, root = document) {
    return [...root.querySelectorAll(selector)].find(
      (el) => visible(el) && pattern.test(`${el.getAttribute("aria-label") || ""} ${el.textContent || ""}`.trim()),
    );
  }

  // The profile's own top card, so a "Connect" button in the "People also viewed" list is never used.
  function topCard() {
    return document.querySelector("main section") || document.querySelector("main") || document;
  }

  function banner(text, note) {
    document.getElementById("jaa-linkedin-banner")?.remove();
    const box = document.createElement("div");
    box.id = "jaa-linkedin-banner";
    box.style.cssText =
      "position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:360px;padding:12px 14px;" +
      "background:#0b3d2e;color:#fff;font:14px/1.4 system-ui,sans-serif;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.3)";
    const title = document.createElement("b");
    title.textContent = "Job Agent";
    const body = document.createElement("div");
    body.textContent = text;
    const close = document.createElement("button");
    close.textContent = "Close";
    close.style.cssText = "margin-top:8px;background:#fff;color:#0b3d2e;border:0;border-radius:4px;padding:4px 10px;cursor:pointer";
    close.onclick = () => box.remove();
    box.append(title, body);
    if (note) {
      const pre = document.createElement("div");
      pre.textContent = note;
      pre.style.cssText = "margin-top:6px;padding:6px;background:rgba(255,255,255,.12);border-radius:4px;white-space:pre-wrap";
      box.append(pre);
    }
    box.append(close);
    document.body.append(box);
  }

  async function copy(note) {
    try {
      await navigator.clipboard.writeText(note);
      return true;
    } catch (_) {
      return false;
    }
  }

  function typeInto(box, text) {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
    box.focus();
    setter.call(box, text);
    box.dispatchEvent(new Event("input", { bubbles: true }));
    box.dispatchEvent(new Event("change", { bubbles: true }));
  }

  async function openConnect() {
    const card = topCard();
    let connect = byLabel("button", /^invite .* to connect|^connect$/i, card);
    if (!connect) {
      const more = byLabel("button", /^more actions|^more$/i, card);
      if (!more) return null;
      more.click();
      connect = await waitFor(() => byLabel('[role="button"], [role="menuitem"], button', /^invite .* to connect|^connect$/i, document), 3000);
    }
    if (!connect) return null;
    connect.click();
    return true;
  }

  async function prefill(note) {
    if (!(await openConnect())) {
      const copied = await copy(note);
      const messageButton = byLabel("button", /^message /i, topCard());
      banner(
        messageButton
          ? `You are already connected or can message this person. ${copied ? "The note is copied: " : ""}press Message, paste it and send it yourself.`
          : `Couldn't find the Connect button on this profile. ${copied ? "The note is copied: " : "Copy the note below, "}open Connect yourself, choose "Add a note" and paste it.`,
        note,
      );
      return { ok: false, reason: "no_connect" };
    }
    const addNote = await waitFor(() => byLabel("button", /add a note/i, document));
    if (addNote) addNote.click();
    const box = await waitFor(() => document.querySelector('textarea[name="message"], textarea#custom-message, .send-invite textarea'));
    if (!box) {
      const copied = await copy(note);
      banner(
        `LinkedIn didn't offer a note box (free accounts get a few personalised invitations a month). ${copied ? "The note is copied" : "Copy the note below"} if you want to send it as a message later.`,
        note,
      );
      return { ok: false, reason: "no_note_box" };
    }
    const limit = Number(box.getAttribute("maxlength")) || 300;
    typeInto(box, note.slice(0, limit));
    banner(
      note.length > limit
        ? `The note is typed in but LinkedIn allows ${limit} characters here, so the end was cut. Read it, edit it, then press Send yourself.`
        : "The note is typed in. Read it, change anything you like, then press Send yourself.",
    );
    return { ok: true };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "jaa-linkedin-note" || typeof message.note !== "string") return false;
    prefill(message.note).then(sendResponse, (err) => sendResponse({ ok: false, reason: String(err) }));
    return true;
  });
})();

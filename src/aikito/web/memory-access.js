/* Account sessions stay in a separate HTTPS window; keys never enter this page. */
async function openMemoryAccess(project) {
  const dialog = document.createElement("dialog");
  dialog.className = "memory-access-dialog";
  dialog.innerHTML = `<form method="dialog"><button class="access-close" aria-label="Close">×</button></form><h2>Memory access · ${escapeHtml(project)}</h2><p>Allow a recipient to read this project's Memory while your devices are offline. Other projects, inbox and global resources stay private.</p><label>Recipient HTTPS origin<input name="origin" type="url" placeholder="https://recipient.example" required></label><label>Permission<select name="permission"><option value="read">Read only</option><option value="write">Read and write</option></select></label><button class="access-connect">Connect</button><p class="access-message" role="status"></p><div class="access-state"></div><button class="access-authorize" disabled>Confirm authorization for 30 days</button>`;
  document.body.append(dialog);
  dialog.showModal();
  const message = dialog.querySelector(".access-message");
  const state = dialog.querySelector(".access-state");
  const authorize = dialog.querySelector(".access-authorize");
  const connect = dialog.querySelector(".access-connect");
  let popup, intent, token, account, timer, busy = false;
  let sequence = 0;
  const pending = new Map();
  function receive(event) {
    if (!intent || event.source !== popup || event.origin !== intent.origin || event.data?.intent_id !== intent.intent_id) return;
    if (event.data.type === "ready") {
      void refresh();
      return;
    }
    const call = pending.get(event.data.request_id);
    if (event.data.type !== "result" || !call) return;
    pending.delete(event.data.request_id);
    clearTimeout(call.timeout);
    event.data.error ? call.reject(new Error(event.data.error)) : call.resolve(event.data.value);
  }
  window.addEventListener("message", receive);
  function rpc(action, payload = {}) {
    return new Promise((resolve, reject) => {
      if (!popup || popup.closed) { reject(new Error("Reconnect the recipient window")); return; }
      const request_id = String(++sequence);
      const timeout = setTimeout(() => { pending.delete(request_id); reject(new Error("Recipient response timed out; refresh status before retrying")); }, 45000);
      pending.set(request_id, {resolve, reject, timeout});
      popup.postMessage({type: "request", intent_id: intent.intent_id, request_id, action, context: intent.context, payload}, intent.origin);
    });
  }
  async function local(path, body) {
    const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Aikito-Console-Token": token}, body: JSON.stringify(body)});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || "Access operation failed");
    return value;
  }
  async function refresh() {
    if (busy || !intent || dialog.open === false) return;
    busy = true;
    try {
      const session = await rpc("session");
      if (account && session.account_id !== account) throw new Error("Account changed; reconnect and confirm again");
      account = session.account_id;
      const grants = await rpc("list", {account_id: account});
      state.replaceChildren();
      const identity = document.createElement("p");
      identity.textContent = `Account: ${account} · ${intent.origin}`;
      state.append(identity);
      const active = grants.filter(grant => grant.status === "active");
      for (const grant of active) {
        const row = document.createElement("p");
        row.textContent = `${grant.permissions.join(" + ")} · expires ${new Date(grant.expires_at * 1000).toLocaleString()} `;
        const revoke = document.createElement("button");
        revoke.textContent = "Revoke";
        revoke.onclick = async () => {
          if (!window.confirm(`Revoke this recipient's ${grant.permissions.join(" + ")} access to ${project} Memory?`)) return;
          revoke.disabled = true;
          try { await rpc("revoke", {account_id: account, grant_id: grant.grant_id}); busy = false; await refresh(); }
          catch (error) { message.textContent = error.message; revoke.disabled = false; }
        };
        row.append(revoke);
        state.append(row);
      }
      message.textContent = active.length ? "Authorized. Consumer connections require their own permission. Revocation here stops future service access." : "Not authorized. Review the account, recipient and permission before confirming.";
      authorize.disabled = active.some(grant => JSON.stringify(grant.permissions) === JSON.stringify(intent.context.permissions));
    } catch (error) {
      authorize.disabled = true;
      message.textContent = error.message;
    } finally { busy = false; }
  }
  connect.onclick = async () => {
    if (intent) { popup?.focus(); void refresh(); return; }
    const origin = dialog.querySelector("[name=origin]").value.trim();
    const permission = dialog.querySelector("[name=permission]").value;
    if (!/^https:\/\/[^/?#]+$/.test(origin)) { message.textContent = "Enter an HTTPS origin without a path"; return; }
    popup = window.open("about:blank", "_blank", "popup,width=620,height=650");
    if (!popup) { message.textContent = "Allow popups for this local console"; return; }
    connect.disabled = true;
    message.textContent = "Reading encrypted Store identity…";
    try {
      token = (await get("/api/console-session")).token;
      intent = await local(`/api/projects/${encodeURIComponent(project)}/access`, {origin, permission});
      const fragment = new URLSearchParams({origin: location.origin, intent: intent.intent_id});
      popup.location = `${intent.origin}/local-console#${fragment}`;
      dialog.querySelector("[name=origin]").disabled = true;
      dialog.querySelector("[name=permission]").disabled = true;
      message.textContent = "Sign in using the recipient window";
      connect.textContent = "Refresh status";
      timer = setInterval(() => { if (popup?.closed) { clearInterval(timer); message.textContent = "Recipient window closed. Reopen this dialog to reconnect."; authorize.disabled = true; } else void refresh(); }, 5000);
    } catch (error) { popup.close(); message.textContent = error.message; }
    finally { connect.disabled = false; }
  };
  authorize.onclick = async () => {
    if (!account || busy) return;
    const permission = intent.context.permissions.includes("write") ? "read and write" : "read";
    if (!window.confirm(`Allow ${intent.origin}, account ${account}, to ${permission} ${project} Memory for 30 days? Authorized content will be readable by this recipient.`)) return;
    busy = true;
    authorize.disabled = true;
    try {
      await local(`/api/access/${intent.intent_id}/approve`, {account_id: account});
      // Only an ownership failure triggers the existing explicit Store proof.
      let request;
      try { request = await rpc("issue", {account_id: account}); }
      catch (error) {
        if (error.message !== "store_not_bound") throw error;
        const challenge = await rpc("challenge", {account_id: account});
        await local(`/api/access/${intent.intent_id}/claim`, challenge);
        request = await rpc("issue", {account_id: account});
      }
      message.textContent = "Sealing and submitting selected Memory keys…";
      await local(`/api/access/${intent.intent_id}/submit`, request);
      busy = false;
      await refresh();
    } catch (error) { message.textContent = `${error.message} Reconnect to authorize again; check status before retrying.`; }
    finally { busy = false; }
  };
  dialog.addEventListener("close", () => {
    clearInterval(timer);
    window.removeEventListener("message", receive);
    for (const call of pending.values()) { clearTimeout(call.timeout); call.reject(new Error("Access dialog closed")); }
    pending.clear();
    popup?.close();
    dialog.remove();
  });
}

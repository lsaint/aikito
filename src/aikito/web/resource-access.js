/* Account sessions stay at the HTTPS recipient; keys stay in the local process. */
async function openResourceAccess(project) {
  const dialog = document.createElement("dialog");
  dialog.className = "resource-access-dialog";
  dialog.innerHTML = `<form method="dialog"><button class="access-close" aria-label="Close">×</button></form><h2>Resource access</h2><p>Choose which collections a recipient can read or change while your devices are offline. Access includes current and future content. Skills include all attachments and scripts. Private configuration is excluded.</p><label>Recipient HTTPS origin<input name="origin" type="url" value="https://aikito.notsonan.com" placeholder="https://recipient.example" required></label><fieldset class="access-collections"><legend>Collections</legend></fieldset><p class="access-progress">Choose collections, connect and sign in, then confirm once.</p><button class="access-connect" disabled>Connect and sign in</button><p class="access-message" role="status"></p><div class="access-state"></div><button class="access-authorize" disabled>Confirm selected access for 30 days</button><button class="access-new">Start a new selection</button>`;
  document.body.append(dialog);
  dialog.showModal();
  const message = dialog.querySelector(".access-message"), state = dialog.querySelector(".access-state");
  const authorize = dialog.querySelector(".access-authorize"), connect = dialog.querySelector(".access-connect");
  let popup, intent, token, account, timer, busy = false, signingIn = false, finished = false, finishRequested = false;
  let verifiedActive = false, confirmed = false, submissionFailed = false, activeGrants = [], historyWarning = "";
  let sequence = 0;
  const pending = new Map();
  const selected = () => [...dialog.querySelectorAll("select[data-collection]")].filter(select => select.value).map(select => ({collection_id: select.dataset.collection, permission: select.value}));
  const hasAccess = collection => activeGrants.some(grant => grant.collection_id === collection.collection_id && JSON.stringify(grant.permissions) === JSON.stringify(collection.permissions));
  dialog.querySelector(".access-new").onclick = () => { dialog.close(); void openResourceAccess(project); };
  dialog.addEventListener("close", () => {
    clearInterval(timer); window.removeEventListener("message", receive);
    for (const call of pending.values()) { clearTimeout(call.timeout); call.reject(new Error("Access dialog closed")); }
    pending.clear(); popup?.close(); dialog.remove();
  });
  try {
    const {collections, records} = await get("/api/access");
    if (!dialog.open) return;
    const choices = [...collections].sort((a, b) => Number(b.collection_id.startsWith(`project/${project}/`)) - Number(a.collection_id.startsWith(`project/${project}/`)));
    for (const collection of choices) {
      const row = document.createElement("label");
      row.className = "access-collection";
      const name = document.createElement("span");
      name.textContent = collection.label;
      const select = document.createElement("select");
      select.dataset.collection = collection.collection_id;
      select.setAttribute("aria-label", `Access to ${collection.label}`);
      for (const [value, label] of [["", "No access"], ["read", "Read only"], ["write", "Read and write"]]) {
        const option = document.createElement("option"); option.value = value; option.textContent = label; select.append(option);
      }
      row.append(name, select); dialog.querySelector(".access-collections").append(row);
    }
    if (records.length) {
      dialog.querySelector("[name=origin]").value = records[0].origin;
      message.textContent = "Saved status is not a current authorization check. Choose collections and reconnect to manage access.";
      for (const record of records) {
        const row = document.createElement("p");
        row.textContent = `${record.collection_id} · ${record.origin} · Account ${record.account_id} · Last checked ${new Date(record.checked_at * 1000).toLocaleString()}`;
        for (const grant of record.grants) {
          const detail = document.createElement("span");
          detail.textContent = ` · ${grant.permissions.join(" + ")} · expires ${new Date(grant.expires_at * 1000).toLocaleString()}`;
          row.append(detail);
          if (grant.expires_at * 1000 > Date.now()) {
            const revoke = document.createElement("button"); revoke.textContent = "Reconnect to revoke";
            revoke.onclick = () => {
              if (intent || connect.disabled) { popup?.focus(); return; }
              dialog.querySelector("[name=origin]").value = record.origin;
              for (const select of dialog.querySelectorAll("select[data-collection]")) select.value = select.dataset.collection === record.collection_id ? (grant.permissions.includes("write") ? "write" : "read") : "";
              connect.click();
            };
            row.append(revoke);
          }
        }
        state.append(row);
      }
    }
    connect.disabled = false;
  } catch { message.textContent = "Resource choices are unavailable. Reopen this dialog to try again."; }
  function receive(event) {
    if (!intent || event.source !== popup || event.origin !== intent.origin || event.data?.intent_id !== intent.intent_id) return;
    if (event.data.type === "signing-in") {
      signingIn = true; authorize.disabled = true;
      message.textContent = "Finish signing in in the recipient window, then return here to confirm the selected collections.";
      for (const call of pending.values()) { clearTimeout(call.timeout); call.reject(new Error(message.textContent)); }
      pending.clear(); return;
    }
    if (event.data.type === "finish") { if (!finished && account) { finishRequested = true; void refresh(); } return; }
    if (event.data.type === "continue") { window.focus(); void refresh(); return; }
    if (event.data.type === "ready") {
      signingIn = false;
      for (const call of pending.values()) { clearTimeout(call.timeout); call.reject(new Error("Refreshing sign-in status…")); }
      pending.clear(); queueMicrotask(() => void refresh()); return;
    }
    const call = pending.get(event.data.request_id);
    if (event.data.type !== "result" || !call) return;
    pending.delete(event.data.request_id); clearTimeout(call.timeout);
    event.data.error ? call.reject(new Error(event.data.error)) : call.resolve(event.data.value);
  }
  window.addEventListener("message", receive);
  function rpc(action, payload = {}) {
    return new Promise((resolve, reject) => {
      if (!popup || popup.closed) { reject(new Error("Reconnect the recipient window")); return; }
      const request_id = String(++sequence);
      const timeout = setTimeout(() => { pending.delete(request_id); reject(new Error("Recipient response timed out; check status before retrying")); }, 45000);
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
  function finish() {
    finished = true; clearInterval(timer); authorize.disabled = true;
    for (const button of state.querySelectorAll("button")) { button.textContent = "Manage access"; button.onclick = () => dialog.querySelector(".access-new").click(); }
    connect.textContent = "Access confirmed"; connect.disabled = true;
    message.textContent = "Selected collection access confirmed. AI client connections require separate approval." + historyWarning;
    popup?.close(); window.focus();
  }
  async function refresh() {
    if (finished || busy || signingIn || !intent || !dialog.open) return;
    busy = true; let checked = false;
    try {
      const session = await rpc("session");
      if (account && session.account_id !== account) throw new Error("Account changed; start a new selection and confirm again");
      account = session.account_id;
      const grants = await rpc("list", {account_id: account});
      activeGrants = grants.filter(grant => grant.status === "active" && grant.expires_at * 1000 > Date.now());
      try { await local(`/api/access/${intent.intent_id}/observe`, {account_id: account, grants}); historyWarning = ""; }
      catch { historyWarning = " This device could not save the status; reconnect next time to check access."; }
      state.replaceChildren();
      const identity = document.createElement("p"); identity.textContent = `Account: ${account} · ${intent.origin} · Store ${intent.context.store_id}`; state.append(identity);
      for (const collection of intent.context.collections) {
        const row = document.createElement("p");
        row.textContent = `${collection.collection_id} · ${collection.permissions.join(" + ")} · ${hasAccess(collection) ? "active" : "not authorized"}`;
        for (const grant of activeGrants.filter(grant => grant.collection_id === collection.collection_id)) {
          const detail = document.createElement("span"); detail.textContent = ` · ${grant.permissions.join(" + ")} until ${new Date(grant.expires_at * 1000).toLocaleString()}`; row.append(detail);
          const revoke = document.createElement("button"); revoke.textContent = "Revoke";
          revoke.onclick = async () => {
            if (busy || !window.confirm(`Revoke ${grant.permissions.join(" + ")} access to ${collection.collection_id}?`)) return;
            busy = true; revoke.disabled = true;
            try { await rpc("revoke", {account_id: account, grant_id: grant.grant_id}); }
            catch (error) { message.textContent = error.message; }
            finally { busy = false; await refresh(); }
          };
          row.append(revoke);
        }
        state.append(row);
      }
      verifiedActive = intent.context.collections.every(hasAccess); checked = true;
      authorize.disabled = verifiedActive || confirmed || submissionFailed;
      authorize.hidden = verifiedActive;
      dialog.querySelector(".access-progress").textContent = verifiedActive ? "Selected collections are authorized." : "Signed in · review each collection and confirm once.";
      message.textContent = (verifiedActive ? "Selected access is active. AI client connections require separate approval." : submissionFailed ? "Some collections may have been authorized. Check each status above, then start a new selection for remaining access. Nothing is retried automatically." : confirmed ? "Access changed after confirmation. Start a new selection to authorize again." : "Signed in. Access is not granted yet. Review the selected collections and confirm below.") + historyWarning;
    } catch (error) { authorize.disabled = true; message.textContent = error.message; }
    finally { busy = false; if (finishRequested && checked) { finishRequested = false; if (verifiedActive) finish(); } }
  }
  connect.onclick = async () => {
    if (intent) { popup?.focus(); void refresh(); return; }
    const origin = dialog.querySelector("[name=origin]").value.trim(), selections = selected();
    if (!/^https:\/\/[^/?#]+$/.test(origin)) { message.textContent = "Enter an HTTPS origin without a path"; return; }
    if (!selections.length || selections.length > 32) { message.textContent = "Select between 1 and 32 collections"; return; }
    popup = window.open("about:blank", "_blank", "popup,width=620,height=650");
    if (!popup) { message.textContent = "Allow popups for this local console"; return; }
    connect.disabled = true;
    for (const field of dialog.querySelectorAll("input, select")) field.disabled = true;
    message.textContent = "Reading encrypted Store identity…";
    try {
      token = (await get("/api/console-session")).token;
      intent = await local("/api/access", {origin, selections});
      if (!dialog.open) throw new Error("Access dialog closed");
      popup.location = `${intent.origin}/local-console#${new URLSearchParams({origin: location.origin, intent: intent.intent_id})}`;
      for (const field of dialog.querySelectorAll("input, select")) field.disabled = true;
      message.textContent = "Sign in in the recipient window, then confirm the selected collections here."; connect.textContent = "Refresh status";
      timer = setInterval(() => { if (popup?.closed && !finished) { clearInterval(timer); message.textContent = "Recipient window closed. Start a new selection to reconnect."; authorize.disabled = true; } else void refresh(); }, 5000);
    } catch (error) { popup.close(); message.textContent = error.message; }
    finally {
      connect.disabled = false;
      if (!intent) for (const field of dialog.querySelectorAll("input, select")) field.disabled = false;
    }
  };
  authorize.onclick = async () => {
    if (!account || busy || confirmed || submissionFailed) return;
    busy = true; authorize.disabled = true;
    try {
      await local(`/api/access/${intent.intent_id}/approve`, {account_id: account}); confirmed = true;
      for (const collection of intent.context.collections.filter(collection => !hasAccess(collection))) {
        let request;
        const payload = {account_id: account, collection_id: collection.collection_id};
        try { request = await rpc("issue", payload); }
        catch (error) {
          if (error.message !== "store_not_bound") throw error;
          const challenge = await rpc("challenge", {account_id: account});
          await local(`/api/access/${intent.intent_id}/claim`, challenge); request = await rpc("issue", payload);
        }
        message.textContent = `Authorizing ${collection.collection_id}…`;
        const receipt = await local(`/api/access/${intent.intent_id}/submit`, request);
        if (receipt.history_saved === false) historyWarning = " This device could not save the status.";
      }
    } catch (error) { submissionFailed = true; message.textContent = error.message; }
    finally { busy = false; await refresh(); }
  };

}

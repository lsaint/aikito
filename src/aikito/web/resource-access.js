/* Online selection is immutable; local clicks only confirm or reject it. */

function getGrantsKey(origin) {
  return "aikito_active_grants" + (origin ? `_${origin}` : "");
}

function loadActiveGrants(origin) {
  try {
    const now = Math.floor(Date.now() / 1000);
    const filterValid = (arr) =>
      Array.isArray(arr)
        ? arr.filter((item) => !item.expires_at || item.expires_at > now)
        : [];
    if (origin) {
      const raw = localStorage.getItem("aikito_active_grants_" + origin);
      if (raw) return filterValid(JSON.parse(raw));
    }
    const all = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && key.startsWith("aikito_active_grants_")) {
        try {
          const items = filterValid(JSON.parse(localStorage.getItem(key) || "[]"));
          all.push(...items);
        } catch {}
      }
    }
    if (all.length) {
      const map = new Map();
      for (const item of all) {
        if (!map.has(item.request_id)) map.set(item.request_id, item);
      }
      return Array.from(map.values());
    }
    return [];
  } catch {
    return [];
  }
}

function storeActiveGrant(origin, grant) {
  try {
    const key = "aikito_active_grants_" + (origin || "default");
    let current = [];
    try {
      const raw = localStorage.getItem(key);
      if (raw) current = JSON.parse(raw);
    } catch {}
    current = current.filter((item) => item.request_id !== grant.request_id);
    current.unshift(grant);
    localStorage.setItem(key, JSON.stringify(current.slice(0, 50)));
  } catch {}
}

function removeActiveGrant(origin, requestId) {
  try {
    const key = "aikito_active_grants_" + (origin || "default");
    let current = [];
    try {
      const raw = localStorage.getItem(key);
      if (raw) current = JSON.parse(raw);
    } catch {}
    current = current.filter((item) => item.request_id !== requestId);
    localStorage.setItem(key, JSON.stringify(current));
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith("aikito_active_grants_") && k !== key) {
        try {
          let list = JSON.parse(localStorage.getItem(k) || "[]");
          list = list.filter((item) => item.request_id !== requestId);
          localStorage.setItem(k, JSON.stringify(list));
        } catch {}
      }
    }
  } catch {}
}

let activeAccessTimer = null;

async function showAccess(data, remember = true) {
  if (activeAccessTimer) {
    clearInterval(activeAccessTimer);
    activeAccessTimer = null;
  }

  const contentEl = document.querySelector("#content");
  const governanceEl = document.querySelector("#governance");
  if (!contentEl) return;

  if (typeof rememberView === "function") {
    rememberView("access", data || {}, remember);
  }
  if (typeof selectResource === "function") {
    selectResource(document.querySelector('.resource[data-access="true"]') || null);
  }

  contentEl.innerHTML = `
    <div class="title">
      <h1>Resource Access</h1>
      <div class="title-meta"><span class="kind-badge" id="access-status-badge">Loading…</span></div>
    </div>
    <div class="remote-panel access-panel">
      <div class="remote-grid" id="access-summary-grid">
        <div class="remote-item"><span class="remote-item-label">Connected AI Clients</span><strong id="access-clients-stat">None</strong></div>
        <div class="remote-item"><span class="remote-item-label">Access State</span><strong id="access-state-text">Ready</strong></div>
        <div class="remote-item full-width"><span class="remote-item-label">Gateway Endpoint</span><span class="remote-code-text" id="access-origin">-</span></div>
      </div>
      <div class="approval-requests" id="approval-requests-list"></div>
      <div class="approval-results" id="approval-results-list"></div>
      <div class="approval-cloud-banner">
        <div>
          <strong>Cloud Authorization Management</strong>
          <p>Active grants and AI client permissions are enforced in the Cloud. You can view or revoke them anytime on the online account page.</p>
        </div>
        <a class="approval-cloud-link" target="_blank" rel="noopener noreferrer" href="#">Open Cloud Account &nearr;</a>
      </div>
      <div class="approval-active-section">
        <div class="approval-section-header">
          <h2>Active grants & revocation</h2>
          <span class="approval-tab-badge" id="approval-active-count">0</span>
        </div>
        <div class="approval-active-list"></div>
      </div>
      <div class="remote-actions">
        <a class="action-btn secondary" id="access-manage-cloud-btn" target="_blank" rel="noopener noreferrer" href="#">Open Online Account &nearr;</a>
        <button type="button" class="action-btn secondary" id="access-refresh-btn">Refresh</button>
      </div>
    </div>
  `;

  if (typeof addBackButton === "function") {
    addBackButton();
  }

  const list = contentEl.querySelector(".approval-requests"),
    results = contentEl.querySelector(".approval-results"),
    activeList = contentEl.querySelector(".approval-active-list"),
    activeCount = contentEl.querySelector("#approval-active-count"),
    cloudLink = contentEl.querySelector(".approval-cloud-link"),
    manageCloudBtn = contentEl.querySelector("#access-manage-cloud-btn"),
    statusBadge = contentEl.querySelector("#access-status-badge"),
    clientsStat = contentEl.querySelector("#access-clients-stat"),
    stateText = contentEl.querySelector("#access-state-text"),
    originEl = contentEl.querySelector("#access-origin"),
    refreshBtn = contentEl.querySelector("#access-refresh-btn");

  let loading = false,
    busy = false,
    latestOrigin = "",
    latestCloudGrants = [];
  const displayed = new Set(),
    outcomes = new Map();

  if (cloudLink) {
    cloudLink.addEventListener("click", (e) => {
      if (!latestOrigin) e.preventDefault();
    });
  }
  if (manageCloudBtn) {
    manageCloudBtn.addEventListener("click", (e) => {
      if (!latestOrigin) e.preventDefault();
    });
  }

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function renderActiveGrants() {
    if (latestOrigin) {
      if (cloudLink) cloudLink.href = latestOrigin + "/account";
      if (manageCloudBtn) manageCloudBtn.href = latestOrigin + "/account";
    }
    const localGrants = loadActiveGrants(latestOrigin);
    const grantsMap = new Map();
    for (const cg of latestCloudGrants) {
      grantsMap.set(cg.grant_id || cg.request_id, cg);
    }
    for (const lg of localGrants) {
      const id = lg.grant_id || lg.request_id;
      if (!grantsMap.has(id)) {
        grantsMap.set(id, lg);
      }
    }
    const grants = Array.from(grantsMap.values());
    if (activeCount) activeCount.textContent = String(grants.length);
    if (!activeList) return;
    activeList.replaceChildren();
    if (!grants.length) {
      const empty = element(
        "p",
        "No active grants tracked locally. When you approve authorization requests, they appear here with quick revocation links. You can also view and revoke all grants at your online account page."
      );
      empty.className = "approval-empty";
      activeList.append(empty);
      return;
    }
    for (const grant of grants) {
      const card = document.createElement("article");
      card.className = "approval-active-card";
      card.append(
        element(
          "h3",
          grant.username ? `${grant.username} (${grant.account_id})` : grant.account_id
        )
      );
      if (grant.recipient) {
        const rec = element("p", `Recipient: ${grant.recipient}`);
        rec.className = "approval-active-meta";
        card.append(rec);
      }
      if (grant.store_id) {
        const store = element("p", "Synchronization connected");
        store.className = "approval-active-meta";
        card.append(store);
      }
      if (grant.created_at || grant.confirmed_at) {
        const ts = grant.created_at || grant.confirmed_at;
        const conf = element(
          "p",
          `Authorized at: ${new Date(ts * 1000).toLocaleString()}`
        );
        conf.className = "approval-active-meta";
        card.append(conf);
      }
      if (grant.expires_at) {
        const exp = element(
          "p",
          `Expires: ${new Date(grant.expires_at * 1000).toLocaleString()}`
        );
        exp.className = "approval-active-meta";
        card.append(exp);
      }
      const ul = document.createElement("ul");
      if (Array.isArray(grant.items) && grant.items.length) {
        for (const item of grant.items) {
          const perms = (item.permissions || []).includes("write")
            ? "Read and write"
            : "Read only";
          const until = item.expires_at
            ? ` · until ${new Date(item.expires_at * 1000).toLocaleString()}`
            : "";
          ul.append(element("li", `${item.collection_id} · ${perms}${until}`));
        }
      } else if (grant.collection_id) {
        const perms = (grant.permissions || []).includes("write")
          ? "Read and write"
          : "Read only";
        ul.append(element("li", `${grant.collection_id} · ${perms}`));
      }
      if (ul.children.length) {
        card.append(ul);
      }
      const actions = document.createElement("div");
      actions.className = "approval-active-actions";
      const revokeLink = document.createElement("a");
      revokeLink.className = "approval-btn-revoke";
      revokeLink.href = (grant.origin || latestOrigin || "") + "/account";
      revokeLink.target = "_blank";
      revokeLink.rel = "noopener noreferrer";
      revokeLink.textContent = "Revoke on Cloud \u2197";
      actions.append(revokeLink);

      if (grant.request_id && !grant.grant_id) {
        const dismissBtn = element("button", "Dismiss from local list");
        dismissBtn.type = "button";
        dismissBtn.className = "approval-btn-dismiss";
        dismissBtn.onclick = () => {
          removeActiveGrant(latestOrigin, grant.request_id);
          renderActiveGrants();
        };
        actions.append(dismissBtn);
      }
      card.append(actions);
      activeList.append(card);
    }
  }

  function summary(pending) {}

  async function refresh() {
    if (loading || busy) return false;
    loading = true;
    try {
      const result = await get("/api/access");
      latestOrigin = result.origin || "";
      latestCloudGrants = Array.isArray(result.grants) ? result.grants : [];
      if (originEl) originEl.textContent = latestOrigin || "Offline";
      const clientNames = [
        ...new Set(
          latestCloudGrants
            .flatMap((g) => g.clients || [])
            .filter(Boolean)
        ),
      ];
      if (clientsStat) {
        clientsStat.textContent = clientNames.length ? clientNames.join(", ") : "None";
        clientsStat.className = clientNames.length ? "status-running" : "";
      }
      renderActiveGrants();

      list.replaceChildren();
      const pending = result.requests.filter(
        (request) => !outcomes.has(request.request_id)
      );
      summary(pending.length);

      const hasActive =
        latestCloudGrants.length > 0 || loadActiveGrants(latestOrigin).length > 0;

      if (stateText) {
        if (pending.length > 0) {
          stateText.textContent = `${pending.length} Pending Approval`;
          stateText.className = "status-stopped";
        } else if (hasActive) {
          stateText.textContent = "Active & Serving";
          stateText.className = "status-running";
        } else {
          stateText.textContent = "Ready";
          stateText.className = "";
        }
      }

      if (statusBadge) {
        if (pending.length > 0) {
          statusBadge.className = "kind-badge issue";
          statusBadge.textContent = `${pending.length} Pending`;
        } else if (hasActive) {
          statusBadge.className = "kind-badge ok";
          statusBadge.textContent = `${latestCloudGrants.length || 1} Active`;
        } else {
          statusBadge.className = "kind-badge";
          statusBadge.textContent = "Ready";
        }
      }

      if (governanceEl && typeof properties === "function") {
        governanceEl.innerHTML = `<h2>Governance</h2>${properties({
          Scope: "Resource Access",
          Connected_Clients: clientNames.length ? clientNames.join(", ") : "None",
          Active_Grants: latestCloudGrants.length,
          Pending_Requests: pending.length,
          Gateway: latestOrigin || "Offline",
          Synchronization: result.store_id ? "Connected" : "Not connected",
          Local_Approval: "Device-confirmed",
          Cloud_Enforcement: "Zero-Knowledge",
        })}`;
      }

      if (pending.length === 0 && outcomes.size === 0) {
        const emptyBanner = document.createElement("div");
        emptyBanner.className = "approval-cloud-banner approval-pending-empty";
        emptyBanner.innerHTML = `
          <div>
            <strong>Pending Requests</strong>
            <p>No authorization requests waiting for local confirmation.</p>
          </div>
          <span class="approval-tab-badge">0 pending</span>
        `;
        list.append(emptyBanner);
      }

      for (const request of pending) {
        displayed.add(request.request_id);
        const card = document.createElement("article");
        card.className = "approval-request-row";

        const main = document.createElement("div");
        main.className = "approval-request-main";

        const heading = document.createElement("div");
        heading.className = "approval-request-heading";
        heading.append(element("strong", request.username || request.account_id));

        if (request.recipient) heading.append(element("span", `Recipient: ${request.recipient}`, "approval-request-pill"));

        const collections = request.items
          .map(
            (item) =>
              `${item.authorization.collection_id} (${item.authorization.permissions.includes("write") ? "read and write" : "read only"})`
          )
          .join(" \u00b7 ");
        const details = element("p", collections, "approval-request-details");

        const feedback = element("p", "", "approval-feedback");
        feedback.setAttribute("role", "status");
        feedback.hidden = true;

        main.append(heading, details, feedback);

        const actions = document.createElement("div");
        actions.className = "approval-request-actions";

        for (const [action, label] of [
          ["confirm", "Confirm authorization"],
          ["reject", "Reject"],
        ]) {
          const button = element("button", label);
          button.type = "button";
          button.className = action === "confirm" ? "approval-confirm" : "approval-reject";
          button.onclick = async () => {
            if (busy || outcomes.has(request.request_id)) return;
            busy = true;
            contentEl
              .querySelectorAll(".approval-request-row button")
              .forEach((node) => (node.disabled = true));
            card.setAttribute("aria-busy", "true");
            button.textContent =
              action === "confirm" ? "Authorizing..." : "Rejecting...";
            feedback.hidden = false;
            feedback.dataset.state = "notice";
            feedback.textContent =
              action === "confirm"
                ? "Authorizing the selected collections. Please wait..."
                : "Rejecting this request...";
            try {
              const { token } = await get("/api/console-session");
              const response = await fetch(
                `/api/access/${request.request_id}/${action}`,
                {
                  method: "POST",
                  headers: {
                    "Content-Type": "application/json",
                    "X-Aikito-Console-Token": token,
                  },
                  body: JSON.stringify({ confirmation: request.confirmation }),
                }
              );
              if (!response.ok)
                throw Error(
                  "Could not verify completion. Check the online account page before creating another request; completed grants remain active."
                );
              outcomes.set(request.request_id, action);
              if (action === "confirm") {
                storeActiveGrant(latestOrigin, {
                  request_id: request.request_id,
                  account_id: request.account_id,
                  username: request.username,
                  recipient: request.recipient,
                  store_id: request.store_id,
                  expires_at: request.expires_at,
                  confirmed_at: Math.floor(Date.now() / 1000),
                  origin: latestOrigin,
                  items: request.items.map((it) => ({
                    collection_id: it.authorization?.collection_id,
                    permissions: it.authorization?.permissions || [],
                    expires_at: it.authorization?.expires_at,
                  })),
                });
                renderActiveGrants();
              }
              feedback.dataset.state = "success";
              feedback.textContent =
                action === "confirm"
                  ? `Authorization completed for ${request.items.length} collection${request.items.length === 1 ? "" : "s"}.`
                  : "Request rejected.";
              button.textContent = action === "confirm" ? "Authorized" : "Rejected";
            } catch (error) {
              outcomes.set(request.request_id, "unknown");
              feedback.dataset.state = "error";
              feedback.textContent = error.message;
              button.textContent = "Check online status";
            } finally {
              busy = false;
              card.removeAttribute("aria-busy");
              card.classList.add("approval-result");
              card.querySelectorAll("button").forEach((node) => {
                if (node !== button) node.hidden = true;
              });
              results.append(card);
              contentEl
                .querySelectorAll(".approval-request-row button")
                .forEach(
                  (node) =>
                    (node.disabled = Boolean(node.closest(".approval-result")))
                );
              card.scrollIntoView({ block: "nearest", behavior: "smooth" });
            }
          };
          actions.append(button);
        }
        card.append(main, actions);
        list.append(card);
      }
      return true;
    } catch (err) {
      if (err && err.data && err.data.origin) {
        latestOrigin = err.data.origin;
        if (originEl) originEl.textContent = latestOrigin;
      }
      renderActiveGrants();
      return false;
    } finally {
      loading = false;
    }
  }

  await refresh();

  if (refreshBtn) {
    refreshBtn.onclick = async () => {
      if (loading || busy) return;
      refreshBtn.disabled = true;
      refreshBtn.textContent = "Refreshing...";
      const ok = await refresh();
      refreshBtn.textContent = ok ? "Refreshed" : "Refresh failed";
      setTimeout(() => {
        if (!busy) {
          refreshBtn.textContent = "Refresh";
          refreshBtn.disabled = false;
        }
      }, 800);
    };
  }

  activeAccessTimer = setInterval(() => {
    if (document.hidden) return;
    const currentActive = document.querySelector("#access-summary-grid");
    if (!currentActive) {
      clearInterval(activeAccessTimer);
      activeAccessTimer = null;
      return;
    }
    void refresh();
  }, 10000);
}

function openAccessConfirmations() {
  return showAccess();
}

let checkingAccessRequests = false;
async function checkAccessConfirmations() {
  if (checkingAccessRequests || document.hidden) return;
  checkingAccessRequests = true;
  try {
    const result = await get("/api/access");
    const count = (result.requests || []).length;
    const accessBtn = document.querySelector("#workspace-access");
    if (accessBtn) {
      if (count > 0) accessBtn.setAttribute("data-pending", String(count));
      else accessBtn.removeAttribute("data-pending");
    }
    const accessResource = document.querySelector('.resource[data-access="true"]');
    if (accessResource) {
      if (count > 0) accessResource.setAttribute("data-pending", String(count));
      else accessResource.removeAttribute("data-pending");
    }
  } catch {}
  finally {
    checkingAccessRequests = false;
  }
}
setTimeout(() => void checkAccessConfirmations(), 1000);
setInterval(() => void checkAccessConfirmations(), 15000);

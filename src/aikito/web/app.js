const groups = [
  ["RESOURCES", [["instructions","Instructions"],["skills","Skills"],["mcps","MCP"],["subagents","Subagents"]]],
  ["KNOWLEDGE", [["inbox","Inbox"],["memory","Memory"]]],
  ["TARGETS", [["projects","Projects"],["agents","Agents"]]],
  ["REMOTE", [["remote","Store & Sync"]]],
  ["HEALTH", [["doctor","Doctor"],["diff","Diff"]]],
];
const explorer = document.querySelector("#explorer");
const content = document.querySelector("#content");
const governance = document.querySelector("#governance");
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const escapeAttribute = escapeHtml;
const label = item => item.skill_name || item.note_name || item.agent_name || item.name || item.server_name;
const id = (kind,item) => kind === "memory" ? `${item.scope_name}/${item.note_name}` : (item.agent_name || item.skill_name || item.name);
async function get(path) { const response=await fetch(path); if(!response.ok) throw new Error((await response.json()).error || response.statusText); return response.json(); }
async function post(path, body = {}) {
  const { token } = await get("/api/console-session");
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Aikito-Console-Token": token,
    },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}
function propertyValue(key,value) { return key === "details" ? `<pre class="pretty-data">${escapeHtml(JSON.stringify(value,null,2))}</pre>` : escapeHtml(typeof value === "object" ? JSON.stringify(value) : value); }
function properties(value) { return `<dl class="properties">${Object.entries(value || {}).map(([key,item]) => `<dt>${escapeHtml(key.replaceAll("_"," "))}</dt><dd>${propertyValue(key,item)}</dd>`).join("")}</dl>`; }
if (typeof marked === "undefined" && typeof require !== "undefined") {
  const path = require("path");
  const baseDir = (typeof __filename !== "undefined" && __filename !== "[eval]")
    ? path.dirname(__filename)
    : (process.argv[1] ? path.dirname(process.argv[1]) : ".");
  globalThis.marked = require(path.resolve(baseDir, "marked.umd.js"));
}

marked.use({
  breaks: true,
  gfm: true,
  extensions: [
    {
      name: "wikilink",
      level: "inline",
      start(src) { return src.indexOf("[["); },
      tokenizer(src) {
        const match = /^\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/.exec(src);
        if (match) {
          return {
            type: "wikilink",
            raw: match[0],
            target: match[1].trim(),
            label: match[2] !== undefined ? match[2].trim() : match[1].trim(),
          };
        }
      },
      renderer(token) {
        const wikilinks = this.parser?.options?.wikilinks || {};
        const target = wikilinks[token.target];
        if (target) {
          return `<a class="wikilink" href="#" data-kind="${escapeAttribute(target.kind)}" data-name="${escapeAttribute(target.name)}">${escapeHtml(token.label)}</a>`;
        }
        return escapeHtml(token.raw);
      },
    },
  ],
  tokenizer: {
    url(src) {
      const match = /^https?:\/\/[^\s<]+/i.exec(src);
      if (match) {
        const trailing = match[0].match(/[)\]},.;:!?。，；：！？、）】》]+$/)?.[0] || "";
        const url = trailing ? match[0].slice(0, -trailing.length) : match[0];
        return {
          type: "link",
          raw: url,
          text: url,
          href: url,
          tokens: [{ type: "text", raw: url, text: url }],
        };
      }
    },
  },
  renderer: {
    link({ href, title, text, tokens }) {
      const targetUrl = href.trim();
      const isHttp = /^https?:\/\//i.test(targetUrl);
      const titleAttr = title ? ` title="${escapeAttribute(title)}"` : (targetUrl ? ` title="${escapeAttribute(targetUrl)}"` : "");
      const content = tokens ? this.parser.parseInline(tokens) : escapeHtml(text);
      if (isHttp) {
        return `<a class="external-link" href="${escapeAttribute(targetUrl)}"${titleAttr} target="_blank" rel="noopener noreferrer">${content}</a>`;
      }
      return `<span class="external-link"${titleAttr}>${content}</span>`;
    },
    html({ text }) {
      return escapeHtml(text);
    },
    codespan({ text }) {
      return `<span class="inline-code">${text}</span>`;
    },
    hr() {
      return '<hr class="markdown-divider">';
    },
    table(token) {
      const header = token.header.map(c => `<th>${this.parser.parseInline(c.tokens)}</th>`).join("");
      const rows = token.rows.map(r => `<tr>${r.map(c => `<td>${this.parser.parseInline(c.tokens)}</td>`).join("")}</tr>`).join("");
      return `<table class="markdown-table"><thead><tr>${header}</tr></thead><tbody>${rows}</tbody></table>`;
    },
  },
});

function markdown(source, wikilinks = {}) {
  if (!source) return "";
  let text = String(source);
  let frontmatter = "";
  if (text.startsWith("---")) {
    const lines = text.split("\n");
    if (lines[0].trim() === "---") {
      const end = lines.findIndex((line, index) => index > 0 && line.trim() === "---");
      if (end > 0) {
        const rows = lines.slice(1, end).map(line => {
          const sep = line.indexOf(":");
          if (sep < 0) return `<tr><td colspan="2">${marked.parseInline(line, { wikilinks })}</td></tr>`;
          const k = line.slice(0, sep).trim();
          const v = line.slice(sep + 1).trim();
          return `<tr><th>${escapeHtml(k)}</th><td>${marked.parseInline(v, { wikilinks })}</td></tr>`;
        }).join("");
        frontmatter = `<table class="markdown-frontmatter"><tbody>${rows}</tbody></table>`;
        text = lines.slice(end + 1).join("\n");
      }
    }
  }
  return frontmatter + marked.parse(text, { wikilinks });
}
let currentView = null;
const previousViews = [];
function rememberView(kind, data, remember) {
  if (remember && currentView) {
    currentView.scrollTop = content.scrollTop;
    previousViews.push(currentView);
    if (previousViews.length > 20) previousViews.shift();
  }
  currentView = {kind, data, selected: null, scrollTop: 0};
  content.scrollTop = 0;
}
function selectResource(button) {
  document.querySelectorAll(".resource.selected").forEach(node => node.classList.remove("selected"));
  button?.classList.add("selected");
  if (currentView) currentView.selected = button;
}
function addBackButton() {
  if (!previousViews.length) return;
  const button = document.createElement("button");
  button.className = "page-back";
  button.type = "button";
  button.textContent = "← Back";
  button.onclick = () => {
    const previous = previousViews.pop();
    if (previous.kind === "workspace") showWorkspace(previous.data, false);
    else if (previous.kind === "remote") showRemote(previous.data, false);
    else show(previous.data, false);
    selectResource(previous.selected);
    content.scrollTop = previous.scrollTop;
  };
  content.prepend(button);
}
function show(detail, remember = true) {
  rememberView("detail", detail, remember);
  const switcher=detail.kind === "markdown" ? `<div class="view-switch"><button class="active" data-view="rendered">Rendered</button><button data-view="raw">Raw</button></div>` : "";
  const body=detail.kind === "markdown" ? `<article class="rendered-content">${markdown(detail.content,detail.wikilinks)}</article>` : properties(detail.content);
  content.innerHTML=`<div class="title"><h1>${escapeHtml(detail.name)}</h1><div class="title-meta"><span class="kind-badge">${escapeHtml(detail.kind)}</span>${switcher}</div></div>${body}`;
  if(detail.kind === "markdown") document.querySelector(".view-switch").onclick=event=>{
    const selected=event.target.closest("button[data-view]"); if(!selected) return;
    const article=document.querySelector("#content article");
    document.querySelectorAll(".view-switch button").forEach(button=>button.classList.toggle("active",button === selected));
    if(selected.dataset.view === "raw") { article.className="source-content"; article.textContent=detail.content; }
    if(selected.dataset.view === "rendered") { article.className="rendered-content"; article.innerHTML=markdown(detail.content,detail.wikilinks); }
  };
  if (detail.project_summary) {
    const project = detail.content;
    content.innerHTML = `<div class="project-heading"><span class="eyebrow">PROJECT</span><h1>${escapeHtml(detail.name)}</h1>${project.description?.trim() ? `<p>${escapeHtml(project.description.trim())}</p>` : ""}</div><div class="project-metrics"><div><strong>${escapeHtml(project.memory_notes_count)}</strong><span>Memory notes</span></div><div><strong>${escapeHtml(project.skills_count)}</strong><span>Skills</span></div><div><strong>${escapeHtml(project.runtime_status)}</strong><span>Local resources</span></div></div><details class="project-details"><summary>Project details</summary>${properties(project)}</details>`;
  }
  addBackButton();
  governance.innerHTML=`<h2>Governance</h2>${properties({Source:detail.source,Scope:detail.scope,Trust:detail.trust,Updated:detail.updated,Freshness:detail.freshness_days == null ? undefined : `${detail.freshness_days} days / ${detail.stale_after_days} days`})}`;
}
async function openResource(kind,item,button) {
  try {
    const detail = await get(`/api/${kind}/${encodeURI(id(kind,item))}`);
    show(detail);
    selectResource(button);
  } catch(error) { content.innerHTML=`<p>${escapeHtml(error.message)}</p>`; }
}
content.addEventListener("click", async event => {
  const link = event.target.closest("a.wikilink");
  if (!link) return;
  event.preventDefault();
  try {
    const detail = await get(`/api/${link.dataset.kind}/${encodeURI(link.dataset.name)}`);
    show(detail);
    const button = [...document.querySelectorAll(".resource[data-kind]")].find(item => item.dataset.kind === link.dataset.kind && item.dataset.name === link.dataset.name);
    selectResource(button);
    if (button) {
      for (const parent of button.closest(".group").querySelectorAll("details")) {
        if (parent.contains(button)) parent.open = true;
      }
    }
  } catch(error) { content.innerHTML=`<p>${escapeHtml(error.message)}</p>`; }
});
function updateStoreBadge(data) {
  const storeBadge = document.querySelector("#store-badge");
  if (!storeBadge) return;
  const remote = (data && "remote" in data) ? data.remote : data;
  if (!remote) {
    storeBadge.textContent = "Store: unbound";
    storeBadge.className = "unbound";
    return;
  }
  if (remote.store_id) {
    storeBadge.textContent = `Store: ${remote.store_id}`;
    storeBadge.className = remote.service?.running ? "ok" : "idle";
  } else {
    storeBadge.textContent = "Store: unbound";
    storeBadge.className = "unbound";
  }
  storeBadge.style.cursor = "pointer";
  storeBadge.onclick = async () => {
    try {
      const report = await get("/api/remote");
      showRemote(report);
      selectResource(document.querySelector('.resource[data-remote="true"]'));
    } catch(e) {}
  };
}

async function showRemote(report, remember = true) {
  rememberView("remote", report, remember);
  selectResource(document.querySelector('.resource[data-remote="true"]') || null);
  const bound = Boolean(report.binding);
  const storeId = report.store_id || (bound ? "Custom Store" : "Unbound");

  const statusBadge = bound
    ? `<span class="kind-badge ok">Bound</span>`
    : `<span class="kind-badge issue">Unbound</span>`;

  let bodyHtml = "";
  if (bound) {
    const running = report.service?.running;
    bodyHtml = `
      <div class="remote-panel">
        <div class="remote-grid">
          <div class="remote-item"><span class="remote-item-label">Store ID</span><strong>${escapeHtml(storeId)}</strong></div>
          <div class="remote-item"><span class="remote-item-label">Sync Service</span><strong class="${running ? "status-running" : "status-stopped"}">${escapeHtml(running ? "Running (PID " + report.service.pid + ")" : "Idle / Stopped")}</strong></div>
          <div class="remote-item full-width"><span class="remote-item-label">Endpoint</span><span class="remote-code-text" title="${escapeAttribute(report.binding.endpoint)}">${escapeHtml(report.binding.endpoint)}</span></div>
          <div class="remote-item full-width"><span class="remote-item-label">Sync ID</span><span class="remote-code-text">${escapeHtml(report.binding.sync_id)}</span></div>
          <div class="remote-item"><span class="remote-item-label">Token Env</span><code>${escapeHtml(report.binding.token_env)}</code></div>
          <div class="remote-item"><span class="remote-item-label">Last Revision</span><span>${escapeHtml(report.last_revision || "Initial")}</span></div>
        </div>
        <div class="remote-actions">
          <button type="button" class="action-btn primary" id="remote-sync-btn">Sync Now</button>
          <button type="button" class="action-btn secondary" id="remote-rebind-toggle">Rebind Store</button>
          <button type="button" class="action-btn danger" id="remote-unbind-btn">Unbind Store</button>
        </div>
        <p id="remote-feedback" class="remote-feedback" role="status" hidden></p>
      </div>

      <div id="remote-bind-box" class="remote-box" hidden>
        <h3>Rebind to a New Store</h3>
        <p class="muted">Replace the current binding with a new Store endpoint. Background sync will be reconfigured.</p>
        <form id="remote-rebind-form" class="remote-form">
          <div class="form-group">
            <label for="rebind-endpoint">Remote Store Endpoint</label>
            <input id="rebind-endpoint" type="url" name="endpoint" required value="${escapeAttribute(report.binding.endpoint)}" placeholder="https://aikito.notsonan.com/v1/stores/main/remote">
          </div>
          <div class="form-group">
            <label for="rebind-token-env">Token Environment Variable</label>
            <input id="rebind-token-env" type="text" name="token_env" required value="${escapeAttribute(report.binding.token_env || 'AIKITO_REMOTE_TOKEN')}">
          </div>
          <button type="submit" class="action-btn primary">Save &amp; Rebind</button>
        </form>
      </div>
    `;
  } else {
    bodyHtml = `
      <div class="remote-panel empty-remote">
        <h2>No Remote Store Bound</h2>
        <p class="muted">Connect your local workspace to an external Aikito Store to enable zero-knowledge encrypted backups, multi-device synchronization, and AI client access.</p>
        <form id="remote-bind-form" class="remote-form">
          <div class="form-group">
            <label for="bind-endpoint">Remote Store Endpoint</label>
            <input id="bind-endpoint" type="url" name="endpoint" required placeholder="https://aikito.notsonan.com/v1/stores/main/remote">
          </div>
          <div class="form-group">
            <label for="bind-token-env">Token Environment Variable</label>
            <input id="bind-token-env" type="text" name="token_env" required value="AIKITO_REMOTE_TOKEN" placeholder="AIKITO_REMOTE_TOKEN">
          </div>
          <button type="submit" class="action-btn primary">Bind Store</button>
        </form>
        <p id="remote-feedback" class="remote-feedback" role="status" hidden></p>
      </div>
    `;
  }

  bodyHtml += `<details class="project-details" style="margin-top: 20px;"><summary>Technical details &amp; diagnostics</summary>${properties(report)}</details>`;

  content.innerHTML = `<div class="title"><h1>Store &amp; Sync</h1><div class="title-meta">${statusBadge}</div></div>${bodyHtml}`;
  addBackButton();
  governance.innerHTML = `<h2>Governance</h2>${properties({
    Scope: "Remote Store",
    Store: storeId,
    Status: bound ? "Bound" : "Unbound",
    Sync: report.service?.running ? "Continuous" : "Manual",
  })}`;

  function showFeedback(text, state = "notice") {
    const fb = content.querySelector("#remote-feedback");
    if (!fb) return;
    fb.hidden = false;
    fb.dataset.state = state;
    fb.textContent = text;
  }

  async function reloadRemote() {
    try {
      const updated = await get("/api/remote");
      showRemote(updated);
      updateStoreBadge(updated);
    } catch (e) {
      showFeedback(e.message, "error");
    }
  }

  const syncBtn = content.querySelector("#remote-sync-btn");
  if (syncBtn) {
    syncBtn.onclick = async () => {
      syncBtn.disabled = true;
      syncBtn.textContent = "Syncing...";
      showFeedback("Synchronizing with remote store...", "notice");
      try {
        const res = await post("/api/remote/sync");
        if (res.conflicts_count > 0) {
          showFeedback(`Sync completed with ${res.conflicts_count} conflict(s).`, "error");
        } else {
          showFeedback(`Sync completed: ${res.applied_count} applied (revision ${res.revision}).`, "success");
        }
        setTimeout(reloadRemote, 1200);
      } catch (err) {
        syncBtn.disabled = false;
        syncBtn.textContent = "Sync Now";
        showFeedback(err.message, "error");
      }
    };
  }

  const unbindBtn = content.querySelector("#remote-unbind-btn");
  if (unbindBtn) {
    unbindBtn.onclick = async () => {
      if (!confirm("Are you sure you want to unbind this workspace from the remote Store? Background sync will be stopped.")) return;
      unbindBtn.disabled = true;
      unbindBtn.textContent = "Unbinding...";
      showFeedback("Unbinding workspace from store...", "notice");
      try {
        await post("/api/remote/unbind", { force: true });
        showFeedback("Workspace unbound successfully.", "success");
        setTimeout(reloadRemote, 1000);
      } catch (err) {
        unbindBtn.disabled = false;
        unbindBtn.textContent = "Unbind Store";
        showFeedback(err.message, "error");
      }
    };
  }

  const rebindToggle = content.querySelector("#remote-rebind-toggle");
  const rebindBox = content.querySelector("#remote-bind-box");
  if (rebindToggle && rebindBox) {
    rebindToggle.onclick = () => {
      rebindBox.hidden = !rebindBox.hidden;
    };
  }

  const rebindForm = content.querySelector("#remote-rebind-form");
  if (rebindForm) {
    rebindForm.onsubmit = async (e) => {
      e.preventDefault();
      const endpoint = rebindForm.endpoint.value.trim();
      const token_env = rebindForm.token_env.value.trim();
      const submitBtn = rebindForm.querySelector("button");
      submitBtn.disabled = true;
      showFeedback("Rebinding to remote store...", "notice");
      try {
        await post("/api/remote/bind", { endpoint, token_env, rebind: true });
        showFeedback("Rebound successfully to store.", "success");
        setTimeout(reloadRemote, 1000);
      } catch (err) {
        submitBtn.disabled = false;
        showFeedback(err.message, "error");
      }
    };
  }

  const bindForm = content.querySelector("#remote-bind-form");
  if (bindForm) {
    bindForm.onsubmit = async (e) => {
      e.preventDefault();
      const endpoint = bindForm.endpoint.value.trim();
      const token_env = bindForm.token_env.value.trim();
      const submitBtn = bindForm.querySelector("button");
      submitBtn.disabled = true;
      showFeedback("Binding workspace to remote store...", "notice");
      try {
        await post("/api/remote/bind", { endpoint, token_env });
        showFeedback("Bound workspace to store successfully.", "success");
        setTimeout(reloadRemote, 1000);
      } catch (err) {
        submitBtn.disabled = false;
        showFeedback(err.message, "error");
      }
    };
  }
}

function showWorkspace(overview, remember = true) {
  rememberView("workspace", overview, remember);
  selectResource(null);
  governance.innerHTML = '<h2>Governance</h2><p class="muted">Select a resource.</p>';
  const storeBound = Boolean(overview.remote?.store_id);
  const storeTitle = storeBound
    ? `Connected Store: ${overview.remote.store_id}`
    : "Connect Remote Store";
  const storeDesc = storeBound
    ? "View remote store diagnostics, connection settings, and sync activity."
    : "Bind this workspace to a remote store to publish and sync resources.";
  const storeMeta = storeBound
    ? (overview.remote.service?.running ? "sync active" : "sync idle")
    : "Unbound · Connect store";

  content.innerHTML = `<div class="project-heading"><span class="eyebrow">YOUR WORKSPACE</span><h1>Context, in one place.</h1><p>Browse your workspace and confirm authorization requests selected online.</p></div><div class="workspace-action-grid"><section class="project-card workspace-action-card" id="workspace-store-card"><h2><button type="button" class="project-open" id="workspace-open-remote">${escapeHtml(storeTitle)}</button></h2><p>${escapeHtml(storeDesc)}</p><div class="project-card-meta">${escapeHtml(storeMeta)}</div></section><section class="project-card workspace-action-card" id="workspace-access-card"><h2><button type="button" class="project-open" id="workspace-access">Review pending authorizations</button></h2><p>Review and confirm authorization requests selected online for this workspace.</p><div class="project-card-meta">Local confirmation required</div></section></div><div class="project-grid">${overview.projects.map(project => `<section class="project-card"><h2><button class="project-open" data-project-open="${escapeAttribute(project.name)}">${escapeHtml(project.name)}</button></h2>${project.description?.trim() ? `<p>${escapeHtml(project.description.trim())}</p>` : ""}<div class="project-card-meta">${escapeHtml(project.memory_notes_count)} Memory notes · ${escapeHtml(project.skills_count)} skills</div></section>`).join("") || '<p>No projects registered yet.</p>'}</div>`;

  const openRemote = content.querySelector("#workspace-open-remote");
  if(openRemote) openRemote.onclick = async () => {
    try {
      const report = await get("/api/remote");
      showRemote(report);
      selectResource(document.querySelector('.resource[data-remote="true"]'));
    } catch(e) { content.textContent = e.message; }
  };
  const storeCard = content.querySelector("#workspace-store-card");
  if (storeCard && openRemote) {
    storeCard.onclick = (e) => {
      if (e.target.closest("button")) return;
      openRemote.click();
    };
  }
  const openAccess = content.querySelector("#workspace-access");
  if (openAccess) openAccess.onclick = () => openAccessConfirmations();
  const accessCard = content.querySelector("#workspace-access-card");
  if (accessCard && openAccess) {
    accessCard.onclick = (e) => {
      if (e.target.closest("button")) return;
      openAccess.click();
    };
  }
  content.querySelectorAll("[data-project-open]").forEach(button => button.onclick = async () => {
    try {
      show(await get(`/api/projects/${encodeURIComponent(button.dataset.projectOpen)}`));
      selectResource(document.querySelector(`.resource[data-kind="projects"][data-name="${CSS.escape(button.dataset.projectOpen)}"]`));
    }
    catch (error) { content.textContent = error.message; }
  });
  addBackButton();
}

async function renderGroup(title, entries, section) {
  section.className = "group";
  section.innerHTML = `<div class="group-label">${title}</div>`;
  const renderedEntries = await Promise.all(
    entries.map(async ([kind, name]) => {
      if (kind === "doctor" || kind === "diff" || kind === "remote") {
        const button = document.createElement("button");
        button.className = "resource";
        if (kind === "remote") button.dataset.remote = "true";
        button.textContent = name;
        button.onclick = async () => {
          const report = await get(`/api/${kind}`);
          if (kind === "remote") {
            showRemote(report);
          } else {
            show({
              name,
              kind: "properties",
              source: `aikito ${kind}`,
              scope: "Workspace",
              trust: "Live diagnostics",
              content: report,
            });
          }
          selectResource(button);
        };
        return button;
      }
      const items = await get(`/api/${kind}`);
      const disclosure = document.createElement("details");
      disclosure.className = "resource-group";
      const heading = document.createElement("summary");
      heading.innerHTML = `<span>${escapeHtml(name)}</span><span class="count">${items.length}</span>`;
      disclosure.append(heading);
      const children = document.createElement("div");
      children.className = "resource-items";
      const appendItem = (item, target) => {
        const button = document.createElement("button");
        button.className = "resource";
        button.dataset.kind = kind;
        button.dataset.name = id(kind, item);
        button.textContent = label(item);
        button.onclick = () => openResource(kind, item, button);
        target.append(button);
      };
      if (kind === "memory") {
        const scopes = new Map();
        items.forEach((item) =>
          scopes.set(item.scope_name, [
            ...(scopes.get(item.scope_name) || []),
            item,
          ])
        );
        scopes.forEach((scopeItems, scope) => {
          const scopeGroup = document.createElement("details");
          scopeGroup.className = "memory-scope";
          const scopeHeading = document.createElement("summary");
          scopeHeading.innerHTML = `<span>${escapeHtml(scope)}</span><span class="count">${scopeItems.length}</span>`;
          scopeGroup.append(scopeHeading);
          const scopeChildren = document.createElement("div");
          scopeChildren.className = "memory-items";
          scopeItems.forEach((item) => appendItem(item, scopeChildren));
          scopeGroup.append(scopeChildren);
          children.append(scopeGroup);
        });
      } else {
        items.forEach((item) => appendItem(item, children));
      }
      disclosure.append(children);
      return disclosure;
    })
  );
  renderedEntries.forEach((element) => section.append(element));
}
(async () => {
  try {
    const remotePromise = get("/api/remote")
      .then((remote) => {
        updateStoreBadge(remote);
        return remote;
      })
      .catch(() => null);

    const overviewPromise = get("/api/overview")
      .then(async (overview) => {
        document.querySelector("#workspace").textContent = overview.workspace;
        document.querySelector("#version").textContent = `Aikito ${overview.version}`;
        const health = document.querySelector("#health");
        health.textContent = overview.healthy
          ? "Healthy"
          : `${overview.counts.issues} issues`;
        health.className = overview.healthy ? "ok" : "issue";
        const remote = await remotePromise;
        if (remote) overview.remote = remote;
        updateStoreBadge(overview);
        showWorkspace(overview);
        return overview;
      })
      .catch((error) => {
        content.textContent = error.message;
      });

    const groupSections = groups.map(([title]) => {
      const section = document.createElement("section");
      section.className = "group";
      section.innerHTML = `<div class="group-label">${title}</div>`;
      explorer.append(section);
      return section;
    });

    await Promise.all(
      groups.map((group, idx) =>
        renderGroup(group[0], group[1], groupSections[idx])
      )
    );
    await overviewPromise;
  } catch (error) {
    explorer.textContent = error.message;
  }
})();
document.addEventListener("scroll", event => {
  const element = event.target === document ? document.documentElement : event.target;
  if (!element || !element.classList) return;
  element.classList.add("is-scrolling");
  clearTimeout(element._scrollbarTimer);
  element._scrollbarTimer = setTimeout(() => {
    element.classList.remove("is-scrolling");
  }, 600);
}, { capture: true, passive: true });
function initResizers() {
  const main = document.querySelector("main");
  const left = document.querySelector("#resizer-left");
  const right = document.querySelector("#resizer-right");
  const savedNav = localStorage.getItem("aikito-nav-w");
  if (savedNav) main.style.setProperty("--nav-w", savedNav);
  const savedAside = localStorage.getItem("aikito-aside-w");
  if (savedAside) main.style.setProperty("--aside-w", savedAside);

  const bind = (handle, isLeft) => {
    if (!handle) return;
    handle.addEventListener("pointerdown", event => {
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      handle.classList.add("dragging");
      document.body.classList.add("is-resizing");
      const target = document.querySelector(isLeft ? "#explorer" : "#governance");
      const startWidth = target ? target.getBoundingClientRect().width : (isLeft ? 260 : 280);
      const startX = event.clientX;
      const onMove = e => {
        const delta = e.clientX - startX;
        const width = isLeft
          ? Math.max(160, Math.min(window.innerWidth - 450, Math.round(startWidth + delta)))
          : Math.max(180, Math.min(window.innerWidth - 450, Math.round(startWidth - delta)));
        main.style.setProperty(isLeft ? "--nav-w" : "--aside-w", `${width}px`);
      };
      const onUp = e => {
        handle.releasePointerCapture(e.pointerId);
        handle.classList.remove("dragging");
        document.body.classList.remove("is-resizing");
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        handle.removeEventListener("pointercancel", onUp);
        const prop = isLeft ? "--nav-w" : "--aside-w";
        const val = main.style.getPropertyValue(prop);
        if (val) localStorage.setItem(isLeft ? "aikito-nav-w" : "aikito-aside-w", val);
      };
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
      handle.addEventListener("pointercancel", onUp);
    });
  };
  bind(left, true);
  bind(right, false);
}
initResizers();

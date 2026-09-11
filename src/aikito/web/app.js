const groups = [
  ["RESOURCES", [["instructions","Instructions"],["skills","Skills"],["mcps","MCP"],["subagents","Subagents"]]],
  ["KNOWLEDGE", [["inbox","Inbox"],["memory","Memory"]]],
  ["TARGETS", [["projects","Projects"],["agents","Agents"]]],
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
function show(detail) {
  const switcher=detail.kind === "markdown" ? `<div class="view-switch"><button class="active" data-view="rendered">Rendered</button><button data-view="raw">Raw</button></div>` : "";
  const body=detail.kind === "markdown" ? `<article class="rendered-content">${markdown(detail.content,detail.wikilinks)}</article>` : properties(detail.content);
  content.innerHTML=`<div class="title"><h1>${escapeHtml(detail.name)}</h1><div class="title-meta"><span class="muted">${escapeHtml(detail.kind)}</span>${switcher}</div></div>${body}`;
  if(detail.kind === "markdown") document.querySelector(".view-switch").onclick=event=>{
    const selected=event.target.closest("button[data-view]"); if(!selected) return;
    const article=document.querySelector("#content article");
    document.querySelectorAll(".view-switch button").forEach(button=>button.classList.toggle("active",button === selected));
    if(selected.dataset.view === "raw") { article.className="source-content"; article.textContent=detail.content; }
    if(selected.dataset.view === "rendered") { article.className="rendered-content"; article.innerHTML=markdown(detail.content,detail.wikilinks); }
  };
  governance.innerHTML=`<h2>Governance</h2>${properties({Source:detail.source,Scope:detail.scope,Trust:detail.trust,Updated:detail.updated,Indexed:detail.indexed,Freshness:detail.freshness_days == null ? undefined : `${detail.freshness_days} days / ${detail.stale_after_days} days`})}`;
}
async function openResource(kind,item,button) { document.querySelectorAll(".resource").forEach(node=>node.classList.remove("selected")); button.classList.add("selected"); try { show(await get(`/api/${kind}/${encodeURI(id(kind,item))}`)); } catch(error) { content.innerHTML=`<p>${escapeHtml(error.message)}</p>`; } }
content.addEventListener("click",async event=>{ const link=event.target.closest("a.wikilink"); if(!link) return; event.preventDefault(); try { const detail=await get(`/api/${link.dataset.kind}/${encodeURI(link.dataset.name)}`); show(detail); const button=[...document.querySelectorAll(".resource[data-kind]")].find(item=>item.dataset.kind === link.dataset.kind && item.dataset.name === link.dataset.name); if(button) { document.querySelectorAll(".resource").forEach(item=>item.classList.remove("selected")); button.classList.add("selected"); for(const parent of button.closest(".group").querySelectorAll("details")) { if(parent.contains(button)) parent.open=true; } } } catch(error) { content.innerHTML=`<p>${escapeHtml(error.message)}</p>`; } });
async function renderGroup(title,entries) {
  const section=document.createElement("section"); section.className="group"; section.innerHTML=`<div class="group-label">${title}</div>`;
  for (const [kind,name] of entries) {
    if(kind === "doctor" || kind === "diff") { const button=document.createElement("button"); button.className="resource"; button.textContent=name; button.onclick=async()=>{ const report=await get(`/api/${kind}`); show({name,kind:"properties",source:`aikito ${kind}`,scope:"Workspace",trust:"Live diagnostics",content:report}); }; section.append(button); continue; }
    const items=await get(`/api/${kind}`);
    const disclosure=document.createElement("details"); disclosure.className="resource-group";
    const heading=document.createElement("summary"); heading.innerHTML=`<span>${escapeHtml(name)}</span><span class="count">${items.length}</span>`; disclosure.append(heading);
    const children=document.createElement("div"); children.className="resource-items";
    const appendItem=(item,target)=>{ const button=document.createElement("button"); button.className="resource"; button.dataset.kind=kind; button.dataset.name=id(kind,item); button.textContent=label(item); button.onclick=()=>openResource(kind,item,button); target.append(button); };
    if(kind === "memory") {
      const scopes=new Map(); items.forEach(item=>scopes.set(item.scope_name,[...(scopes.get(item.scope_name) || []),item]));
      scopes.forEach((scopeItems,scope)=>{
        const scopeGroup=document.createElement("details"); scopeGroup.className="memory-scope";
        const scopeHeading=document.createElement("summary"); scopeHeading.innerHTML=`<span>${escapeHtml(scope)}</span><span class="count">${scopeItems.length}</span>`; scopeGroup.append(scopeHeading);
        const scopeChildren=document.createElement("div"); scopeChildren.className="memory-items"; scopeItems.forEach(item=>appendItem(item,scopeChildren)); scopeGroup.append(scopeChildren); children.append(scopeGroup);
      });
    } else items.forEach(item=>appendItem(item,children));
    disclosure.append(children); section.append(disclosure);
  }
  explorer.append(section);
}
(async()=>{ try { const overview=await get("/api/overview"); document.querySelector("#workspace").textContent=overview.workspace; document.querySelector("#version").textContent=`Aikito ${overview.version}`; const health=document.querySelector("#health"); health.textContent=overview.healthy ? "Healthy" : `${overview.counts.issues} issues`; health.className=overview.healthy ? "ok" : "issue"; for(const group of groups) await renderGroup(...group); } catch(error) { explorer.textContent=error.message; } })();
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


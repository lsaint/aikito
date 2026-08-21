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
const inlineMarkdown = (value,wikilinks={}) => {
  const pattern=/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]|\[([^\]]+)\]\(([^)]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|(https?:\/\/[^\s<]+)/g;
  let output=""; let cursor=0;
  for(const match of value.matchAll(pattern)) {
    const [original,linkTarget,linkLabel,mdLabel,mdTarget,codeValue,boldValue,urlValue]=match;
    output+=escapeHtml(value.slice(cursor,match.index));
    if(linkTarget !== undefined) {
      const target=wikilinks[linkTarget.trim()];
      output+=target ? `<a class="wikilink" href="#" data-kind="${escapeAttribute(target.kind)}" data-name="${escapeAttribute(target.name)}">${escapeHtml(linkLabel || linkTarget)}</a>` : escapeHtml(original);
    } else if(mdLabel !== undefined && mdTarget !== undefined) {
      const targetUrl=mdTarget.trim();
      const isHttp=/^https?:\/\//i.test(targetUrl);
      output+=isHttp
        ? `<a class="external-link" href="${escapeAttribute(targetUrl)}" title="${escapeAttribute(targetUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(mdLabel)}</a>`
        : `<span class="external-link" title="${escapeAttribute(targetUrl)}">${escapeHtml(mdLabel)}</span>`;
    } else if(codeValue !== undefined) output+=`<span class="inline-code">${escapeHtml(codeValue)}</span>`;
    else if(boldValue !== undefined) output+=`<strong>${escapeHtml(boldValue)}</strong>`;
    else if(urlValue !== undefined) {
      const trailing=urlValue.match(/[)\]},.;:!?。，；：！？、）】》]+$/)?.[0] || ""; const url=trailing ? urlValue.slice(0,-trailing.length) : urlValue;
      output+=`<a class="external-link" href="${escapeAttribute(url)}" title="${escapeAttribute(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a>${escapeHtml(trailing)}`;
    }
    cursor=match.index+original.length;
  }
  return output+escapeHtml(value.slice(cursor));
};
const tableCells = line => line.trim().replace(/^\|/g,"").replace(/\|$/g,"").split("|").map(cell=>cell.trim());
const isTableDivider = line => {
  const cells=tableCells(line);
  return cells.length > 0 && cells.every(cell=>/^:?-{3,}:?$/.test(cell));
};
function markdown(source,wikilinks={}) {
  const lines=source.split("\n");
  let frontmatter="";
  if(lines[0]?.trim() === "---") {
    const end=lines.findIndex((line,index)=>index > 0 && line.trim() === "---");
    if(end > 0) {
      const rows=lines.slice(1,end).map(line=>{
        const separator=line.indexOf(":");
        if(separator < 0) return `<tr><td colspan="2">${inlineMarkdown(line,wikilinks)}</td></tr>`;
        return `<tr><th>${escapeHtml(line.slice(0,separator).trim())}</th><td>${inlineMarkdown(line.slice(separator+1).trim(),wikilinks)}</td></tr>`;
      }).join("");
      frontmatter=`<table class="markdown-frontmatter"><tbody>${rows}</tbody></table>`;
      lines.splice(0,end+1);
    }
  }
  let code=false;
  const output=[];
  for(let index=0;index<lines.length;index++) {
    const line=lines[index];
    if(line.startsWith("```")) { code=!code; output.push(code ? "<pre><code>" : "</code></pre>"); continue; }
    if(code) { output.push(escapeHtml(line)+"\n"); continue; }
    if(line.includes("|") && isTableDivider(lines[index+1] || "")) {
      const headers=tableCells(line).map(cell=>`<th>${inlineMarkdown(cell,wikilinks)}</th>`).join("");
      const rows=[];
      index+=2;
      while(index<lines.length && lines[index].includes("|")) {
        const cells=tableCells(lines[index]).map(cell=>`<td>${inlineMarkdown(cell,wikilinks)}</td>`).join("");
        rows.push(`<tr>${cells}</tr>`);
        index++;
      }
      index--;
      output.push(`<table class="markdown-table"><thead><tr>${headers}</tr></thead><tbody>${rows.join("")}</tbody></table>`);
      continue;
    }
    if(line.trim() === "---") output.push('<hr class="markdown-divider">');
    else if(line.startsWith("### ")) output.push(`<h3>${inlineMarkdown(line.slice(4),wikilinks)}</h3>`);
    else if(line.startsWith("## ")) output.push(`<h2>${inlineMarkdown(line.slice(3),wikilinks)}</h2>`);
    else if(line.startsWith("# ")) output.push(`<h1>${inlineMarkdown(line.slice(2),wikilinks)}</h1>`);
    else if(line.startsWith("- ")) output.push(`<div class="list-item">• ${inlineMarkdown(line.slice(2),wikilinks)}</div>`);
    else if(line) output.push(`<p>${inlineMarkdown(line,wikilinks)}</p>`);
  }
  return frontmatter+output.join("");
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


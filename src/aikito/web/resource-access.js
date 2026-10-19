/* Online selection is immutable; local clicks only confirm or reject it. */
async function openAccessConfirmations() {
  if(document.querySelector(".approval-dialog"))return;
  const dialog=document.createElement("dialog");dialog.className="approval-dialog";
  dialog.innerHTML='<form method="dialog"><button class="approval-close" aria-label="Close">\u00d7</button></form><h2>Pending authorizations</h2><p>Choose resources on your online account page. Review the complete request here before releasing collection keys.</p><p class="approval-status" role="status"></p><div class="approval-requests"></div><div class="approval-results"></div><button type="button" class="approval-refresh">Refresh requests</button>';
  document.body.append(dialog);dialog.showModal();
  const state=dialog.querySelector(".approval-status"),list=dialog.querySelector(".approval-requests"),results=dialog.querySelector(".approval-results");
  let loading=false,busy=false;
  const displayed=new Set(),outcomes=new Map();
  function element(tag,text){const node=document.createElement(tag);node.textContent=text;return node;}
  function summary(pending){
    const parts=[pending?`${pending} requests waiting for your confirmation`:outcomes.size?"No more requests waiting.":"No requests waiting. Select resources on your online account page first."];
    for(const [outcome,label] of [["confirm","authorized"],["reject","rejected"],["unknown","need online review"]]){
      const count=[...outcomes.values()].filter(value=>value===outcome).length;
      if(count)parts.push(`${count} ${label}`);
    }
    state.textContent=parts.join(" \u00b7 ");
  }
  async function refresh(){if(loading||busy||!dialog.open)return;loading=true;try{
    const result=await get("/api/access");if(!dialog.open||busy)return;list.replaceChildren();
    const pending=result.requests.filter(request=>!outcomes.has(request.request_id));summary(pending.length);
    const link=element("a","Open online authorization management");link.href=result.origin+"/account";link.target="_blank";link.rel="noopener noreferrer";list.append(link);
    for(const request of pending){displayed.add(request.request_id);const card=document.createElement("article");card.className="approval-request";
      card.append(element("h3",request.username||request.account_id),element("p",`Account: ${request.account_id}`),element("p",`Recipient: ${request.recipient}`),element("p",`Store: ${request.store_id}`),element("p",`Confirm before: ${new Date(request.expires_at*1000).toLocaleString()}`));
      const resources=document.createElement("ul");for(const item of request.items){const auth=item.authorization;resources.append(element("li",`${auth.collection_id} \u00b7 ${auth.permissions.includes("write")?"Read and write":"Read only"} \u00b7 until ${new Date(auth.expires_at*1000).toLocaleString()}`));}card.append(resources);
      const feedback=element("p","");feedback.className="approval-feedback";feedback.setAttribute("role","status");feedback.hidden=true;card.append(feedback);
      for(const [action,label] of [["confirm","Confirm authorization"],["reject","Reject"]]){
        const button=element("button",label);button.type="button";
        button.onclick=async()=>{
          if(busy||outcomes.has(request.request_id))return;
          busy=true;dialog.querySelectorAll("button").forEach(node=>node.disabled=true);
          card.setAttribute("aria-busy","true");button.textContent=action==="confirm"?"Authorizing...":"Rejecting...";
          feedback.hidden=false;feedback.dataset.state="notice";
          feedback.textContent=action==="confirm"?"Authorizing the selected collections. Please wait...":"Rejecting this request...";
          state.textContent=feedback.textContent;
          try{
            const {token}=await get("/api/console-session");
            const response=await fetch(`/api/access/${request.request_id}/${action}`,{method:"POST",headers:{"Content-Type":"application/json","X-Aikito-Console-Token":token},body:JSON.stringify({confirmation:request.confirmation})});
            if(!response.ok)throw Error("Could not verify completion. Check the online account page before creating another request; completed grants remain active.");
            outcomes.set(request.request_id,action);
            feedback.dataset.state="success";
            feedback.textContent=action==="confirm"?`Authorization completed for ${request.items.length} collection${request.items.length===1?"":"s"}. Return to the online account page or reconnect your AI client.`:"Request rejected. You can return to the online account page.";
            button.textContent=action==="confirm"?"Authorized":"Rejected";
          }catch(error){
            outcomes.set(request.request_id,"unknown");feedback.dataset.state="error";feedback.textContent=error.message;button.textContent="Check online status";
          }finally{
            busy=false;card.removeAttribute("aria-busy");card.classList.add("approval-result");
            card.querySelectorAll("button").forEach(node=>{if(node!==button)node.hidden=true;});
            results.append(card);state.textContent=feedback.textContent;
            dialog.querySelectorAll("button").forEach(node=>node.disabled=Boolean(node.closest(".approval-result")));
            card.scrollIntoView({block:"nearest",behavior:"smooth"});
          }
        };
        card.append(button);
      }
      list.append(card);
    }
  }catch{state.textContent="Cannot fetch requests. Check the remote binding and network, then refresh.";}finally{loading=false;}}
  dialog.querySelector(".approval-refresh").onclick=()=>void refresh();
  const timer=setInterval(()=>void refresh(),10000);
  dialog.addEventListener("cancel",event=>{if(busy)event.preventDefault();});
  dialog.addEventListener("close",()=>{clearInterval(timer);for(const identity of displayed)dismissedAccessRequests.add(identity);dialog.remove();});
  await refresh();
}
const dismissedAccessRequests=new Set();
let checkingAccessRequests=false;
async function checkAccessConfirmations(){
  if(checkingAccessRequests||document.hidden||document.querySelector(".approval-dialog"))return;
  checkingAccessRequests=true;
  try{const result=await get("/api/access");if(result.requests.some(request=>!dismissedAccessRequests.has(request.request_id)))await openAccessConfirmations();}catch{}finally{checkingAccessRequests=false;}
}
setTimeout(()=>void checkAccessConfirmations(),1000);
setInterval(()=>void checkAccessConfirmations(),15000);

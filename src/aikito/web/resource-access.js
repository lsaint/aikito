/* Online selection is immutable; local clicks only confirm or reject it. */
async function openAccessConfirmations() {
  const dialog=document.createElement("dialog");dialog.className="approval-dialog";
  dialog.innerHTML='<form method="dialog"><button class="approval-close" aria-label="Close">×</button></form><h2>Pending authorizations</h2><p>Choose resources on your online account page. Review the complete request here before releasing collection keys.</p><p class="approval-status" role="status"></p><div class="approval-requests"></div><button type="button" class="approval-refresh">Refresh requests</button>';
  document.body.append(dialog);dialog.showModal();
  const state=dialog.querySelector(".approval-status"),list=dialog.querySelector(".approval-requests");
  let loading=false,busy=false;
  const displayed=new Set();
  function element(tag,text){const node=document.createElement(tag);node.textContent=text;return node;}
  async function refresh(){if(loading||busy||!dialog.open)return;loading=true;try{
    const result=await get("/api/access");if(!dialog.open)return;list.replaceChildren();
    state.textContent=result.requests.length?`${result.requests.length} requests waiting for your confirmation`:"No requests waiting. Select resources on your online account page first.";
    const link=element("a","Open online authorization management");link.href=result.origin+"/account";link.target="_blank";link.rel="noopener noreferrer";list.append(link);
    for(const request of result.requests){displayed.add(request.request_id);const card=document.createElement("article");card.className="approval-request";
      card.append(element("h3",request.username||request.account_id),element("p",`Account: ${request.account_id}`),element("p",`Recipient: ${request.recipient}`),element("p",`Workspace: ${request.store_id}`),element("p",`Confirm before: ${new Date(request.expires_at*1000).toLocaleString()}`));
      const resources=document.createElement("ul");for(const item of request.items){const auth=item.authorization;resources.append(element("li",`${auth.collection_id} · ${auth.permissions.includes("write")?"Read and write":"Read only"} · until ${new Date(auth.expires_at*1000).toLocaleString()}`));}card.append(resources);
      for(const [action,label] of [["confirm","Confirm authorization"],["reject","Reject"]]){const button=element("button",label);button.type="button";button.onclick=async()=>{if(busy)return;busy=true;dialog.querySelectorAll("button").forEach(node=>node.disabled=true);state.textContent=action==="confirm"?"Validating and submitting the selected collection keys…":"Rejecting request…";try{const {token}=await get("/api/console-session");const response=await fetch(`/api/access/${request.request_id}/${action}`,{method:"POST",headers:{"Content-Type":"application/json","X-Aikito-Console-Token":token},body:JSON.stringify({confirmation:request.confirmation})});if(!response.ok)throw Error("Request could not be completed. Check its online status before creating another request; completed grants remain active.");state.textContent=action==="confirm"?"Authorization completed. Return to the online account page or reconnect your AI client.":"Request rejected.";card.remove();}catch(error){state.textContent=error.message;card.remove();}finally{busy=false;dialog.querySelectorAll("button").forEach(node=>node.disabled=false);}};card.append(button);}list.append(card);
    }
  }catch{state.textContent="Cannot fetch requests. Check the remote binding and network, then refresh.";}finally{loading=false;}}
  dialog.querySelector(".approval-refresh").onclick=()=>void refresh();
  const timer=setInterval(()=>void refresh(),10000);
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

let sessions = [];
let profiles = {};
let eventCache = [];
let diagnosticCache = [];
let currentTab = "lives";
let ws = null;
let wsPing = null;

const $ = id => document.getElementById(id);
const esc = value => {
  const d = document.createElement("div");
  d.textContent = value ?? "";
  return d.innerHTML;
};

async function request(path, options={}) {
  options.credentials = "same-origin";
  if (options.body && !options.headers) options.headers = {"Content-Type":"application/json"};
  const r = await fetch(path, options);
  if (r.status === 401) {
    showLogin();
    throw new Error("Sessão expirada");
  }
  if (!r.ok) {
    let detail = await r.text();
    try { detail = JSON.parse(detail).detail || detail; } catch {}
    throw new Error(detail);
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("application/json") ? r.json() : r;
}

async function authCheck() {
  const d = await fetch("/api/auth/status", {credentials:"same-origin"}).then(r=>r.json());
  if (!d.authenticated) { showLogin(); return false; }
  hideLogin(); return true;
}

function showLogin(){ $("login").classList.remove("hidden"); }
function hideLogin(){ $("login").classList.add("hidden"); }

async function login(){
  $("loginError").textContent="";
  try{
    await fetch("/api/auth/login",{
      method:"POST",headers:{"Content-Type":"application/json"},credentials:"same-origin",
      body:JSON.stringify({password:$("loginPassword").value})
    }).then(async r=>{if(!r.ok)throw new Error((await r.json()).detail||"Falha")});
    $("loginPassword").value="";
    hideLogin(); await boot();
  }catch(e){$("loginError").textContent=e.message}
}

async function logout(){
  await fetch("/api/auth/logout",{method:"POST",credentials:"same-origin"});
  if(ws)ws.close(); showLogin();
}

function tab(name){
  document.querySelectorAll(".page").forEach(p=>p.classList.add("hidden"));
  $(name).classList.remove("hidden");
  document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("active",b.dataset.tab===name));
  currentTab=name;
  const titles={
    lives:["Lives","Conecte contas sem redeploy e acompanhe cada sessão."],
    events:["Eventos","Feed em tempo real e exportação."],
    monitor:["Monitor","Diagnóstico de conexão e sinais de segurança."],
    rules:["Regras","Transforme eventos em ações configuráveis para o Roblox."],
    ranking:["Ranking","Participantes mais ativos da sessão."],
    roblox:["Roblox/Teste","Simule eventos sem gastar presentes reais."]
  };
  $("pageTitle").textContent=titles[name][0];$("pageSubtitle").textContent=titles[name][1];
  if(name==="events")loadEvents();
  if(name==="monitor")loadMonitor();
  if(name==="rules")loadRules();
  if(name==="ranking")loadRanking();
}

function profileOptions(){
  return Object.entries(profiles).map(([k,v])=>`<option value="${k}">${esc(v.label||k)}</option>`).join("");
}

async function loadMeta(){
  const d=await request("/api/meta");
  profiles=d.profiles||{};
  $("versionBadge").textContent="v"+d.version;
  $("connectProfile").innerHTML=profileOptions();
  renderProfileButtons();
}

function renderProfileButtons(){
  $("profileButtons").innerHTML=Object.entries(profiles).map(([k,v])=>
    `<button onclick="applyProfile('${k}')">${esc(v.label||k)}</button>`
  ).join("");
}

async function refresh(){
  try{
    const h=await fetch("/health").then(r=>r.json());
    $("backendStatus").textContent="● Backend online";
    $("backendStatus").className="badge green";
    const d=await request("/api/live/sessions");
    sessions=d.sessions||[];
    renderSessions();
    updateSessionSelects();
  }catch(e){
    $("backendStatus").textContent="● Backend/API indisponível";
    $("backendStatus").className="badge red";
  }
}

function renderSessions(){
  $("sessions").innerHTML=sessions.length?sessions.map(s=>{
    const st=s.connection_state||"idle";
    const paused=s.automation_paused?`<div class="safety-banner">Automações pausadas pelo monitor.</div>`:"";
    return `<div class="card">
      ${paused}
      <div class="session-title">
        <div><strong>@${esc(s.username)}</strong><div class="status ${st}">● ${esc(st.toUpperCase())}</div></div>
        <button class="danger" onclick="disconnectLive('${s.session_id}')">Desconectar</button>
      </div>
      <div class="stats">
        <div class="stat"><span>Viewers</span><b>${s.viewers??"-"}</b></div>
        <div class="stat"><span>Gifts</span><b>${s.stats?.gifts??0}</b></div>
        <div class="stat"><span>Moedas</span><b>${s.stats?.gift_coins??0}</b></div>
        <div class="stat"><span>Comentários</span><b>${s.stats?.comments??0}</b></div>
      </div>
      <div class="muted">Perfil: ${esc(s.profile)} · Reconexões: ${s.reconnects} · Monitor: ${s.safety_state}</div>
      ${s.last_error?`<div class="error">${esc(s.last_error)}</div>`:""}
    </div>`;
  }).join(""):`<div class="card muted">Nenhuma LIVE conectada.</div>`;
}

function updateSessionSelects(){
  const html=sessions.map(s=>`<option value="${s.session_id}">@${esc(s.username)}</option>`).join("");
  ["eventSession","rulesSession","rankSession","simSession"].forEach(id=>{
    const el=$(id),old=el.value;el.innerHTML=html;if(old&&sessions.some(s=>s.session_id===old))el.value=old;
  });
}

async function connectLive(usernameOverride){
  const username=(usernameOverride||$("username").value).trim();
  if(!username)return;
  try{
    await request("/api/live/connect",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({username,profile:$("connectProfile").value||"raw"})});
    $("username").value="";await refresh();
  }catch(e){alert(e.message)}
}

async function disconnectLive(id){
  if(!confirm("Desconectar esta LIVE do backend?"))return;
  await request(`/api/live/${id}/disconnect`,{method:"POST"});await refresh();
}

function favorites(){
  try{return JSON.parse(localStorage.getItem("live_favorites")||"[]")}catch{return[]}
}
function saveFavorite(){
  const u=$("username").value.trim().replace(/^@/,"");if(!u)return;
  const rows=[...new Set([...favorites(),u])].slice(0,20);
  localStorage.setItem("live_favorites",JSON.stringify(rows));renderFavorites();
}
function deleteFavorite(u){
  localStorage.setItem("live_favorites",JSON.stringify(favorites().filter(x=>x!==u)));renderFavorites();
}
function renderFavorites(){
  $("favorites").innerHTML=favorites().map(u=>
    `<button class="favorite" onclick="connectLive('${esc(u)}')">@${esc(u)}</button><button class="favorite danger" onclick="deleteFavorite('${esc(u)}')">×</button>`
  ).join("");
}

function isDiagnostic(type){
  return ["bottom_notice","perception","partnership_punish","room_verify","gift_restriction","access_control","gift_prompt","notice","room_notify","system","in_room_banner","toast","access_recall","connection_error"].includes(type);
}

async function loadEvents(){
  const id=$("eventSession").value;if(!id)return;
  try{
    const d=await request(`/api/live/${id}/events?after=0&limit=500`);
    eventCache=d.events||[];renderCachedEvents();
  }catch{}
}

function renderCachedEvents(){
  const filter=$("eventFilter").value;
  let rows=eventCache.slice().reverse();
  if(filter==="diagnostic")rows=rows.filter(e=>isDiagnostic(e.type));
  else if(filter==="simulation")rows=rows.filter(e=>e.source==="simulation");
  else if(filter)rows=rows.filter(e=>e.type===filter);
  $("eventList").innerHTML=rows.map(eventHtml).join("")||`<div class="muted">Sem eventos.</div>`;
}

function ageText(ms){
  if(ms===null||ms===undefined)return "tempo desconhecido";
  const abs=Math.abs(ms);
  if(abs<1000)return `${Math.round(abs)} ms`;
  if(abs<60000)return `${(abs/1000).toFixed(1)} s`;
  if(abs<3600000)return `${Math.floor(abs/60000)} min ${Math.floor((abs%60000)/1000)} s`;
  return `${Math.floor(abs/3600000)} h ${Math.floor((abs%3600000)/60000)} min`;
}

function eventHtml(e){
  const data=e.data||{},u=data.user?.unique_id||"";
  let detail="";
  if(e.type==="comment")detail=data.comment||"";
  else if(e.type==="gift")detail=`${data.gift?.name||"Gift"} ×${data.repeat_count||1} · ${data.gift?.diamond_count||0} moedas cada`;
  else if(e.type==="like")detail=`+${data.count||1}`;
  const actions=(e.actions||[]).map(a=>a.type+(a.name?`:${a.name}`:"")).join(", ");
  const safety=e.safety||null;
  const level=safety?.level||"";
  const levelClass=level?`level-${level.toLowerCase()}`:"";
  const diagnosticMeta=safety?`
    <div class="diagnostic-meta">
      <span class="level-pill ${levelClass}">${esc(level)}</span>
      <span>${esc(safety.freshness||"")}</span>
      <span>confiança: ${esc(safety.confidence||"-")}</span>
      ${safety.age_ms!==null&&safety.age_ms!==undefined?`<span>idade: ${ageText(safety.age_ms)}</span>`:""}
    </div>
    <div class="diagnostic-reason">${esc(safety.reason||"")}</div>`:"";
  const payload=isDiagnostic(e.type)&&e.data?.payload
    ? `<details><summary>Ver payload sanitizado</summary><pre>${esc(JSON.stringify(e.data.payload,null,2))}</pre></details>`
    : "";
  return `<div class="event ${levelClass}">
    <strong>${e.source==="simulation"?"[TESTE] ":""}${esc(e.type)}</strong>
    ${u?` @${esc(u)} · `:""}${esc(detail)}
    <small>${new Date(e.timestamp).toLocaleTimeString()}</small>
    ${diagnosticMeta}
    ${actions?`<div class="actions">Ações: ${esc(actions)}</div>`:""}
    ${payload}
  </div>`;
}

async function loadMonitor(){
  const id=$("eventSession").value||sessions[0]?.session_id;if(!id)return;
  const s=await request(`/api/live/${id}/status`);
  $("monitorState").innerHTML=`
    <p>Conexão: <strong>${esc(s.connection_state)}</strong></p>
    <p>TikTok LIVE: <strong>${s.live?"ativa":"offline"}</strong></p>
    <p>Viewers: <strong>${s.viewers??"-"}</strong></p>
    <p>Automações: <strong>${s.automation_paused?"PAUSADAS":"ativas"}</strong></p>
    <p>Monitor: <strong>${esc(s.safety_state)}</strong></p>
    <p>Último erro: ${esc(s.last_error||"nenhum")}</p>`;
  const st=s.stats||{};
  $("monitorCounts").innerHTML=`
    <div><span>Observação</span><b>${st.diagnostic_observation||0}</b></div>
    <div><span>Alerta</span><b>${st.diagnostic_alert||0}</b></div>
    <div><span>Crítico</span><b>${st.diagnostic_critical||0}</b></div>
    <div><span>Histórico</span><b>${st.diagnostic_historical||0}</b></div>
    <div><span>Suprimidos</span><b>${st.diagnostic_suppressed||0}</b></div>`;
  $("safetyBanner").classList.toggle("hidden",!s.automation_paused);
  $("safetyBanner").textContent=s.automation_paused?"Diagnóstico CRÍTICO confirmado: ações automáticas estão pausadas até revisão manual.":"";
  const d=await request(`/api/live/${id}/events?after=0&limit=500`);
  diagnosticCache=(d.events||[]).filter(e=>isDiagnostic(e.type));
  renderDiagnostics();
}

function renderDiagnostics(){
  const mode=$("monitorFilter")?.value||"relevant";
  let rows=diagnosticCache.slice().reverse();
  if(mode==="relevant")rows=rows.filter(e=>["OBSERVATION","ALERT","CRITICAL"].includes(e.safety?.level));
  else if(mode==="history")rows=rows.filter(e=>["HISTORICAL","INFO"].includes(e.safety?.level));
  $("diagnostics").innerHTML=rows.map(eventHtml).join("")||`<div class="muted">Nenhum sinal neste filtro.</div>`;
}

async function resetSafety(){
  const id=$("eventSession").value||sessions[0]?.session_id;if(!id)return;
  await request(`/api/live/${id}/safety/reset`,{method:"POST"});await refresh();await loadMonitor();
}

async function loadRules(){
  const id=$("rulesSession").value||sessions[0]?.session_id;if(!id)return;
  const s=await request(`/api/live/${id}/status`);
  $("cfgComment").checked=!!s.config.comment_enabled;
  $("cfgLike").checked=!!s.config.like_enabled;
  $("cfgFollow").checked=!!s.config.follow_enabled;
  $("cfgShare").checked=!!s.config.share_enabled;
  $("cfgGift").checked=!!s.config.gift_enabled;
  $("cfgSafety").checked=!!s.config.safety_auto_pause_critical;
  $("rulesJson").value=JSON.stringify(s.rules,null,2);
}

async function saveConfig(){
  const id=$("rulesSession").value;if(!id)return;
  await request(`/api/live/${id}/config`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({config:{
    comment_enabled:$("cfgComment").checked,like_enabled:$("cfgLike").checked,
    follow_enabled:$("cfgFollow").checked,share_enabled:$("cfgShare").checked,
    gift_enabled:$("cfgGift").checked,safety_auto_pause_critical:$("cfgSafety").checked
  }})});
  alert("Configuração salva.");
}

async function applyProfile(name){
  const id=$("rulesSession").value;if(!id)return;
  await request(`/api/live/${id}/profile`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({profile:name})});
  await refresh();await loadRules();
}

function formatRules(){
  try{$("rulesJson").value=JSON.stringify(JSON.parse($("rulesJson").value),null,2);$("rulesError").textContent=""}
  catch(e){$("rulesError").textContent=e.message}
}
async function saveRules(){
  const id=$("rulesSession").value;if(!id)return;
  try{
    const rules=JSON.parse($("rulesJson").value);
    await request(`/api/live/${id}/rules`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({rules})});
    $("rulesError").textContent="";alert("Regras salvas.");
  }catch(e){$("rulesError").textContent=e.message}
}

async function loadRanking(){
  const id=$("rankSession").value||sessions[0]?.session_id;if(!id)return;
  const d=await request(`/api/live/${id}/leaderboard?sort=${encodeURIComponent($("rankSort").value)}&limit=100`);
  $("rankingBody").innerHTML=(d.rows||[]).map((r,i)=>`<tr><td>${i+1}</td><td>@${esc(r.unique_id||r.nickname||"-")}</td><td>${r.coins}</td><td>${r.gifts}</td><td>${r.likes}</td><td>${r.comments}</td><td>${r.shares}</td></tr>`).join("");
}

async function simulate(){
  const id=$("simSession").value;if(!id)return alert("Conecte uma sessão.");
  const body={type:$("simType").value,username:$("simUser").value,gift_name:$("simGift").value,
    gift_coins:+$("simCoins").value,count:+$("simCount").value,comment:$("simComment").value};
  const e=await request(`/api/live/${id}/simulate`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  $("simResult").textContent=`Evento #${e.seq} criado. Ações: ${(e.actions||[]).map(a=>a.type).join(", ")||"nenhuma"}`;
}

function exportJson(){const id=$("eventSession").value;if(id)location.href=`/api/live/${id}/export.json`}
function exportCsv(){const id=$("eventSession").value;if(id)location.href=`/api/live/${id}/export.csv`}

function connectWs(){
  if(ws)try{ws.close()}catch{}
  const proto=location.protocol==="https:"?"wss":"ws";
  ws=new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen=()=>{
    clearInterval(wsPing);
    // Render Free counts inbound WS messages as activity, so the dashboard pings while open.
    wsPing=setInterval(()=>{if(ws?.readyState===1)ws.send("ping")},30000);
  };
  ws.onmessage=ev=>{
    if(ev.data==="pong")return;
    let m;try{m=JSON.parse(ev.data)}catch{return}
    if(m.kind==="event"){
      if(currentTab==="events" && m.event.session_id===$("eventSession").value){
        eventCache.push(m.event);if(eventCache.length>500)eventCache.shift();renderCachedEvents();
      }
      if(currentTab==="monitor"&&isDiagnostic(m.event.type))loadMonitor();
      if(["gift","comment","like","follow","share"].includes(m.event.type))refresh();
    } else if(m.kind==="status") refresh();
  };
  ws.onclose=()=>{clearInterval(wsPing);setTimeout(connectWs,3500)};
}

async function boot(){
  if(!(await authCheck()))return;
  await loadMeta();renderFavorites();await refresh();connectWs();
  setInterval(refresh,5000);
}

boot();

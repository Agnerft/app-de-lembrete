const tokenInput = document.querySelector("#adminToken");
const connectButton = document.querySelector("#connectButton");
const auditPanel = document.querySelector("#auditPanel");
const auditState = document.querySelector("#auditState");
const auditDescription = document.querySelector("#auditDescription");
const toggleAuditButton = document.querySelector("#toggleAudit");
const refreshEventsButton = document.querySelector("#refreshEvents");
const clearEventsButton = document.querySelector("#clearEvents");
const eventList = document.querySelector("#eventList");
const statusMessage = document.querySelector("#statusMessage");
const adminTabs = document.querySelector("#adminTabs");
const contactsPanel = document.querySelector("#contactsPanel");
const contactsStatus = document.querySelector("#contactsStatus");
const officialForm = document.querySelector("#officialForm");
const officialWhatsapp = document.querySelector("#officialWhatsapp");
const syncResellersButton = document.querySelector("#syncResellers");
const resellerSearch = document.querySelector("#resellerSearch");
const resellerCount = document.querySelector("#resellerCount");
const resellerList = document.querySelector("#resellerList");

let enabled = false;
let pollTimer = null;
let resellers = [];

function setStatus(text, isError = false) {
  statusMessage.textContent = text;
  statusMessage.classList.toggle("error", isError);
}

function setContactsStatus(text, isError = false) {
  contactsStatus.textContent = text;
  contactsStatus.classList.toggle("error", isError);
}

async function adminFetch(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Authorization": `Bearer ${tokenInput.value.trim()}`,
      ...(options.body ? {"Content-Type": "application/json"} : {}),
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Requisicao recusada (${response.status}).`);
  return payload;
}

function renderEvents(events) {
  eventList.replaceChildren();
  if (!events.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.className = "empty";
    cell.textContent = "Nenhuma consulta registrada.";
    row.append(cell);
    eventList.append(row);
    return;
  }

  for (const event of events) {
    const row = document.createElement("tr");
    const values = [
      new Date(event.created_at).toLocaleTimeString("pt-BR"),
      event.login,
      event.reseller,
      event.source,
    ];
    for (const value of values) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    eventList.append(row);
  }
}

function renderState(payload) {
  enabled = payload.enabled;
  auditState.textContent = enabled ? "Ativa" : "Desativada";
  auditState.classList.toggle("off", !enabled);
  toggleAuditButton.textContent = enabled ? "Desativar agora" : "Ativar por 30 min";
  if (enabled) {
    const end = new Date(payload.enabled_until * 1000).toLocaleTimeString("pt-BR", {hour: "2-digit", minute: "2-digit"});
    auditDescription.textContent = `Registrando login e revendedor ate ${end}.`;
  } else {
    auditDescription.textContent = "A auditoria esta desativada.";
  }
  renderEvents(payload.events || []);
}

function resellerMatches(reseller, query) {
  const normalized = query.trim().toLocaleLowerCase("pt-BR");
  return !normalized || `${reseller.nome} ${reseller.username}`.toLocaleLowerCase("pt-BR").includes(normalized);
}

function renderResellers() {
  const visible = resellers.filter((reseller) => resellerMatches(reseller, resellerSearch.value));
  resellerList.replaceChildren();
  resellerCount.textContent = `${visible.length} de ${resellers.length}`;

  if (!visible.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = resellers.length ? "Nenhuma revenda encontrada." : "Nenhuma revenda cadastrada no arquivo de logins.";
    resellerList.append(empty);
    return;
  }

  for (const reseller of visible) {
    const row = document.createElement("form");
    row.className = "reseller-row";
    row.dataset.username = reseller.username;

    const identity = document.createElement("div");
    identity.className = "reseller-identity";
    const name = document.createElement("strong");
    name.textContent = reseller.nome;
    const details = document.createElement("span");
    details.textContent = `${reseller.username} · ${reseller.linhas_ativas}/${reseller.linhas} linhas ativas`;
    identity.append(name, details);

    const input = document.createElement("input");
    input.type = "tel";
    input.inputMode = "tel";
    input.autocomplete = "off";
    input.placeholder = "WhatsApp com DDD";
    input.value = reseller.whatsapp;
    input.setAttribute("aria-label", `WhatsApp de ${reseller.nome}`);

    const button = document.createElement("button");
    button.type = "submit";
    button.className = "secondary compact";
    button.textContent = "Salvar";
    row.append(identity, input, button);
    resellerList.append(row);
  }
}

async function loadContacts() {
  const payload = await adminFetch("/api/admin/contatos");
  officialWhatsapp.value = payload.oficial || "";
  resellers = payload.revendas || [];
  renderResellers();
}

async function refresh() {
  try {
    renderState(await adminFetch("/api/admin/auditoria"));
    setStatus("");
  } catch (error) {
    setStatus(error.message, true);
    if (pollTimer) clearInterval(pollTimer);
  }
}

connectButton.addEventListener("click", async () => {
  if (!tokenInput.value.trim()) {
    tokenInput.focus();
    return;
  }
  try {
    const [audit, contacts] = await Promise.all([
      adminFetch("/api/admin/auditoria"),
      adminFetch("/api/admin/contatos"),
    ]);
    renderState(audit);
    officialWhatsapp.value = contacts.oficial || "";
    resellers = contacts.revendas || [];
    renderResellers();
    adminTabs.hidden = false;
    contactsPanel.hidden = false;
    tokenInput.disabled = true;
    connectButton.disabled = true;
    pollTimer = setInterval(refresh, 3000);
  } catch (error) {
    setStatus(error.message, true);
  }
});

adminTabs.addEventListener("click", (event) => {
  const button = event.target.closest("[data-panel]");
  if (!button) return;
  for (const tab of adminTabs.querySelectorAll("[data-panel]")) tab.classList.toggle("active", tab === button);
  contactsPanel.hidden = button.dataset.panel !== "contactsPanel";
  auditPanel.hidden = button.dataset.panel !== "auditPanel";
});

officialForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = officialForm.querySelector("button");
  button.disabled = true;
  try {
    const payload = await adminFetch("/api/admin/contatos/oficial", {
      method: "PUT",
      body: JSON.stringify({whatsapp: officialWhatsapp.value}),
    });
    officialWhatsapp.value = payload.whatsapp;
    setContactsStatus("Numero oficial salvo.");
  } catch (error) {
    setContactsStatus(error.message, true);
  } finally {
    button.disabled = false;
  }
});

resellerList.addEventListener("submit", async (event) => {
  event.preventDefault();
  const row = event.target.closest(".reseller-row");
  if (!row) return;
  const input = row.querySelector("input");
  const button = row.querySelector("button");
  button.disabled = true;
  try {
    const payload = await adminFetch(`/api/admin/contatos/revendas/${encodeURIComponent(row.dataset.username)}`, {
      method: "PUT",
      body: JSON.stringify({whatsapp: input.value}),
    });
    input.value = payload.whatsapp;
    const reseller = resellers.find((item) => item.username === row.dataset.username);
    if (reseller) reseller.whatsapp = payload.whatsapp;
    setContactsStatus(`Contato de ${reseller?.nome || row.dataset.username} salvo.`);
  } catch (error) {
    setContactsStatus(error.message, true);
  } finally {
    button.disabled = false;
  }
});

resellerSearch.addEventListener("input", renderResellers);

syncResellersButton.addEventListener("click", async () => {
  syncResellersButton.disabled = true;
  setContactsStatus("Atualizando a lista de revendas...");
  try {
    const payload = await adminFetch("/api/admin/revendas/sincronizar", {method: "POST"});
    await loadContacts();
    setContactsStatus(`${payload.total} revendas carregadas do arquivo de logins.`);
  } catch (error) {
    setContactsStatus(error.message, true);
  } finally {
    syncResellersButton.disabled = false;
  }
});

toggleAuditButton.addEventListener("click", async () => {
  try {
    const payload = await adminFetch("/api/admin/auditoria", {
      method: "POST",
      body: JSON.stringify({enabled: !enabled}),
    });
    renderState({...payload, events: await adminFetch("/api/admin/auditoria").then(data => data.events)});
    setStatus(enabled ? "Auditoria ativada." : "Auditoria desativada.");
  } catch (error) {
    setStatus(error.message, true);
  }
});

refreshEventsButton.addEventListener("click", refresh);
clearEventsButton.addEventListener("click", async () => {
  try {
    await adminFetch("/api/admin/auditoria/eventos", {method: "DELETE"});
    await refresh();
    setStatus("Eventos removidos.");
  } catch (error) {
    setStatus(error.message, true);
  }
});

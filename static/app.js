const form = document.getElementById('clientForm');
const phoneInput = document.getElementById('telefone');
const submitButton = document.getElementById('submitButton');
const message = document.getElementById('message');
const searchSupportLink = document.getElementById('searchSupportLink');
const resultPanel = document.getElementById('resultPanel');
const clientLogin = document.getElementById('clientLogin');
const clientPassword = document.getElementById('clientPassword');
const dueDate = document.getElementById('dueDate');
const dueStatus = document.getElementById('dueStatus');
const screens = document.getElementById('screens');
const plan = document.getElementById('plan');
const paymentLink = document.getElementById('paymentLink');
const copyPaymentLink = document.getElementById('copyPaymentLink');
const togglePasswordButton = document.getElementById('togglePassword');
const noLink = document.getElementById('noLink');
const installButton = document.getElementById('installButton');
const installDialog = document.getElementById('installDialog');
const installSteps = document.getElementById('installSteps');
const reminderButton = document.getElementById('reminderButton');
const reminderDayInputs = [...document.querySelectorAll('input[name="reminderDay"]')];
const supportLink = document.getElementById('supportLink');
const appSlots = document.getElementById('appSlots');
const screensSummary = document.getElementById('screensSummary');
const appTip = document.getElementById('appTip');
const copyStatus = document.getElementById('copyStatus');
const likeButton = document.getElementById('likeButton');
const communityUsers = document.getElementById('communityUsers');
const communityUsersLabel = document.getElementById('communityUsersLabel');
const communityLikes = document.getElementById('communityLikes');
const communityLikesLabel = document.getElementById('communityLikesLabel');
const communityStatus = document.getElementById('communityStatus');

const APP_OPTIONS = ['HD Player', 'Max Player', 'Clouddy', 'Blessed Player', 'Zynx', 'Noxa', 'Vertu Play'];
const APP_CODES = {
  'HD Player': ['700', '789', '889', '9999', '333'],
  'Blessed Player': ['789'],
  'Vertu Play': ['789'],
};

let deferredInstallPrompt = null;
let lastCliente = null;
let lastAccessToken = null;
let communityLiked = false;

function getCommunityDeviceId() {
  const storageKey = 'mega-app-community-device-id';
  try {
    const saved = window.localStorage.getItem(storageKey);
    if (saved) return saved;
    const created = window.crypto?.randomUUID?.()
      || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-device`;
    window.localStorage.setItem(storageKey, created);
    return created;
  } catch (_error) {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-session`;
  }
}

const communityDeviceId = getCommunityDeviceId();
const communityNumber = new Intl.NumberFormat('pt-BR');

function renderCommunity(data) {
  const users = Number(data.users) || 0;
  const likes = Number(data.likes) || 0;
  communityLiked = Boolean(data.liked);
  communityUsers.textContent = communityNumber.format(users);
  communityLikes.textContent = communityNumber.format(likes);
  communityUsersLabel.textContent = users === 1 ? 'pessoa já usa o Mega App' : 'pessoas já usam o Mega App';
  communityLikesLabel.textContent = likes === 1 ? 'curtida' : 'curtidas';
  likeButton.classList.toggle('active', communityLiked);
  likeButton.setAttribute('aria-pressed', String(communityLiked));
  likeButton.querySelector('span').textContent = communityLiked ? 'Curtido' : 'Curtir';
}

async function registerCommunityVisit() {
  try {
    const response = await fetch('/api/comunidade/visita', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_id: communityDeviceId }),
    });
    if (!response.ok) throw new Error('Não foi possível carregar a comunidade.');
    renderCommunity(await response.json());
  } catch (_error) {
    document.querySelector('.community-panel').classList.add('community-unavailable');
  }
}

likeButton.addEventListener('click', async () => {
  const nextLiked = !communityLiked;
  likeButton.disabled = true;
  communityStatus.textContent = '';
  try {
    const response = await fetch('/api/comunidade/curtida', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_id: communityDeviceId, liked: nextLiked }),
    });
    if (!response.ok) throw new Error('Não foi possível salvar sua curtida.');
    renderCommunity(await response.json());
    communityStatus.textContent = nextLiked ? 'Você curtiu o Mega App.' : 'Curtida removida.';
  } catch (error) {
    communityStatus.textContent = error.message;
  } finally {
    likeButton.disabled = false;
  }
});

registerCommunityVisit();

function setMessage(text, isError = true) {
  message.textContent = text;
  if (!text) {
    message.className = 'message';
    return;
  }
  const kind = isError === false ? 'success' : isError === 'warning' ? 'warning' : 'error';
  message.className = `message ${kind}`;
}

function setLoading(loading) {
  submitButton.disabled = loading;
  submitButton.classList.toggle('loading', loading);
  submitButton.querySelector('.button-label').textContent = loading ? 'Consultando' : 'Pesquisar';
}

function setSearchSupport(support) {
  if (support?.url) {
    searchSupportLink.href = support.url;
    searchSupportLink.hidden = false;
  } else {
    searchSupportLink.href = '#';
    searchSupportLink.hidden = true;
  }
}

function setCopyPaymentLabel(text) {
  copyPaymentLink.innerHTML = `
    <span>${text}</span>
    <svg aria-hidden="true" viewBox="0 0 24 24"><rect x="9" y="9" width="10" height="10" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v1"/></svg>
  `;
}

function setPasswordVisible(visible) {
  clientPassword.textContent = visible ? (lastCliente?.senha || 'N/A') : '••••••••';
  clientPassword.setAttribute('aria-label', visible ? 'Senha visível' : 'Senha oculta');
  togglePasswordButton.setAttribute('aria-pressed', String(visible));
  togglePasswordButton.setAttribute('aria-label', visible ? 'Ocultar senha' : 'Mostrar senha');
}

function showCopyFeedback(button, label) {
  const feedback = button.dataset.copyFeedback || `${label} copiado`;
  const originalLabel = button.getAttribute('aria-label') || `Copiar ${label.toLowerCase()}`;
  button.classList.add('copied');
  button.dataset.feedback = feedback;
  button.setAttribute('aria-label', feedback);
  copyStatus.textContent = '';
  window.requestAnimationFrame(() => {
    copyStatus.textContent = feedback;
  });
  window.setTimeout(() => {
    button.classList.remove('copied');
    delete button.dataset.feedback;
    button.setAttribute('aria-label', originalLabel);
  }, 1800);
}

function clientScreenCount(value) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? Math.min(3, Math.max(1, parsed)) : 1;
}

function savedAppsBySlot(preference) {
  const saved = new Map();
  if (Array.isArray(preference?.apps)) {
    preference.apps.forEach((item) => saved.set(Number(item.slot), item.app_usado || ''));
  } else if (preference?.app_usado) {
    saved.set(1, preference.app_usado);
  }
  return saved;
}

function updateAppExtras(slotElement, appName) {
  const credentials = slotElement.querySelector('.clouddy-credentials');
  const unavailable = slotElement.querySelector('.clouddy-unavailable');
  const codesPanel = slotElement.querySelector('.app-codes');
  const codesList = slotElement.querySelector('[data-app-codes]');
  const showClouddy = appName === 'Clouddy';
  const hasCredentials = Boolean(lastCliente?.clouddy_acesso?.email && lastCliente?.clouddy_acesso?.senha);
  const appCodes = APP_CODES[appName] || [];
  credentials.hidden = !showClouddy || !hasCredentials;
  unavailable.hidden = !showClouddy || hasCredentials;
  codesPanel.hidden = appCodes.length === 0;
  codesList.replaceChildren(...appCodes.map((code) => {
    const item = document.createElement('span');
    item.className = 'app-code-chip';
    item.textContent = code;
    return item;
  }));

  if (hasCredentials) {
    credentials.querySelector('[data-clouddy-email]').textContent = lastCliente.clouddy_acesso.email;
    credentials.querySelector('[data-clouddy-password]').textContent = lastCliente.clouddy_acesso.senha;
  }
}

function createAppSlot(slot, selectedApp) {
  const slotElement = document.createElement('section');
  slotElement.className = 'app-slot';
  slotElement.dataset.slot = String(slot);

  const options = [
    '<option value="">Selecionar app</option>',
    ...APP_OPTIONS.map((appName) => `<option value="${appName}">${appName}</option>`),
  ].join('');

  slotElement.innerHTML = `
    <div class="app-slot-head">
      <label for="appUsed${slot}">Tela ${slot}</label>
      <span class="save-state" data-save-state aria-live="polite"></span>
    </div>
    <select id="appUsed${slot}" data-app-slot-select data-slot="${slot}">${options}</select>
    <div class="clouddy-credentials" hidden>
      <span class="clouddy-title">Acesso Clouddy</span>
      <div class="credential-line">
        <span><small>E-mail</small><strong data-clouddy-email id="clouddyEmail${slot}"></strong></span>
        <button class="copy-button" type="button" data-copy-target="clouddyEmail${slot}" aria-label="Copiar e-mail do Clouddy">
          <svg aria-hidden="true" viewBox="0 0 24 24"><rect x="9" y="9" width="10" height="10" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v1"/></svg>
        </button>
      </div>
      <div class="credential-line">
        <span><small>Senha</small><strong data-clouddy-password id="clouddyPassword${slot}"></strong></span>
        <button class="copy-button" type="button" data-copy-target="clouddyPassword${slot}" aria-label="Copiar senha do Clouddy">
          <svg aria-hidden="true" viewBox="0 0 24 24"><rect x="9" y="9" width="10" height="10" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2 2v1"/></svg>
        </button>
      </div>
    </div>
    <p class="clouddy-unavailable" hidden>ID do cliente não disponível para gerar o acesso Clouddy.</p>
    <div class="app-codes" hidden>
      <span class="app-codes-title">Códigos disponíveis</span>
      <div class="app-code-list" data-app-codes></div>
    </div>
  `;

  const select = slotElement.querySelector('select');
  select.value = selectedApp || '';
  updateAppExtras(slotElement, select.value);
  return slotElement;
}

function renderAppSlots(cliente) {
  const total = clientScreenCount(cliente.telas);
  const savedApps = savedAppsBySlot(cliente.app_preferencia);
  screensSummary.textContent = `${total} ${total === 1 ? 'tela' : 'telas'}`;
  appTip.textContent = total === 1
    ? 'Escolha o aplicativo usado nesta tela.'
    : `Escolha o aplicativo usado em cada uma das ${total} telas.`;
  appSlots.replaceChildren();
  for (let slot = 1; slot <= total; slot += 1) {
    appSlots.appendChild(createAppSlot(slot, savedApps.get(slot) || ''));
  }
}

async function copyText(text) {
  if (!text) return false;

  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return true;
  }

  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  textarea.style.top = '0';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();

  let copied = false;
  try {
    copied = document.execCommand('copy');
  } finally {
    document.body.removeChild(textarea);
  }

  if (!copied) {
    throw new Error('Não foi possível copiar automaticamente.');
  }
  return true;
}

function renderResult(cliente) {
  lastCliente = cliente;
  clientLogin.textContent = cliente.login || 'N/A';
  setPasswordVisible(false);
  dueDate.textContent = cliente.vencimento || 'N/A';
  screens.textContent = cliente.telas || 'N/A';
  plan.textContent = cliente.plano || 'N/A';
  renderAppSlots(cliente);
  const savedReminderDays = Array.isArray(cliente.lembrete_dias)
    ? cliente.lembrete_dias.map(Number)
    : [3, 2, 1, 0];
  reminderDayInputs.forEach((input) => {
    input.checked = savedReminderDays.includes(Number(input.value));
  });

  dueStatus.textContent = cliente.status_vencimento?.label || 'Sem data';
  dueStatus.className = `status-pill ${cliente.status_vencimento?.kind || 'neutral'}`;

  if (cliente.link_pagamento) {
    paymentLink.href = cliente.link_pagamento;
    paymentLink.hidden = false;
    copyPaymentLink.hidden = false;
    noLink.hidden = true;
  } else {
    paymentLink.href = '#';
    paymentLink.hidden = true;
    copyPaymentLink.hidden = true;
    noLink.hidden = false;
  }

  if (cliente.suporte?.url) {
    supportLink.href = cliente.suporte.url;
    supportLink.hidden = false;
  } else {
    supportLink.href = '#';
    supportLink.hidden = true;
  }

  resultPanel.hidden = false;
  resultPanel.classList.remove('is-visible');
  window.requestAnimationFrame(() => {
    resultPanel.classList.add('is-visible');
    if (window.matchMedia('(max-width: 860px)').matches) {
      resultPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  });
  reminderButton.classList.remove('active');
  setReminderButtonLabel('Ativar lembretes');
}

appSlots.addEventListener('change', async (event) => {
  const select = event.target.closest('[data-app-slot-select]');
  if (!select) return;
  if (!lastCliente) {
    setMessage('Pesquise o telefone antes de salvar o app.', 'warning');
    return;
  }

  const slotElement = select.closest('.app-slot');
  const saveState = slotElement.querySelector('[data-save-state]');
  const selectedApp = select.value;
  const slot = Number(select.dataset.slot);
  updateAppExtras(slotElement, selectedApp);
  saveState.textContent = 'Salvando...';
  saveState.className = 'save-state saving';
  try {
    const response = await fetch('/api/app-preferencia', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        telefone: lastCliente.telefone,
        login: lastCliente.login,
        app_usado: selectedApp,
        access_token: lastAccessToken,
        slot,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.detail || 'Não foi possível salvar o app.');
    }
    const apps = Array.isArray(lastCliente.app_preferencia?.apps)
      ? [...lastCliente.app_preferencia.apps]
      : [];
    const otherApps = apps.filter((item) => Number(item.slot) !== slot);
    if (selectedApp) otherApps.push(data.preferencia);
    lastCliente.app_preferencia = { apps: otherApps };
    saveState.textContent = selectedApp ? 'Salvo' : 'Removido';
    saveState.className = 'save-state saved';
    setMessage(`Aplicativo da tela ${slot} ${selectedApp ? 'salvo' : 'removido'}.`, false);
  } catch (error) {
    saveState.textContent = 'Erro';
    saveState.className = 'save-state error';
    setMessage(error.message || 'Não foi possível salvar o app.');
  }
});

togglePasswordButton.addEventListener('click', () => {
  const isVisible = togglePasswordButton.getAttribute('aria-pressed') === 'true';
  setPasswordVisible(!isVisible);
});

document.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-copy-target], [data-copy-secret]');
    if (!button) return;
    const target = button.dataset.copyTarget ? document.getElementById(button.dataset.copyTarget) : null;
    const text = button.dataset.copySecret === 'password'
      ? lastCliente?.senha
      : target?.textContent?.trim();
    if (!text || text === 'N/A') return;

    try {
      await copyText(text);
      const rawLabel = button.dataset.copyLabel
        || (button.getAttribute('aria-label') || 'Copiar conteúdo').replace(/^Copiar /i, '');
      const label = rawLabel.charAt(0).toUpperCase() + rawLabel.slice(1);
      showCopyFeedback(button, label);
    } catch {
      setMessage('Não foi possível copiar automaticamente.');
    }
});

copyPaymentLink.addEventListener('click', async () => {
  const href = paymentLink.href;
  if (!href || href.endsWith('#')) return;

  copyPaymentLink.disabled = true;
  const originalText = copyPaymentLink.dataset.defaultLabel || 'Copiar Pix';
  setCopyPaymentLabel('Buscando Pix...');

  try {
    const response = await fetch('/api/pix', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ link: href }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.pix) {
      throw new Error(data.detail || 'Não foi possível encontrar o Pix.');
    }

    await copyText(data.pix);
    setMessage('Código Pix copiado.', false);
  } catch (error) {
    try {
      await copyText(href);
      setMessage('Não consegui obter o Pix agora. Copiei o link de pagamento.', false);
    } catch {
      setMessage(error.message || `Copie o link: ${href}`);
    }
  } finally {
    copyPaymentLink.disabled = false;
    setCopyPaymentLabel(originalText);
  }
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  setMessage('');
  setSearchSupport(null);

  const phoneDigits = phoneInput.value.replace(/\D/g, '');
  const nationalDigits = phoneDigits.startsWith('55') && phoneDigits.length > 11
    ? phoneDigits.slice(2)
    : phoneDigits;
  if (nationalDigits.length < 10) {
    resultPanel.hidden = true;
    setMessage('Por favor, digite o telefone com o DDD.', 'warning');
    return;
  }

  setLoading(true);
  lastCliente = null;
  lastAccessToken = null;

  try {
    const response = await fetch('/api/cliente', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ telefone: phoneInput.value }),
    });

    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 404) {
        setSearchSupport(data.suporte);
        throw new Error(data.detail || 'Não encontrei seus dados. Por favor, fale com o suporte.');
      }
      throw new Error(data.detail || 'Não foi possível consultar agora.');
    }

    lastAccessToken = data.access_token || null;
    renderResult(data.cliente);
    setMessage('');
  } catch (error) {
    resultPanel.hidden = true;
    resultPanel.classList.remove('is-visible');
    setMessage(error.message || 'Erro inesperado.');
  } finally {
    setLoading(false);
  }
});

window.addEventListener('beforeinstallprompt', (event) => {
  event.preventDefault();
  deferredInstallPrompt = event;
  updateInstallState();
});

installButton.addEventListener('click', async () => {
  if (isStandaloneApp()) {
    updateInstallState();
    return;
  }

  if (deferredInstallPrompt) {
    deferredInstallPrompt.prompt();
    const choice = await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
    if (choice.outcome === 'accepted') {
      setMessage('Instalação iniciada.', false);
      updateInstallState(true);
    }
    return;
  }

  showInstallHelp();
});

window.addEventListener('appinstalled', () => {
  deferredInstallPrompt = null;
  updateInstallState(true);
});

reminderButton.addEventListener('click', async () => {
  if (!lastCliente) {
    setMessage('Pesquise seu telefone antes de ativar lembretes.');
    return;
  }
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    setMessage('Este navegador não suporta notificações do app.');
    return;
  }
  const reminderDays = reminderDayInputs
    .filter((input) => input.checked)
    .map((input) => Number(input.value));
  if (!reminderDays.length) {
    setMessage('Escolha pelo menos um momento para receber o lembrete.', 'warning');
    return;
  }

  try {
    const config = await fetch('/api/notificacoes/config').then((response) => response.json());
    if (!config.enabled || !config.public_key) {
      throw new Error('As notificações ainda não estão configuradas no servidor.');
    }

    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      throw new Error('A permissão para notificações não foi liberada.');
    }

    const registration = await navigator.serviceWorker.ready;
    let subscription = await registration.pushManager.getSubscription();
    if (subscription) {
      await subscription.unsubscribe();
    }
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(config.public_key),
    });

    const payload = {
      telefone: phoneInput.value,
      cliente: lastCliente,
      subscription: subscription.toJSON(),
      access_token: lastAccessToken,
      reminder_days: reminderDays,
    };
    const response = await fetch('/api/notificacoes/inscrever', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.detail || 'Não foi possível ativar os lembretes.');
    }

    reminderButton.classList.add('active');
    setReminderButtonLabel('Lembretes ativados');
    const scheduleLabels = reminderDays.map((day) => {
      if (day === 0) return 'no dia';
      return day === 1 ? '1 dia antes' : `${day} dias antes`;
    });
    const scheduleText = scheduleLabels.length > 1
      ? `${scheduleLabels.slice(0, -1).join(', ')} e ${scheduleLabels.at(-1)}`
      : scheduleLabels[0];
    setMessage(
      data.teste_enviado
        ? `Lembretes ativados para ${scheduleText}. Enviamos uma notificação de teste.`
        : `Lembretes ativados para ${scheduleText}. O teste não foi enviado, mas os avisos continuam programados.`,
      data.teste_enviado ? false : 'warning',
    );
  } catch (error) {
    const text = error.message || 'Não foi possível ativar as notificações.';
    if (text.toLowerCase().includes('permission denied') || text.toLowerCase().includes('registration failed')) {
      setMessage('O navegador recusou o registro. Abra no Chrome/Safari, instale o app e tente ativar os lembretes de novo.');
    } else {
      setMessage(text);
    }
  }
});

reminderDayInputs.forEach((input) => {
  input.addEventListener('change', () => {
    if (reminderButton.classList.contains('active')) {
      reminderButton.classList.remove('active');
      setReminderButtonLabel('Salvar lembretes');
    }
  });
});

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}

updateInstallState();

function showInstallHelp() {
  const ua = navigator.userAgent.toLowerCase();
  const isIOS = /iphone|ipad|ipod/.test(ua);
  const isAndroid = /android/.test(ua);

  if (isIOS) {
    installSteps.innerHTML = `
      <p>1. Abra este link no Safari.</p>
      <p>2. Toque no botao Compartilhar.</p>
      <p>3. Escolha "Adicionar a Tela de Inicio".</p>
    `;
  } else if (isAndroid) {
    installSteps.innerHTML = `
      <p>1. Abra este link no Chrome.</p>
      <p>2. Toque nos três pontinhos do navegador.</p>
      <p>3. Escolha "Instalar app" ou "Adicionar a tela inicial".</p>
    `;
  } else {
    installSteps.innerHTML = `
      <p>Se o navegador não abriu a instalação automaticamente, use o ícone de instalar na barra de endereço.</p>
      <p>No Chrome ou Edge, ele costuma aparecer como um computador ou uma seta ao lado do endereço do site.</p>
    `;
  }

  if (typeof installDialog.showModal === 'function') {
    installDialog.showModal();
  } else {
    alert(installSteps.textContent.trim());
  }
}

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; i += 1) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

function setReminderButtonLabel(text) {
  reminderButton.innerHTML = `
    <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M18 8a6 6 0 1 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
    ${text}
  `;
}

function isStandaloneApp() {
  return window.matchMedia?.('(display-mode: standalone)').matches || window.navigator.standalone === true;
}

function updateInstallState(forceInstalled = false) {
  const installed = forceInstalled || isStandaloneApp();
  document.body.classList.toggle('pwa-mode', installed);
  installButton.hidden = installed;
  installButton.setAttribute('aria-hidden', String(installed));
}

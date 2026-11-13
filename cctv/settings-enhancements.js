(() => {
  const systemView = document.querySelector('#view-system');
  const systemShell = systemView?.querySelector('.system-settings-shell');
  const oldNvrView = document.querySelector('#view-nvr');
  const oldNvrNav = document.querySelector('[data-view="nvr"]');
  if (!systemView || !systemShell) return;

  const tabs = systemShell.querySelector('.system-settings-tabs');
  if (!tabs || document.querySelector('#system-tab-nvr')) return;

  tabs.insertAdjacentHTML(
    'beforeend',
    '<button type="button" role="tab" id="system-tab-nvr" aria-selected="false" aria-controls="system-panel-nvr" data-system-tab="nvr">NVR</button>'
  );
  systemShell.insertAdjacentHTML(
    'beforeend',
    '<section class="system-settings-section" id="system-panel-nvr" role="tabpanel" aria-labelledby="system-tab-nvr" data-system-tab-panel="nvr"><div class="panel-head"><div><h2>NVR recordings</h2><span class="notice">Manage retention, storage, segment length, and per-camera capture.</span></div><button class="ghost" type="button" onclick="loadNvr()">Refresh</button></div><div id="system-nvr-settings-form" class="nvr-settings"></div></section>'
  );

  const form = document.querySelector('#nvr-settings-form');
  const target = document.querySelector('#system-nvr-settings-form');
  if (form && target) target.appendChild(form);

  if (oldNvrNav) oldNvrNav.hidden = true;
  if (oldNvrView) oldNvrView.hidden = true;

  if (Array.isArray(window.validSystemTabs)) window.validSystemTabs.push('nvr');

  const originalSelectSystemTab = window.selectSystemTab;
  if (typeof originalSelectSystemTab === 'function') {
    window.selectSystemTab = function selectSystemTabWithNvr(name, save = true) {
      const result = originalSelectSystemTab(name, save);
      if (name === 'nvr') window.loadNvr?.();
      return result;
    };
  }

  const nvrTab = document.querySelector('#system-tab-nvr');
  nvrTab?.addEventListener('click', () => {
    window.selectSystemTab?.('nvr');
    window.loadNvr?.();
  });

  const currentView = localStorage.getItem('cctvView');
  const currentTab = localStorage.getItem('cctvSystemTab');
  if (currentView === 'nvr') {
    window.selectView?.('system');
    window.selectSystemTab?.('nvr');
  } else if (currentView === 'system' && currentTab === 'nvr') {
    window.selectSystemTab?.('nvr');
  }
})();

(() => {
  const originalDiagnosticSummaryMarkup = window.diagnosticSummaryMarkup;
  if (typeof originalDiagnosticSummaryMarkup !== 'function') return;
  const dashboardHosted = new Set(['mediamtx', 'pi-cctv-recorder']);
  window.diagnosticSummaryMarkup = function diagnosticSummaryMarkupWithServiceScope(report) {
    const services = Array.isArray(report?.services) ? report.services.map(service => {
      if (!dashboardHosted.has(service.service)) return service;
      return {...service, active: true, state: 'dashboard host', service: `${service.service} · dashboard host`};
    }) : report?.services;
    return originalDiagnosticSummaryMarkup({...report, services});
  };
})();

(() => {
  const style = document.createElement('style');
  style.textContent = '.device-identity-card{grid-column:1/-1!important;padding:24px 26px!important}.device-identity-card>.panel-head{margin-bottom:18px}.device-identity-card>.panel-head h2{margin-bottom:5px}.device-identity-columns{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.device-identity-group{display:grid;align-content:start;gap:12px;margin:0;padding:17px;border:1px solid var(--line);border-radius:13px;background:var(--bg)}.device-identity-group h3{margin:0;font-size:.95rem}.device-identity-group>p{margin:-5px 0 2px;color:var(--muted);font-size:.78rem;line-height:1.45}.device-identity-group label{display:grid;gap:7px;color:var(--muted);font-size:.78rem;font-weight:700}.device-identity-group input{width:100%;box-sizing:border-box}.device-identity-actions{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.device-identity-actions .notice{min-height:1.2em}@media(max-width:700px){.device-identity-columns{grid-template-columns:1fr}}';
  document.head.appendChild(style);

  function hostnameFor(device) {
    return deviceMetadata(device).hostname || device.id || '';
  }

  function ensureDeviceHostnameEditor(id) {
    const device = state.devices.find(item => item.id === id);
    const page = document.querySelector('#device-detail-page');
    if (!device || !page || authUi.user?.role !== 'admin') return;
    if (page.querySelector('#device-hostname-form')) return;
    let profile = page.querySelector('#device-profile-form');
    if (!profile) {
      const grid = page.querySelector('.device-grid');
      if (!grid) return;
      grid.insertAdjacentHTML('afterbegin', `<section class="device-card device-identity-card"><div class="panel-head"><div><h2>Device identity</h2><span class="notice">Use one name for this Pi in the dashboard and on your network.</span></div></div><div class="device-identity-columns"><form id="device-profile-form" class="device-identity-group" onsubmit="saveDeviceIdentity(event, '${esc(id)}')"><h3>Device name</h3><p>This becomes the dashboard name and Pi hostname.</p><label>Name<input id="device-profile-name" maxlength="63" pattern="[a-zA-Z0-9][a-zA-Z0-9-]{0,61}[a-zA-Z0-9]" required value="${esc(hostnameFor(device))}"></label><label>Location<input id="device-profile-location" maxlength="120" placeholder="Optional" value="${esc(device.location || '')}"></label><div class="device-identity-actions"><button class="primary" type="submit">Save device identity</button><span id="device-profile-status" class="notice" role="status" aria-live="polite"></span></div></form></div></section>`);
      return;
    }
    if (!profile) return;
    const name = profile.querySelector('#device-profile-name');
    if (name) { name.value = hostnameFor(device); name.maxLength = 63; name.pattern = '[a-zA-Z0-9][a-zA-Z0-9-]{0,61}[a-zA-Z0-9]'; }
    profile.onsubmit = event => saveDeviceIdentity(event, id);
  }

  window.saveDeviceIdentity = async function saveDeviceIdentity(event, id) {
    event.preventDefault();
    const input = document.querySelector('#device-profile-name');
    const status = document.querySelector('#device-profile-status');
    const hostname = input?.value.trim().toLowerCase() || '';
    if (!hostname || !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(hostname)) {
      if (status) status.textContent = 'Use 1–63 lowercase letters, numbers, or hyphens.';
      return;
    }
    const location = document.querySelector('#device-profile-location')?.value || '';
    if (!confirm(`Set this Pi's dashboard name and hostname to “${hostname}”?`)) return;
    const button = event.submitter;
    if (button) button.disabled = true;
    if (status) status.textContent = 'Saving device identity…';
    try {
      const profileResponse = await api(`/api/devices/${encodeURIComponent(id)}/profile`, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:hostname, location})});
      const profileResult = await profileResponse.json().catch(() => ({}));
      if (!profileResponse.ok) throw new Error(profileResult.error || 'Could not save the device identity.');
      const hostnameResponse = await api(`/api/devices/${encodeURIComponent(id)}/commands`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({command:'set_hostname', hostname})});
      const hostnameResult = await hostnameResponse.json().catch(() => ({}));
      if (!hostnameResponse.ok) throw new Error(hostnameResult.error || 'The dashboard name was saved, but the Pi hostname could not be queued.');
      const index = state.devices.findIndex(device => device.id === id);
      if (index >= 0) state.devices[index] = profileResult;
      renderDevices();
      const title = document.querySelector('#device-detail-page .device-detail > .panel .panel-head h2');
      if (title) title.textContent = profileResult.name;
      const locationLabel = [...document.querySelectorAll('#device-detail-page dl dt')].find(term => term.textContent === 'Location');
      if (locationLabel) locationLabel.nextElementSibling.textContent = profileResult.location || '—';
      if (status) status.textContent = 'Saved. The Pi hostname will update on its next heartbeat.';
    } catch (error) {
      if (status) status.textContent = error.message || 'Could not save the device identity.';
    } finally {
      if (button) button.disabled = false;
    }
  };

  const originalOpenDevice = window.openDevice;
  if (typeof originalOpenDevice === 'function') {
    window.openDevice = async function openDeviceWithHostnameEditor(id) {
      localStorage.setItem('cctvDeviceId', id);
      await originalOpenDevice(id);
      history.replaceState(null, '', `${location.pathname}#device/${encodeURIComponent(id)}`);
      ensureDeviceHostnameEditor(id);
    };
  }

  const originalLoadAll = window.loadAll;
  if (typeof originalLoadAll === 'function') {
    window.loadAll = async function loadAllWithDeviceRestore() {
      await originalLoadAll();
      const match = location.hash.match(/^#device\/(.+)$/);
      const savedId = match ? decodeURIComponent(match[1]) : localStorage.getItem('cctvDeviceId');
      const device = savedId && state.devices.find(item => item.id === savedId);
      if (device) {
        const detail = document.querySelector('#device-detail-page');
        if (!document.querySelector('#view-device-detail.active') || !detail?.children.length) await window.openDevice(device.id);
      } else if (localStorage.getItem('cctvView') === 'device-detail' || location.hash === '#device-detail') {
        localStorage.removeItem('cctvDeviceId');
        history.replaceState(null, '', `${location.pathname}#devices`);
        window.selectView?.('devices');
      }
    };
  }
})();

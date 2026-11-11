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
  style.textContent = '.device-identity-card{grid-column:1/-1!important;padding:24px 26px!important}.device-identity-card>.panel-head{margin-bottom:18px}.device-identity-card>.panel-head h2{margin-bottom:5px}.device-identity-columns{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.device-identity-group{display:grid;align-content:start;gap:12px;margin:0;padding:17px;border:1px solid var(--line);border-radius:13px;background:var(--bg)}.device-identity-group h3{margin:0;font-size:.95rem}.device-identity-group>p{margin:-5px 0 2px;color:var(--muted);font-size:.78rem;line-height:1.45}.device-identity-group label,.device-hostname-form label{display:grid;gap:7px;color:var(--muted);font-size:.78rem;font-weight:700}.device-identity-group input,.device-hostname-form input{width:100%;box-sizing:border-box}.device-identity-actions,.device-hostname-actions{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.device-identity-actions .notice,.device-hostname-actions .notice{min-height:1.2em}.device-hostname-form{display:grid;gap:12px;margin:0;padding:0;border:0}.device-hostname-form>.notice{margin:0;color:var(--muted);font-size:.78rem;line-height:1.45}.device-identity-columns>.device-hostname-form{display:grid}.device-identity-columns>.device-hostname-form:before{content:"Pi network identity";display:block;margin-bottom:-4px;font-size:.95rem;font-weight:750}.device-identity-columns>.device-hostname-form:after{content:"The hostname used to identify this Raspberry Pi on your network.";display:block;grid-row:2;color:var(--muted);font-size:.78rem;line-height:1.45}.device-identity-columns>.device-hostname-form>.notice{display:none}.device-identity-columns>.device-hostname-form .device-hostname-actions{grid-row:5}.device-identity-columns>.device-hostname-form label{grid-row:3}.device-identity-columns>.device-hostname-form .device-hostname-actions{grid-row:4}.device-identity-columns>.device-hostname-form:after{grid-row:2}.device-identity-columns>.device-hostname-form{grid-template-rows:auto auto auto auto}.device-identity-columns>.device-hostname-form .device-hostname-actions{grid-column:1}.device-identity-columns>.device-hostname-form:after{grid-column:1}.device-identity-columns>.device-hostname-form label{grid-column:1}.device-identity-columns>.device-hostname-form .device-hostname-actions .notice{display:block}@media(max-width:700px){.device-identity-columns{grid-template-columns:1fr}}';
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
      grid.insertAdjacentHTML('afterbegin', `<section class="device-card device-identity-card"><div class="panel-head"><div><h2>Device identity</h2><span class="notice">Manage the labels used to recognize this Pi in the dashboard and on your network.</span></div></div><div class="device-identity-columns"><form id="device-profile-form" class="device-identity-group" onsubmit="saveDeviceProfile(event, '${esc(id)}')"><h3>Dashboard identity</h3><p>Name and location shown throughout Pi CCTV.</p><label>Dashboard name<input id="device-profile-name" maxlength="80" required value="${esc(device.name || '')}"></label><label>Location<input id="device-profile-location" maxlength="120" placeholder="Optional" value="${esc(device.location || '')}"></label><div class="device-identity-actions"><button class="primary" type="submit">Save dashboard details</button><span id="device-profile-status" class="notice" role="status" aria-live="polite"></span></div></form><form id="device-hostname-form" class="device-identity-group device-hostname-form" onsubmit="saveDeviceHostname(event, '${esc(id)}')"><label>Pi hostname<input id="device-hostname" maxlength="63" pattern="[a-zA-Z0-9][a-zA-Z0-9-]{0,61}[a-zA-Z0-9]" required value="${esc(hostnameFor(device))}"></label><div class="device-hostname-actions"><button class="ghost" type="submit">Change Pi hostname</button><span id="device-hostname-status" class="notice" role="status" aria-live="polite"></span></div></form></div></section>`);
      return;
      profile = page.querySelector('#device-profile-form');
    }
    if (!profile) return;
    profile.insertAdjacentHTML('afterend', `<form id="device-hostname-form" class="device-hostname-form" onsubmit="saveDeviceHostname(event, '${esc(id)}')"><label>Pi hostname<input id="device-hostname" maxlength="63" pattern="[a-zA-Z0-9][a-zA-Z0-9-]{0,61}[a-zA-Z0-9]" required value="${esc(hostnameFor(device))}"></label><span class="notice">Changes the Raspberry Pi network hostname. The dashboard device ID stays unchanged.</span><div class="device-hostname-actions"><button class="ghost" type="submit">Change Pi hostname</button><span id="device-hostname-status" class="notice" role="status" aria-live="polite"></span></div></form>`);
  }

  window.saveDeviceHostname = async function saveDeviceHostname(event, id) {
    event.preventDefault();
    const input = document.querySelector('#device-hostname');
    const status = document.querySelector('#device-hostname-status');
    const hostname = input?.value.trim().toLowerCase() || '';
    if (!hostname || !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(hostname)) {
      if (status) status.textContent = 'Use 1–63 lowercase letters, numbers, or hyphens.';
      return;
    }
    if (!confirm(`Change this Pi hostname to “${hostname}”? The Pi will keep its current dashboard device ID.`)) return;
    const button = event.submitter;
    if (button) button.disabled = true;
    if (status) status.textContent = 'Queueing hostname change…';
    try {
      const response = await api(`/api/devices/${encodeURIComponent(id)}/commands`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({command:'set_hostname', hostname})});
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || 'Could not change the Pi hostname.');
      if (status) status.textContent = 'Hostname change queued. It will appear after the Pi reports its next heartbeat.';
    } catch (error) {
      if (status) status.textContent = error.message || 'Could not change the Pi hostname.';
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

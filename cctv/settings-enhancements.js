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
  const style = document.createElement('style');
  style.textContent = '.device-identity-card{grid-column:1/-1!important;padding:24px 26px!important}.device-identity-card .panel-head{margin-bottom:18px}.device-identity-card .panel-head h2{margin-bottom:5px}.device-identity-fields{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px 18px;align-items:end}.device-identity-fields label,.device-hostname-form label{display:grid;gap:7px;color:var(--muted);font-size:.82rem;font-weight:650}.device-identity-fields input,.device-hostname-form input{width:100%;box-sizing:border-box}.device-identity-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap;grid-column:1/-1}.device-identity-actions .notice{min-height:1.2em}.device-hostname-form{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px 18px;align-items:end;margin-top:22px;padding-top:20px;border-top:1px solid var(--line)}.device-hostname-form label{min-width:0}.device-hostname-form>.notice{grid-column:1/-1;margin:0;color:var(--muted);font-size:.8rem}.device-hostname-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.device-hostname-actions .notice{min-height:1.2em}@media(max-width:650px){.device-identity-fields,.device-hostname-form{grid-template-columns:1fr}.device-identity-actions{grid-column:auto}.device-hostname-form>.notice{grid-column:auto}.device-hostname-actions{align-items:flex-start;flex-direction:column}}';
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
      grid.insertAdjacentHTML('afterbegin', `<section class="device-card device-identity-card"><div class="panel-head"><div><h2>Device identity</h2><span class="notice">Keep the dashboard label and Pi network name easy to recognize. The device ID stays unchanged.</span></div></div><form id="device-profile-form" onsubmit="saveDeviceProfile(event, '${esc(id)}')"><div class="device-identity-fields"><label>Dashboard name<input id="device-profile-name" maxlength="80" required value="${esc(device.name || '')}"></label><label>Location<input id="device-profile-location" maxlength="120" placeholder="Optional" value="${esc(device.location || '')}"></label><div class="device-identity-actions"><button class="primary" type="submit">Save dashboard details</button><span id="device-profile-status" class="notice" role="status" aria-live="polite"></span></div></div></form></section>`);
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
      await originalOpenDevice(id);
      ensureDeviceHostnameEditor(id);
    };
  }
})();

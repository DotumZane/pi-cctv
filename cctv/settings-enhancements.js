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

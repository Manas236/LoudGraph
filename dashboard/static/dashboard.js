(() => {
  'use strict';
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  let busy = false, toastTimer, pollBusy = false, selectionRequest = 0;
  const current = () => $('.review-grid')?.dataset.current;
  const queue = () => $$('[data-select]').map(e => e.dataset.select);
  const isEditing = () => ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
  function toast(message) {
    const el = $('#toast'); el.textContent = message; el.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, 5500);
  }
  if ($('#toast')?.textContent.trim()) toast($('#toast').textContent);
  function fitTitle() { const el = $('#video-title'); if (el) { el.style.height = 'auto'; el.style.height = `${el.scrollHeight + 3}px`; } }
  async function send(form, submitter) {
    const body = new FormData(form);
    if (submitter?.name) body.append(submitter.name, submitter.value);  // e.g. the batch size that was pressed
    const response = await fetch(submitter?.getAttribute('formaction') || form.action, {method: 'POST', body, headers: {'Accept': 'application/json'}});
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || 'Please refresh and try again.');
    return result.message;
  }
  async function select(ref, preserve = false) {
    const area = $('#review-area'); if (!area) return;
    const serial = ++selectionRequest;
    const old = $('.review-player'), time = old?.currentTime || 0, playing = old && !old.paused;
    const response = await fetch(`/fragment/review${ref ? '?selected=' + encodeURIComponent(ref) : ''}`, {cache: 'no-store'});
    if (!response.ok) throw new Error('The preview could not load. Please try again.');
    const html = await response.text(); if (serial !== selectionRequest) return;
    old?.pause(); area.innerHTML = html; fitTitle();
    if (preserve && String(ref) === current()) {
      const video = $('.review-player');
      video?.addEventListener('loadedmetadata', () => { video.currentTime = time; if (playing) video.play().catch(() => {}); }, {once: true});
    }
  }
  async function refreshQueue() {
    const list = $('#queue-list'); if (!list) return;
    const response = await fetch('/fragment/queue', {cache: 'no-store'});
    if (response.ok && !list.contains(document.activeElement)) list.innerHTML = await response.text();
  }
  function sidebar(data) {
    for (const [name, n] of Object.entries(data.counts || {})) {
      const badge = $(`[data-count="${name}"]`); if (badge) { badge.textContent = n; badge.hidden = !n; }
    }
    const line = $('[data-telegram-line]'); if (line && data.telegram) line.textContent = data.telegram;
    const bot = $('[data-bot-line]');
    if (bot) { bot.classList.toggle('is-running', data.bot); $('[data-bot-text]', bot).textContent = data.bot ? 'Bot is running' : 'Bot is stopped — double-click Bot.bat'; }
  }
  async function poll() {
    if (pollBusy || busy || document.hidden) return;
    pollBusy = true;
    try {
      const response = await fetch('/api/review', {cache: 'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      if ($('#activity') && !$('#activity details[open]') && !$('#activity')?.contains(document.activeElement)) $('#activity').innerHTML = data.activity;
      $('#status-line').innerHTML = data.status;
      const queued = $('[data-count="queue"]')?.textContent;
      sidebar(data);
      if (queued !== undefined && String(data.counts.queue) !== queued) await refreshQueue();
      if ($('#review-area') && !isEditing() && !busy && data.waiting.map(String).join(',') !== queue().join(',')) await select(current(), true);
    } catch (_) { /* A short offline period must not interrupt a preview. */ }
    finally { pollBusy = false; }
  }
  async function openDrawer(ref) {
    const response = await fetch(`/video/${encodeURIComponent(ref)}/drawer`, {cache: 'no-store'});
    if (!response.ok) throw new Error('Could not open this video. Please refresh.');
    $('#drawer-content').innerHTML = await response.text();
    const dialog = $('#video-drawer'); if (!dialog.open) dialog.showModal();
    dialog.scrollTop = 0;
  }
  document.addEventListener('click', async event => {
    const jump = event.target.closest('[data-select]'), step = event.target.closest('[data-step]'), drawer = event.target.closest('[data-drawer]');
    try {
      if (jump) { event.preventDefault(); if (!busy) await select(jump.dataset.select); }
      if (step && !busy) { const items = queue(), i = items.indexOf(current()); await select(items[(i + Number(step.dataset.step) + items.length) % items.length]); }
      if (drawer) { event.preventDefault(); await openDrawer(drawer.dataset.drawer); }
      if (event.target.closest('.drawer-close')) $('#video-drawer').close();
      if (event.target.closest('[data-theme-toggle]')) switchTheme();
      if (event.target.closest('[data-sidebar-open]')) menu(true);
      if (event.target.closest('[data-sidebar-close]') || (event.target.closest('.side-nav a') && document.body.classList.contains('nav-open'))) menu(false);
    } catch (error) { toast(error.message); }
  });
  function switchTheme() {
    const root = document.documentElement, next = root.dataset.theme === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    document.cookie = `theme=${next}; path=/; max-age=31536000; samesite=lax`;  // the server renders it on the next page
    $('[data-theme-toggle]')?.setAttribute('aria-label', next === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
  }
  function menu(open) {
    document.body.classList.toggle('nav-open', open);
    $('[data-sidebar-open]')?.setAttribute('aria-expanded', String(open));
    if (open) $('#sidebar .side-nav a')?.focus(); else if (document.activeElement?.closest('#sidebar')) $('[data-sidebar-open]')?.focus();
  }
  $('#video-drawer')?.addEventListener('close', () => { $$('#video-drawer video').forEach(v => v.pause()); });
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!form.matches('[data-async], [data-review-action], [data-title-form], [data-caption-form]')) return;
    event.preventDefault(); if (busy) return;
    const ask = event.submitter?.dataset.confirm || form.dataset.confirm;
    if (ask && !window.confirm(ask)) { if ($('[data-autosave]', form)) $('[data-autosave]', form).checked = !$('[data-autosave]', form).checked; return; }
    busy = true;
    const buttons = $$('button', form); buttons.forEach(b => { b.disabled = true; });
    const items = queue(), i = items.indexOf(current()), next = items[i + 1] || items[i - 1];
    try {
      const message = await send(form, event.submitter);
      if (form.matches('[data-title-form]')) { $('.title-result', form).textContent = ' · Saved'; $('#video-title').blur(); }
      else if (form.matches('[data-account-test]')) { $('.test-result', form.closest('.account-row')).textContent = message; }
      else { toast(message); const inline = $('.inline-result', form); if (inline) inline.textContent = message; }
      if (form.matches('[data-review-action]')) await select(next);
      if (form.matches('[data-caption-form]')) await select(current(), true);
      if (form.dataset.refreshDrawer) await openDrawer(form.dataset.refreshDrawer);
      if (form.dataset.refresh === 'queue') await refreshQueue();
      if (form.matches('[data-telegram]')) { form.classList.toggle('is-on', $('[data-autosave]', form).checked); if ($('#review-area')) await select(current(), true); }
    } catch (error) {
      toast(error.message);
      if (form.matches('[data-account-test]')) $('.test-result', form.closest('.account-row')).textContent = error.message;
      if ($('[data-autosave]', form)) $('[data-autosave]', form).checked = !$('[data-autosave]', form).checked;
    } finally { buttons.forEach(b => { b.disabled = false; }); busy = false; poll(); }
  });
  document.addEventListener('change', event => {
    const label = event.target.closest('.switch')?.querySelector('[data-switch-label]');
    if (label) label.textContent = event.target.checked ? 'On' : 'Off';
    if (event.target.matches('[data-autosave]')) event.target.form.requestSubmit();
  });
  document.addEventListener('input', event => { if (event.target.id === 'video-title') fitTitle(); });
  document.addEventListener('keydown', event => {
    if (event.target.id === 'video-title' && event.key === 'Enter') { event.preventDefault(); event.target.form.requestSubmit(); return; }
    if (event.key === 'Escape' && document.body.classList.contains('nav-open')) { menu(false); return; }
    if (busy || event.repeat || isEditing() || event.ctrlKey || event.metaKey || event.altKey || $('#video-drawer')?.open || event.target.tagName === 'VIDEO') return;
    const action = {a: 'approve', t: 'telegram', m: 'remake', x: 'reject'}[event.key.toLowerCase()];
    const button = action && $('[data-review-action]') && $(`[data-action="${action}"]`);
    if (button) { event.preventDefault(); button.form.requestSubmit(button); }
    if (['ArrowLeft', 'ArrowRight'].includes(event.key) && queue().length) { event.preventDefault(); $(`[data-step="${event.key === 'ArrowLeft' ? '-1' : '1'}"]`).click(); }
  });
  const advanced = $('#advanced'); if (advanced && location.hash === '#advanced') advanced.open = true;
  window.addEventListener('hashchange', () => { if (advanced && location.hash === '#advanced') { advanced.open = true; advanced.scrollIntoView(); } });
  const openRef = new URLSearchParams(location.search).get('video');
  if ($('#video-drawer') && /^\d+$/.test(openRef || '')) openDrawer(openRef).catch(e => toast(e.message));
  fitTitle(); setInterval(poll, 2000);
})();

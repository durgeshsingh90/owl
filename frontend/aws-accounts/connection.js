"use strict";
// AWS CLI connection indicator: `aws sts get-caller-identity` checks and a 12-hour `aws sso login` session.
(() => {
  const $ = id => document.getElementById(id);
  const ASSETS = '../bitbucket/assets/';
  const CHECK_EVERY = 5 * 60 * 1000;
  let current = null, offset = 0, pollTimer = 0, checking = false;

  const duration = seconds => {
    seconds = Math.max(0, Math.floor(seconds));
    const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
    return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m ${String(s).padStart(2, '0')}s`;
  };
  const clock = seconds => new Date(seconds * 1000).toLocaleString(undefined, {dateStyle:'medium', timeStyle:'short'});
  const now = () => Date.now() / 1000 + offset;

  async function api(path, options = {}) {
    const response = await fetch(path, {cache:'no-store', ...options, headers:{'Content-Type':'application/json', ...options.headers}});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(typeof body.detail === 'string' ? body.detail : 'OWL backend is unavailable.');
    return body;
  }

  function visualState() {
    if (checking && !current) return 'connecting';
    if (!current) return 'failed';
    if (current.login_status === 'pending') return 'connecting';
    return current.status === 'connected' ? 'connected' : current.status === 'unknown' ? 'connecting' : 'failed';
  }

  function render() {
    const state = visualState();
    const image = state === 'connected' ? 'connected.png' : state === 'connecting' ? 'no-connection.gif' : 'disconnected.png';
    $('connection-status').dataset.state = state;
    $('connection-dialog').dataset.state = state;
    $('connection-image').src = ASSETS + image;
    $('dialog-image').src = ASSETS + image;
    const pending = current?.login_status === 'pending';
    const connected = state === 'connected';
    const left = connected && current.expires_at ? current.expires_at - now() : 0;
    const label = pending ? 'Waiting for approval' : connected ? 'Connected' : state === 'connecting' ? 'Checking…' : 'Disconnected';
    $('connection-label').textContent = label;
    $('connection-timer').textContent = pending ? `started ${duration(now() - current.login_started)} ago`
      : connected && current.expires_at ? `${duration(left)} left` : current?.profile || '';
    $('connection-open').title = `${label}${current ? ` · profile ${current.profile}` : ''}. Click for details.`;
    $('connection-login').hidden = connected || pending || state === 'connecting';

    if (!current) {
      $('dialog-status').textContent = checking ? 'Checking…' : 'OWL backend is unavailable';
      return;
    }
    $('dialog-status').textContent = label;
    const identity = current.identity;
    $('dialog-detail').textContent = connected && identity ? `${identity.Arn} · account ${identity.Account}`
      : `Profile ${current.profile}${current.checked_at ? ` · checked ${clock(current.checked_at)}` : ''}`;
    $('session').hidden = !(connected && current.expires_at);
    if (connected && current.expires_at) {
      $('session-active').textContent = duration(now() - current.approved_at) + (current.source === 'detected' ? ' (detected)' : '');
      $('session-left').textContent = duration(left);
      $('session-expires').textContent = clock(current.expires_at);
      $('session-progress').max = current.session_seconds;
      $('session-progress').value = Math.max(0, left);
    }
    $('login-pending').hidden = !pending;
    $('login-link').hidden = !current.login_url;
    if (current.login_url) $('login-url').href = current.login_url;
    $('login-code').textContent = current.login_code ? `· code ${current.login_code}` : '';
    $('dialog-error').textContent = current.error || '';
    $('dialog-error').hidden = !current.error || connected;
    $('check-command').textContent = `aws sts get-caller-identity --profile ${current.profile}`;
    $('login-command').textContent = `aws sso login --profile ${current.profile}`;
    if (document.activeElement !== $('profile-input')) $('profile-input').value = current.profile;
    $('dialog-login').disabled = pending;
    $('check-now').disabled = checking || pending;
  }

  function accept(state) {
    const wasPending = current?.login_status === 'pending';
    current = state;
    offset = state.now - Date.now() / 1000;
    render();
    clearTimeout(pollTimer);
    if (state.login_status === 'pending') pollTimer = setTimeout(() => load(false), 2000);
    else if (wasPending && state.login_status === 'approved') window.owlToast?.('AWS connection approved · active for 12 hours');
  }

  async function load(refresh) {
    if (refresh) { checking = true; render(); }
    try {
      accept(await api(`/api/aws-accounts/connection${refresh ? '?refresh=1' : ''}`));
    } catch (failure) {
      current = null;
      $('dialog-detail').textContent = failure.message;
      render();
    } finally {
      if (refresh) { checking = false; render(); }
    }
  }

  async function login() {
    try {
      accept(await api('/api/aws-accounts/connection/login', {method:'POST'}));
      $('connection-dialog').open || $('connection-dialog').showModal();
    } catch (failure) {
      $('dialog-error').textContent = failure.message;
      $('dialog-error').hidden = false;
      $('connection-dialog').open || $('connection-dialog').showModal();
    }
  }

  $('connection-open').addEventListener('click', () => $('connection-dialog').showModal());
  $('connection-login').addEventListener('click', login);
  $('dialog-login').addEventListener('click', login);
  $('check-now').addEventListener('click', () => load(true));
  document.querySelector('#connection-dialog [data-close]').addEventListener('click', () => $('connection-dialog').close());
  $('profile-form').addEventListener('submit', async event => {
    event.preventDefault();
    checking = true; render();
    try {
      accept(await api('/api/aws-accounts/connection', {method:'PUT', body:JSON.stringify({profile:$('profile-input').value.trim()})}));
    } catch (failure) {
      $('dialog-error').textContent = failure.message; $('dialog-error').hidden = false;
    } finally { checking = false; render(); }
  });

  // Tick the timers every second; re-check when the session runs out and every five minutes.
  setInterval(() => {
    if (current?.status === 'connected' && current.expires_at && current.expires_at <= now() && !checking) load(true);
    else render();
  }, 1000);
  setInterval(() => { if (current?.login_status !== 'pending') load(true); }, CHECK_EVERY);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && current?.login_status !== 'pending') load(true); });
  load(true);
})();

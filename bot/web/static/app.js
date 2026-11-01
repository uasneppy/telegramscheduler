// Post board: tabs, filters and the post list.
//
// Plain browser JavaScript, no framework and no build step. Text from the API is
// only ever written with textContent or setAttribute. Times are cut out of the
// server's Kyiv ISO strings ("2026-11-02T10:00:00+02:00"); the browser's own
// time zone is never consulted. The view (tab, channel, search) lives in the
// URL hash, e.g. #tab=scheduled&channel=-1001&q=news.
'use strict';

const TABS = ['scheduled', 'queued', 'failed', 'posted'];
// Tabs grouped under day headers, and the time field each one groups by.
const DAY_FIELD = { scheduled: 'scheduled_time', posted: 'posted_at' };
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const RELATIVE_DAYS = { '-1': 'Yesterday', 0: 'Today', 1: 'Tomorrow' };
const SEARCH_DELAY_MS = 300;
const TOAST_MS = 5000;
// Media types that have a thumbnail; every other type shows its icon.
const THUMB_TYPES = new Set(['photo', 'document_image']);
const MEDIA_TYPES = {
  photo: ['image', 'Photo'],
  document_image: ['image', 'Image sent as a file'],
  video: ['video', 'Video'],
  document_video: ['video', 'Video sent as a file'],
  animation: ['play', 'Animation'],
  audio: ['audio', 'Audio'],
  document: ['file', 'File'],
  album: ['album', 'Album'],
};
const KNOWN_STATUSES = new Set(['queued', 'scheduled', 'sending', 'failed', 'posted']);
// What each status allows (spec 6.2); sending and unknown posts allow nothing.
const EDITABLE = new Set(['queued', 'scheduled', 'failed']);
const DELETABLE = new Set(['queued', 'scheduled', 'failed', 'posted']);
const CHANNEL_TONES = 6;

const state = {
  me: null,          // the last /api/me answer
  tab: 'scheduled',
  channel: '',       // channel_id filter, '' for all channels
  q: '',             // caption search
  cursor: null,      // next_cursor of the last page shown; null once the list is complete
  ids: new Set(),    // ids of the rows shown, so a row never appears twice
  lastDay: null,     // day key of the last day header shown
  request: null,     // AbortController of the list request in flight
  loaded: false,     // the rows shown are the result of a finished load of this view
  // The banner message of each source's last failure, null once it last succeeded.
  // The banner shows while either is set; a list error also stops automatic loading.
  errors: { me: null, list: null },
  signedOut: false,
  searchTimer: 0,
  toastTimer: 0,
};

const el = {};
let sentinelObserver = null;

// --- API ----------------------------------------------------------------------------

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status; // 0 for a network failure
  }
}

// fetch with the board's rules: X-Board on every state-changing request, and a
// 401 from anywhere swaps the page for the signed-out view.
async function request(path, options = {}) {
  const method = options.method || 'GET';
  const headers = new Headers(options.headers);
  if (method !== 'GET' && method !== 'HEAD') headers.set('X-Board', '1');
  let response;
  try {
    response = await fetch(path, { ...options, method, headers, credentials: 'same-origin' });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError(0, 'Could not reach the board. Check the connection, then retry.');
  }
  if (response.status === 401) {
    showSignedOut();
    throw new ApiError(401, 'Not signed in.');
  }
  return response;
}

async function getJson(path, signal) {
  const response = await request(path, { signal });
  let body = null;
  try {
    body = await response.json();
  } catch (error) {
    if (error.name === 'AbortError') throw error;
  }
  if (!response.ok || body === null) {
    const message = response.status >= 500 || !body || !body.message
      ? `The board could not load this (error ${response.status}). Try again.`
      : body.message;
    throw new ApiError(response.status, message);
  }
  return body;
}

function postsUrl(cursor) {
  // An empty limit is refused, so limit is never sent; empty filters are left out too.
  const params = new URLSearchParams({ status: state.tab });
  if (state.channel) params.set('channel', state.channel);
  if (state.q) params.set('q', state.q);
  if (cursor) params.set('cursor', cursor);
  return `/api/posts?${params}`;
}

// --- view state in the URL hash -----------------------------------------------------

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const tab = params.get('tab');
  state.tab = TABS.includes(tab) ? tab : 'scheduled';
  state.channel = params.get('channel') || '';
  state.q = (params.get('q') || '').trim();
}

function writeHash() {
  const params = new URLSearchParams({ tab: state.tab });
  if (state.channel) params.set('channel', state.channel);
  if (state.q) params.set('q', state.q);
  history.replaceState(null, '', `#${params}`);
}

// --- times --------------------------------------------------------------------------

function hasIsoDate(iso) {
  return typeof iso === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(iso);
}

// "2026-11-02" from a Kyiv ISO string; '' when there is no usable time.
function dayKey(iso) {
  return hasIsoDate(iso) ? iso.slice(0, 10) : '';
}

function hhmm(iso) {
  return iso.slice(11, 16);
}

function dayNumber(key) {
  const [year, month, day] = key.split('-').map(Number);
  return Date.UTC(year, month - 1, day) / 86400000;
}

// "Mon 02.11"; the weekday comes from the date parts alone, in UTC.
function dayLabel(key) {
  if (!key) return 'No date';
  const weekday = WEEKDAYS[new Date(dayNumber(key) * 86400000).getUTCDay()];
  return `${weekday} ${key.slice(8, 10)}.${key.slice(5, 7)}`;
}

// "Today", "Tomorrow" or "Yesterday" by the server's Kyiv clock, else ''.
function relativeDay(key) {
  if (!key || !state.me || !hasIsoDate(state.me.now)) return '';
  return RELATIVE_DAYS[dayNumber(key) - dayNumber(dayKey(state.me.now))] || '';
}

// --- small DOM helpers --------------------------------------------------------------

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

function iconButton(className, iconName, label, disabled) {
  const button = make('button', `icon-button ${className}`);
  button.type = 'button';
  button.setAttribute('aria-label', label);
  button.title = label.split(',')[0];
  button.disabled = disabled;
  button.append(icon(iconName));
  return button;
}

// A stable colour slot per channel, so the same channel always gets the same dot.
function channelTone(channelId) {
  let hash = 0;
  for (const char of String(channelId ?? '')) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
  return `tone-${hash % CHANNEL_TONES}`;
}

// --- rows ---------------------------------------------------------------------------

function rowTime(item) {
  return DAY_FIELD[state.tab] ? item[DAY_FIELD[state.tab]] : item.scheduled_time;
}

function captionOf(item) {
  return (item.caption_text || '').trim();
}

// Accessible description of a row, used by its controls: "Mon 02.11 10:00, Alpha, Morning coffee…".
function rowLabel(item) {
  const iso = rowTime(item);
  const parts = [];
  if (hasIsoDate(iso)) parts.push(`${dayLabel(dayKey(iso))} ${hhmm(iso)}`);
  parts.push(item.channel_name || 'no channel');
  const caption = captionOf(item);
  parts.push(caption ? (caption.length > 40 ? `${caption.slice(0, 40)}…` : caption) : 'no caption');
  return parts.join(', ');
}

function timeCell(item) {
  const iso = rowTime(item);
  if (!hasIsoDate(iso)) {
    const none = make('span', 'time is-empty', '—');
    none.title = 'Not scheduled';
    return none;
  }
  // The Failed tab has no day headers, so its rows carry the date as well.
  const text = state.tab === 'failed' ? `${iso.slice(8, 10)}.${iso.slice(5, 7)} ${hhmm(iso)}` : hhmm(iso);
  const time = make('time', 'time', text);
  time.dateTime = iso;
  return time;
}

function typeIcon(item) {
  const [name, label] = MEDIA_TYPES[item.media_type] || ['file', 'Media'];
  const box = make('span', 'type-icon');
  box.dataset.type = item.media_type || '';
  box.setAttribute('role', 'img');
  box.setAttribute('aria-label', label);
  box.title = label;
  box.append(icon(name));
  return box;
}

function mediaCell(item) {
  const cell = make('div', 'media');
  if (!THUMB_TYPES.has(item.media_type) || item.media_count < 1) {
    cell.append(typeIcon(item));
    return cell;
  }
  const image = make('img', 'thumb');
  image.setAttribute('loading', 'lazy'); // before src, so the request waits for the scroll
  image.decoding = 'async';
  image.width = 48;
  image.height = 48;
  image.alt = MEDIA_TYPES[item.media_type][1];
  // A file that is gone (published media is cleaned up after 7 days) has no thumbnail.
  image.addEventListener('error', () => image.replaceWith(typeIcon(item)), { once: true });
  image.src = `/api/posts/${item.id}/thumb?i=0&w=160`;
  cell.append(image);
  return cell;
}

function bodyCell(item) {
  const body = make('div', 'row-body');
  const caption = captionOf(item);
  const snippet = make('p', caption ? 'snippet' : 'snippet is-empty', caption || 'No caption');
  if (caption) snippet.title = caption;
  body.append(snippet);
  if (item.status === 'failed') {
    const reason = make('p', 'failure-reason');
    reason.append(icon('alert'), make('span', '', item.failure_reason || 'No reason was recorded.'));
    if (item.retry_count > 0) {
      reason.append(make('span', 'retries', `· ${item.retry_count} ${item.retry_count === 1 ? 'retry' : 'retries'}`));
    }
    body.append(reason);
  }
  return body;
}

function badge(text, kind) {
  return make('span', `badge badge-${kind}`, text);
}

function badges(item) {
  const list = [];
  if (!KNOWN_STATUSES.has(item.status)) list.push(badge('Unknown status', 'unknown'));
  if (item.status === 'sending') list.push(badge('Sending', 'sending'));
  if (item.status === 'failed') list.push(badge('Failed', 'failed'));
  if (item.overdue) list.push(badge('Overdue', 'overdue'));
  if (item.media_type === 'album') list.push(badge(`Album ×${item.media_count}`, 'album'));
  if (item.is_recurring) {
    const hours = item.recurring_interval_hours;
    list.push(badge(hours ? `Recurring · ${hours}h` : 'Recurring', 'recurring'));
  }
  return list;
}

function metaCell(item) {
  const meta = make('div', 'row-meta');
  const name = item.channel_name || 'No channel';
  const chip = make('span', `channel-chip ${channelTone(item.channel_id)}`, name);
  chip.title = name;
  meta.append(chip, ...badges(item));
  return meta;
}

function actionsCell(item) {
  const label = rowLabel(item);
  const viewOnly = item.status === 'posted';
  const actions = make('div', 'row-actions');
  actions.append(
    iconButton('row-edit', viewOnly ? 'view' : 'edit', `${viewOnly ? 'Open' : 'Edit'} post, ${label}`,
      !EDITABLE.has(item.status) && !viewOnly),
    iconButton('row-delete', 'delete', `Delete post, ${label}`, !DELETABLE.has(item.status)),
  );
  return actions;
}

function postRow(item) {
  const row = make('div', 'post-row');
  row.dataset.id = String(item.id);
  row.dataset.status = item.status;
  const readOnly = !DELETABLE.has(item.status);
  if (readOnly) row.classList.add('is-read-only');
  const select = make('input', 'row-select');
  select.type = 'checkbox';
  select.disabled = readOnly;
  select.setAttribute('aria-label', `Select post, ${rowLabel(item)}`);
  // Time and meta share a wrapper: on desktop it is display:contents (both stay grid
  // columns), on phones it is one wrapping line, so the channel never squeezes into
  // the space a long time leaves.
  const head = make('div', 'row-head');
  head.append(timeCell(item), metaCell(item));
  row.append(select, head, mediaCell(item), bodyCell(item), actionsCell(item));
  return row;
}

function dayHeader(key) {
  const header = make('h2', 'day-header', dayLabel(key));
  header.dataset.day = key;
  const relative = relativeDay(key);
  if (relative) header.dataset.relative = relative; // shown by CSS, outside the header's text
  return header;
}

// --- list ---------------------------------------------------------------------------

function clearList() {
  el.list.replaceChildren();
  el.list.dataset.showing = state.tab; // a style hook; [data-tab] belongs to the tab buttons
  state.ids.clear();
  state.lastDay = null;
  state.cursor = null;
  state.loaded = false;
}

function appendItems(items) {
  const fragment = document.createDocumentFragment();
  const field = DAY_FIELD[state.tab];
  for (const item of items) {
    if (state.ids.has(item.id)) continue;
    state.ids.add(item.id);
    if (field) {
      const key = dayKey(item[field]);
      if (key !== state.lastDay) {
        fragment.append(dayHeader(key));
        state.lastDay = key;
      }
    }
    fragment.append(postRow(item));
  }
  el.list.append(fragment);
}

function updateListFooter() {
  const busy = state.request !== null;
  el.list.setAttribute('aria-busy', String(busy));
  const empty = !busy && state.loaded && state.ids.size === 0;
  el.empty.hidden = !empty;
  if (empty) el.empty.textContent = state.channel || state.q ? 'No posts match these filters.' : 'Nothing here yet.';
  if (busy) el.status.textContent = 'Loading…';
  else if (state.loaded && state.ids.size > 0 && state.cursor === null) el.status.textContent = 'End of list';
  else el.status.textContent = '';
  el.refresh.classList.toggle('is-spinning', busy);
}

// Runs one list request; a newer request supersedes (aborts) an older one.
async function runListRequest(work) {
  if (state.request) state.request.abort();
  const controller = new AbortController();
  state.request = controller;
  updateListFooter();
  try {
    await work(controller.signal, () => state.request === controller);
    if (state.request === controller) reportSuccess('list');
  } catch (error) {
    if (state.request === controller) reportFailure('list', error);
  } finally {
    if (state.request === controller) {
      state.request = null;
      updateListFooter();
      watchSentinel();
    }
  }
}

// Load the current tab from its start. keepRows refetches at least as many rows
// as are shown and swaps them in at once, so a refresh keeps the reader's place.
function reloadList({ keepRows = false } = {}) {
  // The hash's channel can only be checked against /api/me's channels, so until
  // /api/me has answered once the list waits, and asking for it retries /api/me.
  if (!state.me) return refreshAll();
  const wanted = keepRows ? state.ids.size : 0;
  if (!keepRows) {
    clearList();
    window.scrollTo(0, 0);
  }
  return runListRequest(async (signal, isCurrent) => {
    const items = [];
    let cursor = null;
    do {
      const page = await getJson(postsUrl(cursor), signal);
      if (!isCurrent()) return;
      items.push(...page.items);
      cursor = page.next_cursor;
    } while (cursor && items.length < wanted);
    clearList();
    appendItems(items);
    state.cursor = cursor;
    state.loaded = true;
  });
}

function loadMore() {
  if (state.request || !state.cursor || state.errors.list || state.signedOut) return;
  const cursor = state.cursor;
  runListRequest(async (signal, isCurrent) => {
    const page = await getJson(postsUrl(cursor), signal);
    if (!isCurrent()) return;
    appendItems(page.items);
    state.cursor = page.next_cursor;
  });
}

// Observing again makes the observer report the sentinel's current position, so
// a page that still leaves the sentinel in view loads the next one.
function watchSentinel() {
  if (!sentinelObserver || state.signedOut) return;
  sentinelObserver.unobserve(el.sentinel);
  sentinelObserver.observe(el.sentinel);
}

// --- /api/me: counts and channels ---------------------------------------------------

function renderCounts(counts) {
  for (const button of el.tabs) {
    const count = Number(counts[button.dataset.tab]) || 0;
    button.querySelector('.count').textContent = String(count);
    button.classList.toggle('has-posts', count > 0);
  }
}

function renderChannels(channels) {
  const options = channels.map((channel) => {
    const option = make('option', '', channel.channel_name || channel.channel_id);
    option.value = channel.channel_id;
    return option;
  });
  el.channel.replaceChildren(el.channel.options[0], ...options);
  el.channel.value = state.channel;
  el.channel.disabled = false; // disabled in the markup until the options are known
}

function activeChannels() {
  return state.me && Array.isArray(state.me.channels) ? state.me.channels : [];
}

// The filter only offers active channels (posts on a removed channel still list
// under All channels), so a channel the hash names that is not one of them is dropped.
function dropUnknownChannel() {
  if (!state.channel || !state.me) return;
  if (activeChannels().some((channel) => channel.channel_id === state.channel)) return;
  state.channel = '';
  writeHash();
}

// Counts keep the tabs' "–" until the first answer, and the last known numbers
// after a later failure (the banner says the refresh failed).
async function loadMe() {
  try {
    state.me = await getJson('/api/me');
    renderCounts(state.me.counts || {});
    dropUnknownChannel();
    renderChannels(activeChannels());
    reportSuccess('me');
  } catch (error) {
    reportFailure('me', error);
  }
}

// --- refresh, banner, toast, signed out ---------------------------------------------

// Refetch /api/me and the list (Refresh, Retry, coming back to the browser tab).
async function refreshAll() {
  if (state.signedOut) return;
  if (!state.me) {
    // Never answered yet: the list waits for the channel check.
    await loadMe();
    if (state.me) await reloadList();
    else updateListFooter();
    return;
  }
  const channelBefore = state.channel;
  await Promise.all([loadMe(), reloadList({ keepRows: true })]);
  if (state.channel !== channelBefore) reloadList();
}

function reportFailure(source, error) {
  if (error.name === 'AbortError' || state.signedOut || error.status === 401) return;
  if (!(error instanceof ApiError)) console.error(error); // a bug in this page, not a network or server problem
  state.errors[source] = error instanceof ApiError ? error.message : 'Something went wrong on this page. Try again.';
  renderBanner();
}

function reportSuccess(source) {
  state.errors[source] = null;
  renderBanner();
}

// The banner stays while /api/me or the list is failing; it goes once both last succeeded.
function renderBanner() {
  const message = state.errors.list || state.errors.me;
  if (message) el.bannerText.textContent = message;
  el.banner.hidden = !message;
}

function showToast(text) {
  el.toast.replaceChildren(make('span', 'toast-text', text));
  el.toast.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { el.toast.hidden = true; }, TOAST_MS);
}

function showSignedOut() {
  if (state.signedOut) return;
  state.signedOut = true;
  if (state.request) state.request.abort();
  if (sentinelObserver) sentinelObserver.disconnect();
  clearTimeout(state.searchTimer);
  el.app.hidden = true;
  el.signedOut.hidden = false;
  document.title = 'Post board: not signed in';
}

async function signOut() {
  el.signout.disabled = true;
  try {
    // A fetch, not a form: the page's referrer policy keeps a real Origin on it.
    const response = await request('/logout', { method: 'POST' });
    if (!response.ok) throw new ApiError(response.status, `Sign-out failed (error ${response.status}). Try again.`);
    location.replace('/');
  } catch (error) {
    el.signout.disabled = false;
    if (error.status !== 401) showToast(error.message);
  }
}

// --- controls -----------------------------------------------------------------------

function syncTabs() {
  for (const button of el.tabs) {
    const selected = button.dataset.tab === state.tab;
    button.setAttribute('aria-selected', String(selected));
    button.tabIndex = selected ? 0 : -1;
    if (selected) el.list.setAttribute('aria-labelledby', button.id);
  }
}

function syncControls() {
  syncTabs();
  el.channel.value = state.channel;
  // Before /api/me answers, only "All channels" exists (and the select is disabled):
  // show it instead of a blank select.
  if (el.channel.selectedIndex < 0) el.channel.selectedIndex = 0;
  el.search.value = state.q;
}

function selectTab(tab) {
  if (tab === state.tab) return;
  state.tab = tab;
  writeHash();
  syncTabs();
  reloadList();
}

function onTabKey(event) {
  const index = el.tabs.findIndex((button) => button.dataset.tab === state.tab);
  const moves = { ArrowLeft: index - 1, ArrowRight: index + 1, Home: 0, End: el.tabs.length - 1 };
  if (!(event.key in moves)) return;
  event.preventDefault();
  const next = el.tabs[(moves[event.key] + el.tabs.length) % el.tabs.length];
  selectTab(next.dataset.tab);
  next.focus();
}

function applySearch() {
  clearTimeout(state.searchTimer);
  const q = el.search.value.trim();
  if (q === state.q) return;
  state.q = q;
  writeHash();
  reloadList();
}

function bindControls() {
  for (const button of el.tabs) button.addEventListener('click', () => selectTab(button.dataset.tab));
  el.tabs[0].parentElement.addEventListener('keydown', onTabKey);
  el.channel.addEventListener('change', () => {
    state.channel = el.channel.value;
    writeHash();
    reloadList();
  });
  el.search.addEventListener('input', () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(applySearch, SEARCH_DELAY_MS);
  });
  el.search.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') applySearch();
  });
  el.refresh.addEventListener('click', refreshAll);
  el.bannerRetry.addEventListener('click', refreshAll);
  el.signout.addEventListener('click', signOut);
  window.addEventListener('hashchange', () => {
    readHash();
    dropUnknownChannel();
    syncControls();
    reloadList();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') refreshAll();
  });
  sentinelObserver = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) loadMore();
  }, { rootMargin: '0px 0px 800px 0px' });
}

async function start() {
  Object.assign(el, {
    app: document.getElementById('app'),
    signedOut: document.getElementById('signed-out'),
    list: document.getElementById('post-list'),
    empty: document.getElementById('empty-state'),
    status: document.getElementById('list-status'),
    sentinel: document.getElementById('list-sentinel'),
    channel: document.getElementById('channel-filter'),
    search: document.getElementById('search'),
    refresh: document.getElementById('refresh'),
    signout: document.getElementById('signout'),
    banner: document.getElementById('banner'),
    bannerText: document.getElementById('banner-text'),
    bannerRetry: document.getElementById('banner-retry'),
    toast: document.getElementById('toast'),
    tabs: [...document.querySelectorAll('.tab[data-tab]')],
  });
  readHash();
  syncControls();
  bindControls();
  writeHash();
  // /api/me first: its channel list decides whether the hash's channel is valid.
  await refreshAll();
}

start();

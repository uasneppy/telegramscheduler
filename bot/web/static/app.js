// Post board: tabs, filters, the post list and the edit panel.
//
// Plain browser JavaScript, no framework and no build step. Text from the API is
// only ever written with textContent or setAttribute; the one exception is the
// server's sanitized caption preview HTML (setPreviewHtml). Times are cut out of
// the server's Kyiv ISO strings ("2026-11-02T10:00:00+02:00") and sent back as
// Kyiv wall-clock "YYYY-MM-DDTHH:MM"; the browser's own time zone is never
// consulted. The view (tab, channel, search) lives in the URL hash, e.g.
// #tab=scheduled&channel=-1001&q=news.
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
// What each status allows (spec 6.2), for the list rows only: list items carry no
// `can`. The edit panel goes by the detail's `can` flags instead.
const EDITABLE = new Set(['queued', 'scheduled', 'failed']);
const DELETABLE = new Set(['queued', 'scheduled', 'failed', 'posted']);
const CHANNEL_TONES = 6;
const ROW_THUMB_W = 160;

// Edit panel.
const PREVIEW_DELAY_MS = 250;
const CAPTION_LIMIT = 1024;
const UPLOAD_MAX_BYTES = 50 * 1024 * 1024;
const PANEL_THUMB_W = 512;
const STRIP_THUMB_W = 240;
const STATUS_LABELS = { queued: 'Queued', scheduled: 'Scheduled', sending: 'Sending', failed: 'Failed', posted: 'Posted' };
// The tab a post lists under, by its board status (spec 6.1).
const TAB_OF_STATUS = { queued: 'queued', scheduled: 'scheduled', sending: 'scheduled', failed: 'failed', posted: 'posted' };
// Ctrl/Cmd + key in the caption editor.
const SHORTCUTS = { b: 'b', i: 'i', u: 'u', k: 'link' };
const URL_SCHEME = /^[a-z][a-z0-9+.-]*:/i;
const LINK_SCHEMES = /^(https?|tg):\/\//i;
const PHOTO_FILE = /\.(jpe?g|png|webp)$/i;
const DISCARD_QUESTION = 'Discard your unsaved changes to this post?';
const POSTED_DELETE_QUESTION = 'Delete this posted post from the board? '
  + 'Removes this record only; the message stays in the channel.';
const READ_ONLY_NOTES = {
  posted: 'Already posted, so it can no longer be edited. Deleting it removes this record only; '
    + 'the message stays in the channel.',
  sending: 'This post is being sent right now, so it cannot be changed.',
};
const UNKNOWN_NOTE = 'This post has an unknown status, so the board cannot change it.';

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
  panel: null,       // the open edit panel (see openPanel); null while it is closed
  // Post id -> a counter bumped each time the board replaces the post's media. It
  // goes into thumbnail URLs: a thumbnail is cached for an hour under the post's
  // URL, so the same URL would keep showing the old picture.
  thumbVersions: new Map(),
};

const el = {};
let sentinelObserver = null;

// --- API ----------------------------------------------------------------------------

class ApiError extends Error {
  constructor(status, message, body = null) {
    super(message);
    this.status = status; // 0 for a network failure
    this.body = body;     // the JSON error body, when there was one (a 409 carries `status`)
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

// The JSON answer of a request; anything else is an ApiError carrying the server's
// message (for a 4xx) and its error body. `verb` words the message for a 5xx.
async function apiJson(path, options = {}, verb = 'load') {
  const response = await request(path, options);
  let body = null;
  try {
    body = await response.json();
  } catch (error) {
    if (error.name === 'AbortError') throw error;
  }
  if (!response.ok || body === null) {
    const message = response.status >= 500 || !body || !body.message
      ? `The board could not ${verb} this (error ${response.status}). Try again.`
      : body.message;
    throw new ApiError(response.status, message, body);
  }
  return body;
}

function getJson(path, signal) {
  return apiJson(path, { signal });
}

// A state-changing call with an optional JSON body (request() adds X-Board).
function sendJson(path, method, body) {
  const options = { method };
  if (body !== undefined) {
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify(body);
  }
  return apiJson(path, options, 'complete');
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

// "Mon 02.11 10:00" ("Today 10:00" when it is today in Kyiv), from a Kyiv ISO string.
function whenText(iso) {
  if (!hasIsoDate(iso)) return '';
  const key = dayKey(iso);
  return `${relativeDay(key) || dayLabel(key)} ${hhmm(iso)}`;
}

// The value of a datetime-local input for a Kyiv ISO string: its wall-clock
// "YYYY-MM-DDTHH:MM", exactly as the server stored it; '' when there is no time.
function localTimeValue(iso) {
  return hasIsoDate(iso) ? iso.slice(0, 16) : '';
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

function thumbUrl(postId, index, width) {
  const version = state.thumbVersions.get(postId);
  return `/api/posts/${postId}/thumb?i=${index}&w=${width}${version ? `&v=${version}` : ''}`;
}

function mediaUrl(postId, index) {
  return `/api/posts/${postId}/media?i=${index}`;
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
  image.src = thumbUrl(item.id, 0, ROW_THUMB_W);
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
  // Every post opens; one the board cannot change opens read-only (the panel follows `can`).
  const editable = EDITABLE.has(item.status);
  const actions = make('div', 'row-actions');
  actions.append(
    iconButton('row-edit', editable ? 'edit' : 'view', `${editable ? 'Edit' : 'Open'} post, ${label}`, false),
    iconButton('row-delete', 'delete', `Delete post, ${label}`, !DELETABLE.has(item.status)),
  );
  return actions;
}

function postRow(item) {
  const row = make('div', 'post-row');
  row.dataset.id = String(item.id);
  row.dataset.status = item.status;
  row.tabIndex = 0; // Enter on a focused row opens the panel
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

function findRow(postId) {
  return el.list.querySelector(`.post-row[data-id="${Number(postId)}"]`);
}

// The row control that has focus, so a re-render can put focus back on the new row.
function focusedRowControl() {
  const active = document.activeElement;
  const rowNode = active instanceof Element ? active.closest('#post-list .post-row') : null;
  if (!rowNode) return null;
  const control = ['row-select', 'row-edit', 'row-delete'].find((name) => active.classList.contains(name));
  return { id: rowNode.dataset.id, control: control || null };
}

function restoreRowFocus(focus) {
  if (!focus) return;
  const rowNode = findRow(focus.id);
  const target = rowNode && focus.control ? rowNode.querySelector(`.${focus.control}`) : rowNode;
  (target || selectedTab()).focus({ preventScroll: true }); // the rows kept their place
}

// Take a post's row out of the list, with its day header when that day is now empty.
function removeRow(postId) {
  const rowNode = findRow(postId);
  if (!rowNode) return;
  const focus = focusedRowControl();
  state.ids.delete(Number(postId));
  const before = rowNode.previousElementSibling;
  const after = rowNode.nextElementSibling;
  rowNode.remove();
  if (before && before.classList.contains('day-header') && (!after || after.classList.contains('day-header'))) {
    before.remove();
    if (!after) {
      // That was the last day shown: the next page starts a header of its own again.
      const headers = el.list.querySelectorAll('.day-header');
      state.lastDay = headers.length ? headers[headers.length - 1].dataset.day : null;
    }
  }
  if (focus) {
    // Focus moves on to the next row (past a day header), else to the tab.
    let next = after;
    while (next && !next.classList.contains('post-row')) next = next.nextElementSibling;
    (next || selectedTab()).focus();
  }
  updateListFooter();
  watchSentinel();
}

// Bring the list in line with a post the panel changed: drop the row when the post
// no longer belongs to this view, reload the rows (keeping the place) when its
// position may have moved, else replace the row where it is.
function refreshRow(before, item) {
  const inView = TAB_OF_STATUS[item.status] === state.tab && (!state.channel || item.channel_id === state.channel);
  if (!inView) {
    removeRow(item.id);
    return;
  }
  const rowNode = findRow(item.id);
  const field = DAY_FIELD[state.tab]; // Queued and Failed keep their order on any edit
  const moved = !rowNode || !before || (field && before[field] !== item[field])
    || (state.q && before.caption_text !== item.caption_text);
  if (moved) {
    reloadList({ keepRows: true });
    return;
  }
  const focus = focusedRowControl();
  rowNode.replaceWith(postRow(item));
  restoreRowFocus(focus);
}

function updateListFooter() {
  const busy = state.request !== null;
  el.list.setAttribute('aria-busy', String(busy));
  const empty = !busy && state.loaded && state.ids.size === 0 && state.cursor === null;
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
    const focus = focusedRowControl();
    clearList();
    appendItems(items);
    state.cursor = cursor;
    state.loaded = true;
    restoreRowFocus(focus);
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
  if (state.panel) dropPanel(); // nothing can be saved any more
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

// --- edit panel ---------------------------------------------------------------------
//
// One post at a time, opened from its row; while it is open the page behind it is
// inert. What it offers comes only from the detail's `can` flags. The fields start
// from the stored post (the baseline) and Save sends only the fields that differ,
// then uploads a chosen replacement file. Post now and Retry save pending changes
// the same way first, so an edit is never left behind. A finished action closes
// the panel and refreshes the post's row and the tab counts.

function newPanel(id, opener, openerKind) {
  return {
    id,
    opener,              // the element that opened the panel; focus goes back to it
    openerKind,          // 'edit' (the row's open button) or 'row' (Enter on the row)
    detail: null,        // the post as the server last returned it
    baseline: null,      // {caption, channel, time}: the field values of the stored post
    loadSeq: 0,          // the newest detail load; older answers are dropped
    previewSeq: 0,       // the newest preview request; older answers are dropped
    previewTimer: 0,
    previewInFlight: false,
    check: null,         // the newest preview answer ({ok, error, length, signature_fits, ...})
    signatureFits: null, // whether the selected channel's signature fits, once known
    file: null,          // a chosen replacement file, uploaded by Save
    busy: '',            // the action in flight: 'load', 'save', 'post-now', 'retry' or 'delete'
    error: '',           // the message of the last failed action
    linkRange: null,     // the caption selection a link is being added to
  };
}

function openPanel(postId, opener, openerKind) {
  if (state.panel || state.signedOut) return;
  const panel = newPanel(postId, opener, openerKind);
  state.panel = panel;
  el.panel.dataset.postId = String(postId);
  el.panelTitle.textContent = 'Post';
  el.panelMeta.replaceChildren();
  el.panelNotices.replaceChildren();
  el.panelLoading.textContent = 'Loading…';
  el.panelLoading.hidden = false;
  el.panelFields.hidden = true;
  el.preview.setAttribute('aria-busy', 'false');
  renderControls(); // no actions until the post is known
  el.scrim.hidden = false;
  el.panel.hidden = false;
  setBackgroundInert(true);
  document.body.classList.add('panel-open');
  el.panel.focus();
  // A fresh Kyiv clock for the time hint (and fresh counts on the way).
  loadMe().then(() => {
    if (state.panel === panel && panel.detail) renderTimeHint(panel);
  });
  loadPanel(panel);
}

function setBackgroundInert(inert) {
  for (const node of el.background) node.inert = inert;
}

// Fetch the post into the panel; resolves to the detail, or null when it did not
// arrive. keepEdits (after a 409) keeps the person's unsaved changes as long as
// the post can still be edited.
async function loadPanel(panel, { keepEdits = false } = {}) {
  const seq = ++panel.loadSeq;
  const edits = keepEdits && panel.baseline ? { changes: changedFields(panel), file: panel.file } : null;
  panel.busy = 'load';
  el.panel.dataset.state = 'loading';
  renderControls();
  let detail;
  try {
    detail = await getJson(`/api/posts/${panel.id}`);
  } catch (error) {
    if (state.panel !== panel || seq !== panel.loadSeq) return null;
    panel.busy = '';
    if (error.status === 404) postGone(panel);
    else if (error.status !== 401) panelLoadFailed(panel, error);
    return null;
  }
  if (state.panel !== panel || seq !== panel.loadSeq) return null;
  applyDetail(panel, detail, edits);
  return detail;
}

function panelLoadFailed(panel, error) {
  if (!(error instanceof ApiError)) console.error(error); // a bug in this page
  const message = error instanceof ApiError ? error.message : 'Something went wrong on this page. Try again.';
  if (panel.detail) {
    // A reload: the post as last seen stays on screen.
    panel.error = message;
    el.panel.dataset.state = 'ready';
    renderControls();
  } else {
    el.panelLoading.textContent = message;
    el.panel.dataset.state = 'error';
  }
}

// Fill the panel from a post detail.
function applyDetail(panel, detail, edits = null) {
  const can = detail.can || {};
  panel.detail = detail;
  panel.baseline = {
    caption: detail.caption_tagged || '',
    channel: detail.channel_id || '',
    time: localTimeValue(detail.scheduled_time),
  };
  panel.busy = '';
  panel.error = '';
  panel.check = null;
  panel.signatureFits = detail.signature_fits ?? null;
  panel.file = null;
  el.mediaInput.value = '';
  el.asFile.checked = false;
  closeLinkRow();
  el.captionInput.value = panel.baseline.caption;
  renderChannelOptions(detail);
  el.channelSelect.value = panel.baseline.channel;
  el.timeInput.value = panel.baseline.time;
  if (edits && can.edit) {
    // Put back what the person had changed; the other fields show the post as stored now.
    const { changes } = edits;
    if ('caption' in changes) el.captionInput.value = changes.caption;
    if ('channel_id' in changes) el.channelSelect.value = changes.channel_id;
    if ('scheduled_time' in changes) el.timeInput.value = changes.scheduled_time || '';
    if (can.media) panel.file = edits.file;
  }
  renderPanelHeader(detail);
  renderNotices(detail);
  renderMedia(panel);
  setPreviewHtml(detail.caption_html || '');
  el.preview.classList.remove('is-stale');
  el.panelLoading.hidden = true;
  el.panelFields.hidden = false;
  el.panel.dataset.state = 'ready';
  schedulePreview(panel, 0); // the counter and the signature note come from the server
  renderControls();
}

function channelOption(value, label) {
  const option = make('option', '', label);
  option.value = value;
  return option;
}

function renderChannelOptions(detail) {
  const channels = activeChannels();
  const options = channels.map((channel) => channelOption(channel.channel_id, channel.channel_name || channel.channel_id));
  if (!detail.channel_id) {
    options.unshift(channelOption('', 'Choose a channel'));
  } else if (!channels.some((channel) => channel.channel_id === detail.channel_id)) {
    // A channel no longer active in the bot: listed so the select still says where the post is.
    options.unshift(channelOption(detail.channel_id, `${detail.channel_name || detail.channel_id} (removed)`));
  }
  el.channelSelect.replaceChildren(...options);
}

function renderPanelHeader(detail) {
  el.panelTitle.textContent = detail.can && detail.can.edit ? 'Edit post' : 'Post details';
  const known = KNOWN_STATUSES.has(detail.status);
  const parts = [badge(known ? STATUS_LABELS[detail.status] : 'Unknown status', known ? detail.status : 'unknown')];
  const name = detail.channel_name || 'No channel';
  const chip = make('span', `channel-chip ${channelTone(detail.channel_id)}`, name);
  chip.title = name;
  parts.push(chip);
  const iso = detail.status === 'posted' ? detail.posted_at : detail.scheduled_time;
  if (hasIsoDate(iso)) {
    const time = make('time', 'panel-when', whenText(iso));
    time.dateTime = iso;
    time.title = 'Kyiv time';
    parts.push(time);
  }
  if (detail.overdue) parts.push(badge('Overdue', 'overdue'));
  el.panelMeta.replaceChildren(...parts);
}

function notice(kind, iconName, text) {
  const box = make('p', `notice notice-${kind}`);
  box.append(icon(iconName), make('span', '', text));
  return box;
}

function renderNotices(detail) {
  const notices = [];
  if (detail.status === 'failed') {
    let text = `Failed: ${detail.failure_reason || 'no reason was recorded.'}`;
    if (detail.retry_count > 0) text += ` (${detail.retry_count} ${detail.retry_count === 1 ? 'retry' : 'retries'})`;
    notices.push(notice('danger', 'alert', text));
  }
  if (!(detail.can && detail.can.edit)) notices.push(notice('info', 'info', READ_ONLY_NOTES[detail.status] || UNKNOWN_NOTE));
  if (detail.is_recurring) {
    const every = detail.recurring_interval_hours ? ` every ${detail.recurring_interval_hours} h` : '';
    notices.push(notice('info', 'refresh', `Repeats${every}. Changes apply to the whole series; deleting it ends the series.`));
  }
  el.panelNotices.replaceChildren(...notices);
}

// --- panel: media

function mediaKind(type) {
  return MEDIA_TYPES[type] || ['file', 'Media'];
}

function mediaPlaceholder(iconName, text) {
  const box = make('div', 'media-placeholder');
  box.append(icon(iconName), make('span', '', text));
  return box;
}

// One media item as the panel shows it: a picture, a player, or its type's icon.
function mediaElement(postId, item, width, label) {
  if (!item.exists) return mediaPlaceholder('alert', 'File no longer available');
  const type = item.media_type;
  const [iconName, typeLabel] = mediaKind(type);
  let node;
  let src;
  if (THUMB_TYPES.has(type) || type === 'animation') {
    node = make('img', 'media-image');
    node.alt = label;
    node.decoding = 'async';
    src = THUMB_TYPES.has(type) ? thumbUrl(postId, item.index, width) : mediaUrl(postId, item.index);
  } else if (type === 'video' || type === 'document_video' || type === 'audio') {
    const audio = type === 'audio';
    node = make(audio ? 'audio' : 'video', audio ? 'media-audio' : 'media-video');
    node.controls = true;
    node.preload = 'metadata';
    if (!audio) node.playsInline = true;
    node.setAttribute('aria-label', label);
    src = mediaUrl(postId, item.index);
  } else {
    return mediaPlaceholder(iconName, typeLabel);
  }
  // A file the browser cannot show (or a thumbnail the server cannot make) falls back to the icon.
  node.addEventListener('error', () => node.replaceWith(mediaPlaceholder(iconName, typeLabel)), { once: true });
  node.src = src;
  return node;
}

function renderMedia(panel) {
  const { detail } = panel;
  const items = Array.isArray(detail.media) ? detail.media : [];
  const album = detail.media_type === 'album';
  const replaceable = Boolean(detail.can && detail.can.media);
  el.mediaView.classList.toggle('is-album', album);
  if (album) {
    const strip = make('div', 'media-strip');
    strip.setAttribute('role', 'list');
    for (const item of items) {
      const cell = make('div', 'media-item');
      cell.setAttribute('role', 'listitem');
      cell.append(mediaElement(detail.id, item, STRIP_THUMB_W, `Album item ${item.index + 1} of ${items.length}`));
      strip.append(cell);
    }
    el.mediaView.replaceChildren(strip);
    el.mediaCaption.textContent = `Album of ${items.length}. An album's media cannot be replaced from the board.`;
  } else if (items.length > 0) {
    const item = items[0];
    const label = mediaKind(item.media_type)[1];
    el.mediaView.replaceChildren(mediaElement(detail.id, item, PANEL_THUMB_W, label));
    if (item.exists) el.mediaCaption.textContent = label;
    else if (replaceable) el.mediaCaption.textContent = 'The file is gone from the server. Choose new media to replace it.';
    else el.mediaCaption.textContent = 'The file is gone from the server (published media is removed after 7 days).';
  } else {
    el.mediaView.replaceChildren(mediaPlaceholder('file', 'No media'));
    el.mediaCaption.textContent = '';
  }
  el.mediaReplace.hidden = !replaceable;
  renderStaged(panel);
}

function formatBytes(size) {
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  if (size >= 1024) return `${Math.round(size / 1024)} KB`;
  return `${size} bytes`;
}

function renderStaged(panel) {
  const { file } = panel;
  el.mediaStaged.hidden = !file;
  el.mediaFileName.textContent = file
    ? `${file.name} (${formatBytes(file.size)}) replaces the current media when you save.`
    : '';
}

function onFileChosen() {
  const panel = state.panel;
  const file = el.mediaInput.files && el.mediaInput.files[0];
  if (!panel || !panel.detail || !file) return;
  el.mediaInput.value = ''; // the same file can be chosen again later
  if (file.size === 0) {
    panel.error = 'The chosen file is empty.';
  } else if (file.size > UPLOAD_MAX_BYTES) {
    panel.error = `The chosen file is ${formatBytes(file.size)}; the limit is 50 MB.`;
  } else {
    panel.file = file;
    panel.error = '';
  }
  renderStaged(panel);
  renderControls();
}

function unstageFile() {
  const panel = state.panel;
  if (!panel) return;
  panel.file = null;
  renderStaged(panel);
  renderControls();
  el.mediaInput.focus(); // the button that had focus is gone
}

// New media keeps the post's URLs: bump its thumbnail version for this page, and
// refresh the browser's cached thumbnails that a later visit would otherwise reuse.
function mediaReplaced(detail) {
  state.thumbVersions.set(detail.id, (state.thumbVersions.get(detail.id) || 0) + 1);
  if (!THUMB_TYPES.has(detail.media_type)) return;
  for (const width of [ROW_THUMB_W, PANEL_THUMB_W]) {
    fetch(`/api/posts/${detail.id}/thumb?i=0&w=${width}`, { cache: 'reload', credentials: 'same-origin' })
      .catch(() => {});
  }
}

// --- panel: caption editor

// The only HTML the board writes: the server's caption preview, built from
// whitelisted tags with all text escaped (captions.render_html). Nothing typed in
// the page and no other API text ever goes through innerHTML.
function setPreviewHtml(html) {
  el.preview.innerHTML = html;
}

function schedulePreview(panel, delay = PREVIEW_DELAY_MS) {
  clearTimeout(panel.previewTimer);
  panel.previewTimer = setTimeout(() => {
    panel.previewTimer = 0;
    runPreview(panel);
  }, delay);
  markPreviewBusy(panel);
}

function markPreviewBusy(panel) {
  el.preview.setAttribute('aria-busy', String(panel.previewTimer !== 0 || panel.previewInFlight));
}

// Ask the server to check and render the caption. Only the newest request's answer counts.
async function runPreview(panel) {
  const seq = ++panel.previewSeq;
  panel.previewInFlight = true;
  markPreviewBusy(panel);
  let answer = null;
  try {
    answer = await sendJson('/api/caption/preview', 'POST', {
      caption: el.captionInput.value,
      channel_id: el.channelSelect.value || null,
    });
  } catch (error) {
    if (!(error instanceof ApiError)) console.error(error);
  }
  if (state.panel !== panel || seq !== panel.previewSeq) return; // a newer request owns the preview
  panel.previewInFlight = false;
  // Without an answer the preview stays as it was and nothing is blocked: Save is checked on the server.
  panel.check = answer;
  if (answer) {
    if (answer.signature_fits !== null) panel.signatureFits = answer.signature_fits;
    if (answer.html !== null) setPreviewHtml(answer.html);
    el.preview.classList.toggle('is-stale', answer.html === null);
  }
  markPreviewBusy(panel);
  renderControls();
}

function onCaptionInput() {
  const panel = state.panel;
  if (!panel || !panel.detail) return;
  panel.error = '';
  schedulePreview(panel);
  renderControls();
}

// [start, end) without the white space at its edges, so tags hug the words.
function trimRange(value, start, end) {
  while (start < end && /\s/.test(value[start])) start += 1;
  while (end > start && /\s/.test(value[end - 1])) end -= 1;
  return [start, end];
}

// Put `text` in place of the caption's [start, end) and select text[from, to).
// insertText keeps the browser's undo history (Ctrl+Z undoes a format);
// setRangeText is the fallback where it is not supported.
function replaceCaptionRange(start, end, text, from, to) {
  const area = el.captionInput;
  area.focus();
  area.setSelectionRange(start, end);
  if (!document.execCommand('insertText', false, text)) {
    area.setRangeText(text, start, end, 'end');
    onCaptionInput();
  }
  area.setSelectionRange(start + from, start + to);
}

// Wrap the selected caption text in a tag, or take the tag off text it wraps exactly.
function formatSelection(tag) {
  const area = el.captionInput;
  if (area.readOnly) return;
  const { value } = area;
  const [start, end] = trimRange(value, area.selectionStart, area.selectionEnd);
  const open = `<${tag}>`;
  const close = `</${tag}>`;
  const selected = value.slice(start, end);
  if (start >= open.length && value.slice(start - open.length, start) === open
      && value.slice(end, end + close.length) === close) {
    replaceCaptionRange(start - open.length, end + close.length, selected, 0, selected.length);
  } else if (selected.length >= open.length + close.length && selected.startsWith(open) && selected.endsWith(close)) {
    const inner = selected.slice(open.length, selected.length - close.length);
    replaceCaptionRange(start, end, inner, 0, inner.length);
  } else {
    replaceCaptionRange(start, end, open + selected + close, open.length, open.length + selected.length);
  }
}

function escapeText(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escapeAttribute(text) {
  return escapeText(text).replace(/"/g, '&quot;');
}

function openLinkRow() {
  const panel = state.panel;
  if (!panel || el.captionInput.readOnly) return;
  panel.linkRange = [el.captionInput.selectionStart, el.captionInput.selectionEnd];
  el.linkRow.hidden = false;
  el.linkButton.setAttribute('aria-expanded', 'true');
  el.linkUrl.value = '';
  el.linkUrl.focus();
}

function closeLinkRow() {
  el.linkRow.hidden = true;
  el.linkButton.setAttribute('aria-expanded', 'false');
}

function toggleLinkRow() {
  if (el.linkRow.hidden) {
    openLinkRow();
  } else {
    closeLinkRow();
    el.captionInput.focus();
  }
}

// Wrap the selection (or, with none, the address itself) in <a href="…">.
function applyLink() {
  const panel = state.panel;
  if (!panel) return;
  let url = el.linkUrl.value.trim();
  if (!url) {
    el.linkUrl.focus();
    return;
  }
  if (!URL_SCHEME.test(url)) url = `https://${url}`;
  if (!LINK_SCHEMES.test(url)) {
    panel.error = 'A link must start with https://, http:// or tg://.';
    renderControls();
    el.linkUrl.focus();
    return;
  }
  const { value } = el.captionInput;
  const range = panel.linkRange || [el.captionInput.selectionStart, el.captionInput.selectionEnd];
  const [start, end] = trimRange(value, range[0], range[1]);
  const text = value.slice(start, end) || escapeText(url);
  const open = `<a href="${escapeAttribute(url)}">`;
  closeLinkRow();
  panel.linkRange = null;
  panel.error = '';
  replaceCaptionRange(start, end, `${open}${text}</a>`, open.length, open.length + text.length);
}

function onCaptionKeydown(event) {
  if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return;
  const action = SHORTCUTS[event.key.toLowerCase()];
  if (!action || el.captionInput.readOnly) return;
  event.preventDefault();
  if (action === 'link') openLinkRow();
  else formatSelection(action);
}

// --- panel: channel and time

function onChannelChange() {
  const panel = state.panel;
  if (!panel || !panel.detail) return;
  panel.error = '';
  panel.signatureFits = null; // until the server answers for this channel
  schedulePreview(panel, 0);
  renderControls();
}

function onTimeInput() {
  const panel = state.panel;
  if (!panel || !panel.detail) return;
  panel.error = '';
  renderControls();
}

function unschedule() {
  const panel = state.panel;
  if (!panel) return;
  el.timeInput.value = '';
  panel.error = '';
  renderControls();
  el.timeInput.focus(); // the button is disabled now that there is no time
}

// A datetime-local the person left half typed reads as '' but is not a cleared time.
function timeIncomplete() {
  return el.timeInput.validity.badInput;
}

function signatureFor(panel, channelId) {
  if (channelId === (panel.detail.channel_id || '')) return panel.detail.signature || null;
  const channel = activeChannels().find((candidate) => candidate.channel_id === channelId);
  return (channel && channel.signature) || null;
}

// --- panel: what changed, and what may be done

function fieldValues() {
  return {
    caption: el.captionInput.value,
    channel: el.channelSelect.value,
    time: el.timeInput.value.slice(0, 16), // "YYYY-MM-DDTHH:MM", Kyiv wall clock
  };
}

// The fields that differ from the stored post, in the shape PATCH takes.
function changedFields(panel) {
  const changes = {};
  const base = panel.baseline;
  if (!base) return changes;
  const now = fieldValues();
  if (now.caption !== base.caption) changes.caption = now.caption;
  if (now.channel && now.channel !== base.channel) changes.channel_id = now.channel;
  if (now.time !== base.time) changes.scheduled_time = now.time || null; // no time unschedules
  return changes;
}

function isDirty(panel) {
  return panel.file !== null || Object.keys(changedFields(panel)).length > 0;
}

// Why these changes cannot be saved as they are, or ''.
function saveProblem(panel, changes) {
  if ('caption' in changes && panel.check && !panel.check.ok) return panel.check.error || 'Check the caption.';
  if ('scheduled_time' in changes && timeIncomplete()) return 'Finish the date and time, or press Unschedule.';
  return '';
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text; // role="alert" re-announces on every change
}

function renderCounter(panel) {
  const length = panel.check && typeof panel.check.length === 'number' ? panel.check.length : null;
  setText(el.counter, `${length === null ? '–' : length} / ${CAPTION_LIMIT}`);
  el.counter.classList.toggle('is-over', length !== null && length > CAPTION_LIMIT);
}

function renderSignature(panel) {
  const signature = signatureFor(panel, el.channelSelect.value);
  const leftOut = Boolean(signature) && panel.signatureFits === false;
  el.signatureNote.hidden = !signature;
  el.signatureNote.classList.toggle('is-warning', leftOut);
  let note = '';
  if (leftOut) {
    note = `The channel signature will be left out: caption and signature together are over ${CAPTION_LIMIT} characters.`;
  } else if (signature) {
    note = 'The channel signature is added below the caption when it is posted.';
  }
  setText(el.signatureNote, note);
  el.previewSignature.hidden = !signature || leftOut;
  setText(el.previewSignature, signature || '');
}

function renderTimeHint(panel) {
  const { detail } = panel;
  const parts = [];
  if (detail.can && detail.can.edit) {
    if (el.timeInput.value === '' && !timeIncomplete()) {
      parts.push(panel.baseline.time
        ? 'No time: saving moves the post to Queued.'
        : 'No time: the post waits in Queued. Pick a time to schedule it.');
    }
    if (detail.can.retry) parts.push('Retry sends it at this time if that is still ahead, otherwise right away.');
  }
  if (state.me && hasIsoDate(state.me.now)) {
    parts.push(`In Kyiv it is now ${dayLabel(dayKey(state.me.now))}, ${hhmm(state.me.now)}.`);
  }
  setText(el.timeHint, parts.join(' '));
}

function renderPanelError(panel) {
  const { check } = panel;
  const invalid = Boolean(check && !check.ok);
  let message = panel.error;
  if (!message && invalid) message = check.error || '';
  if (!message && panel.detail && panel.detail.can && panel.detail.can.edit && timeIncomplete()) {
    message = 'Finish the date and time, or press Unschedule.';
  }
  el.panelError.hidden = !message;
  setText(el.panelError, message);
  el.captionInput.setAttribute('aria-invalid', String(invalid));
}

// Show, enable and label the panel's controls from `can`, the pending changes and
// the action in flight.
function renderControls() {
  const panel = state.panel;
  if (!panel) return;
  const { detail } = panel;
  const can = (detail && detail.can) || {};
  const busy = panel.busy !== '';
  const changes = changedFields(panel);
  const dirty = isDirty(panel);
  const problem = saveProblem(panel, changes);
  // Post now sets its own time, so a pending time change is not saved before it.
  const { scheduled_time: _ignored, ...beforePostNow } = changes;
  const savesFirst = panel.file !== null || Object.keys(beforePostNow).length > 0;

  el.save.hidden = !(can.edit || can.media);
  el.save.disabled = busy || !dirty || Boolean(problem);
  setText(el.save, panel.busy === 'save' ? 'Saving…' : 'Save');
  el.postNow.hidden = !can.post_now;
  el.postNow.disabled = busy || (savesFirst && Boolean(saveProblem(panel, beforePostNow)));
  setText(el.postNow, panel.busy === 'post-now' ? 'Posting…' : savesFirst ? 'Save & post now' : 'Post now');
  el.retry.hidden = !can.retry;
  el.retry.disabled = busy || (dirty && Boolean(problem));
  setText(el.retry, panel.busy === 'retry' ? 'Retrying…' : dirty ? 'Save & retry' : 'Retry');
  el.deleteButton.hidden = !can.delete;
  el.deleteButton.disabled = busy;
  setText(el.deleteButton, panel.busy === 'delete' ? 'Deleting…' : 'Delete');

  el.captionInput.readOnly = !can.edit;
  el.toolbar.hidden = !can.edit;
  if (!can.edit) closeLinkRow();
  el.channelSelect.disabled = !can.edit;
  el.timeInput.disabled = !can.edit;
  el.unschedule.hidden = !can.edit;
  el.unschedule.disabled = el.timeInput.value === '' && !timeIncomplete();
  el.mediaReplace.hidden = !can.media;
  el.asFile.disabled = busy;
  if (detail) {
    renderCounter(panel);
    renderSignature(panel);
    renderTimeHint(panel);
  }
  renderPanelError(panel);
}

// --- panel: actions

// Run one panel action at a time. On failure: a 409 shows its message as a toast
// and reloads the post, a 404 closes the panel, anything else shows in the panel.
async function runPanelAction(panel, kind, work) {
  if (panel.busy || !panel.detail) return;
  const before = panel.detail;
  panel.busy = kind;
  panel.error = '';
  renderControls();
  try {
    await work(before);
  } catch (error) {
    // A save may have gone through before a later step failed: show what is stored now.
    if (panel.detail !== before) {
      refreshRow(before, panel.detail);
      loadMe();
    }
    panel.busy = '';
    actionFailed(panel, error);
  }
}

function actionFailed(panel, error) {
  if (error.name === 'AbortError' || error.status === 401) return;
  if (!(error instanceof ApiError)) console.error(error); // a bug in this page
  const message = error instanceof ApiError ? error.message : 'Something went wrong on this page. Try again.';
  if (error.status === 404) {
    postGone(panel);
  } else if (state.panel !== panel) {
    showToast(message); // closed while the action ran
  } else if (error.status === 409) {
    // The post changed under the panel (it was just published, say): say so and show it as it is now.
    showToast(message);
    const prior = panel.detail;
    loadPanel(panel, { keepEdits: true }).then((fresh) => {
      if (!fresh) return;
      refreshRow(prior, fresh);
      loadMe();
    });
  } else {
    panel.error = message;
    renderControls();
  }
}

function postGone(panel) {
  if (state.panel === panel) closePanel({ force: true });
  removeRow(panel.id);
  loadMe();
  showToast('This post no longer exists; it may have been deleted elsewhere.');
}

// A finished action: close the panel, bring the row and the counts up to date, say what happened.
function actionDone(panel, before, detail, message) {
  if (state.panel === panel) closePanel({ force: true });
  refreshRow(before, detail);
  loadMe();
  showToast(message);
}

// Save what the person changed: one PATCH with only the changed fields (Post now
// leaves the time out, it sets its own), then the chosen file. The baseline follows
// each step, so a later failure never sends a saved change twice. Resolves to a
// note for the toast ('' when there is none).
async function saveChanges(panel, { withTime }) {
  const changes = changedFields(panel);
  if (!withTime) delete changes.scheduled_time;
  if (Object.keys(changes).length > 0) {
    panel.detail = await sendJson(`/api/posts/${panel.id}`, 'PATCH', changes);
    if ('caption' in changes) panel.baseline.caption = changes.caption;
    if ('channel_id' in changes) panel.baseline.channel = changes.channel_id;
    if ('scheduled_time' in changes) panel.baseline.time = changes.scheduled_time || '';
  }
  if (!panel.file) return '';
  const { file } = panel;
  const asFile = el.asFile.checked;
  const form = new FormData();
  form.append('as_file', asFile ? '1' : '0'); // before the file: the server reads it only there
  form.append('file', file, file.name);
  panel.detail = await apiJson(`/api/posts/${panel.id}/media`, { method: 'PUT', body: form }, 'complete');
  panel.file = null;
  mediaReplaced(panel.detail);
  if (!asFile && PHOTO_FILE.test(file.name) && panel.detail.media_type === 'document') {
    return 'The photo is over 10 MB, so it goes out as a file.';
  }
  return '';
}

// "tomorrow at 10:00" or "Wed 04.11 at 10:00", from a Kyiv ISO string.
function whenPhrase(iso) {
  const key = dayKey(iso);
  const relative = relativeDay(key);
  return `${relative ? relative.toLowerCase() : dayLabel(key)} at ${hhmm(iso)}`;
}

function withNote(text, note) {
  return note ? `${text} ${note}` : text;
}

function savedMessage(before, detail) {
  if (detail.status === 'queued' && before.status !== 'queued') return 'Saved. The post moved to Queued.';
  if (detail.scheduled_time === before.scheduled_time || !hasIsoDate(detail.scheduled_time)) return 'Changes saved.';
  const when = `${whenPhrase(detail.scheduled_time)} Kyiv time`;
  return detail.status === 'failed' ? `Saved for ${when}. Press Retry to send it.` : `Saved. Scheduled for ${when}.`;
}

function savePanel() {
  const panel = state.panel;
  if (!panel || el.save.disabled) return;
  runPanelAction(panel, 'save', async (before) => {
    const note = await saveChanges(panel, { withTime: true });
    actionDone(panel, before, panel.detail, withNote(savedMessage(before, panel.detail), note));
  });
}

function postNowFromPanel() {
  const panel = state.panel;
  if (!panel || el.postNow.disabled) return;
  runPanelAction(panel, 'post-now', async (before) => {
    const note = await saveChanges(panel, { withTime: false });
    panel.detail = await sendJson(`/api/posts/${panel.id}/post-now`, 'POST');
    const when = whenPhrase(panel.detail.scheduled_time);
    actionDone(panel, before, panel.detail, withNote(`Posting ${when} Kyiv time, in about a minute.`, note));
  });
}

function retryFromPanel() {
  const panel = state.panel;
  if (!panel || el.retry.disabled) return;
  runPanelAction(panel, 'retry', async (before) => {
    const note = await saveChanges(panel, { withTime: true });
    panel.detail = await sendJson(`/api/posts/${panel.id}/retry`, 'POST');
    const when = whenPhrase(panel.detail.scheduled_time);
    actionDone(panel, before, panel.detail, withNote(`Retry scheduled for ${when} Kyiv time.`, note));
  });
}

function deleteFromPanel() {
  const panel = state.panel;
  if (!panel || !panel.detail || el.deleteButton.disabled) return;
  // A posted post's message stays in the channel; only the board's record goes. Say so first.
  if (panel.detail.status === 'posted' && !window.confirm(POSTED_DELETE_QUESTION)) return;
  runPanelAction(panel, 'delete', async (before) => {
    await sendJson(`/api/posts/${panel.id}`, 'DELETE');
    if (state.panel === panel) closePanel({ force: true });
    removeRow(panel.id);
    loadMe();
    postDeleted(before);
  });
}

// Task 15 hook: the Undo toast goes here (POST /api/posts/{id}/restore puts the post back).
function postDeleted(_item) {
  showToast('Post deleted.');
}

// --- panel: closing

// Close the panel, asking first when there are unsaved changes (unless forced, or
// while an action is saving them). Returns whether it closed.
function closePanel({ force = false } = {}) {
  const panel = state.panel;
  if (!panel) return true;
  const saving = panel.busy !== '' && panel.busy !== 'load';
  if (!force && !saving && isDirty(panel) && !window.confirm(DISCARD_QUESTION)) return false;
  dropPanel();
  returnFocus(panel);
  return true;
}

// Hide the panel and give the page behind it back.
function dropPanel() {
  const panel = state.panel;
  if (!panel) return;
  clearTimeout(panel.previewTimer);
  state.panel = null;
  closeLinkRow();
  el.panel.hidden = true;
  el.scrim.hidden = true;
  el.panel.dataset.state = 'closed';
  delete el.panel.dataset.postId;
  el.mediaView.replaceChildren(); // stops a playing video
  setPreviewHtml('');
  setBackgroundInert(false);
  document.body.classList.remove('panel-open');
}

// Focus goes back where the panel was opened from: the row's open button, or the row.
function returnFocus(panel) {
  let target = panel.opener && panel.opener.isConnected ? panel.opener : null;
  if (!target) {
    const rowNode = findRow(panel.id); // the row was drawn again meanwhile
    if (rowNode) target = panel.openerKind === 'row' ? rowNode : rowNode.querySelector('.row-edit');
  }
  (target || selectedTab()).focus();
}

function onPanelKeydown(event) {
  if (event.key !== 'Escape' || !state.panel) return;
  if (!el.linkRow.hidden && el.linkRow.contains(document.activeElement)) {
    closeLinkRow(); // Esc in the link field closes just the link field
    el.captionInput.focus();
    return;
  }
  event.preventDefault();
  closePanel();
}

function bindPanel() {
  el.list.addEventListener('click', (event) => {
    const opener = event.target instanceof Element ? event.target.closest('.row-edit') : null;
    if (opener && !opener.disabled) openPanel(Number(opener.closest('.post-row').dataset.id), opener, 'edit');
  });
  el.list.addEventListener('keydown', (event) => {
    const target = event.target;
    if (event.key !== 'Enter' || !(target instanceof Element) || !target.classList.contains('post-row')) return;
    event.preventDefault();
    openPanel(Number(target.dataset.id), target, 'row');
  });
  el.panelClose.addEventListener('click', () => closePanel());
  el.scrim.addEventListener('click', () => closePanel());
  document.addEventListener('keydown', onPanelKeydown);
  window.addEventListener('beforeunload', (event) => {
    if (!state.panel || !isDirty(state.panel)) return;
    event.preventDefault(); // leaving the page would lose the panel's unsaved changes
    event.returnValue = ''; // older browsers ask only when this is set
  });

  el.toolbar.addEventListener('mousedown', (event) => {
    // Keep the caption focused, so its selection stays visible while formatting.
    if (event.target instanceof Element && event.target.closest('[data-fmt]')) event.preventDefault();
  });
  el.toolbar.addEventListener('click', (event) => {
    const tool = event.target instanceof Element ? event.target.closest('[data-fmt]') : null;
    if (!tool) return;
    if (tool.dataset.fmt === 'link') toggleLinkRow();
    else formatSelection(tool.dataset.fmt);
  });
  el.linkApply.addEventListener('click', applyLink);
  el.linkUrl.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    applyLink();
  });
  el.captionInput.addEventListener('input', onCaptionInput);
  el.captionInput.addEventListener('keydown', onCaptionKeydown);
  el.channelSelect.addEventListener('change', onChannelChange);
  el.timeInput.addEventListener('input', onTimeInput);
  el.timeInput.addEventListener('change', onTimeInput);
  el.unschedule.addEventListener('click', unschedule);
  el.mediaInput.addEventListener('change', onFileChosen);
  el.mediaUnstage.addEventListener('click', unstageFile);
  el.save.addEventListener('click', savePanel);
  el.postNow.addEventListener('click', postNowFromPanel);
  el.retry.addEventListener('click', retryFromPanel);
  el.deleteButton.addEventListener('click', deleteFromPanel);
}

// --- controls -----------------------------------------------------------------------

function selectedTab() {
  return el.tabs.find((button) => button.dataset.tab === state.tab) || el.tabs[0];
}

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
    // Made inert while the panel is open; the dock with the banner and toast is not.
    background: [...document.querySelectorAll('#app > .topbar, #app > .tabs, #app > .board')],
    panel: document.getElementById('edit-panel'),
    scrim: document.getElementById('panel-scrim'),
    panelTitle: document.getElementById('panel-title'),
    panelMeta: document.getElementById('panel-meta'),
    panelClose: document.getElementById('panel-close'),
    panelLoading: document.getElementById('panel-loading'),
    panelNotices: document.getElementById('panel-notices'),
    panelFields: document.getElementById('panel-fields'),
    mediaView: document.getElementById('media-view'),
    mediaCaption: document.getElementById('media-caption'),
    mediaReplace: document.getElementById('media-replace'),
    mediaInput: document.getElementById('replace-media-input'),
    asFile: document.getElementById('as-file'),
    mediaStaged: document.getElementById('media-staged'),
    mediaFileName: document.getElementById('media-file-name'),
    mediaUnstage: document.getElementById('media-unstage'),
    toolbar: document.getElementById('caption-toolbar'),
    linkButton: document.querySelector('[data-fmt="link"]'),
    linkRow: document.getElementById('link-row'),
    linkUrl: document.getElementById('link-url'),
    linkApply: document.getElementById('link-apply'),
    captionInput: document.getElementById('caption-input'),
    counter: document.getElementById('caption-counter'),
    signatureNote: document.getElementById('signature-note'),
    preview: document.getElementById('caption-preview'),
    previewSignature: document.getElementById('preview-signature'),
    channelSelect: document.getElementById('channel-select'),
    timeInput: document.getElementById('time-input'),
    unschedule: document.getElementById('unschedule'),
    timeHint: document.getElementById('time-hint'),
    panelError: document.getElementById('panel-error'),
    save: document.getElementById('save'),
    postNow: document.getElementById('post-now'),
    retry: document.getElementById('retry'),
    deleteButton: document.getElementById('delete'),
  });
  readHash();
  syncControls();
  bindControls();
  bindPanel();
  writeHash();
  // /api/me first: its channel list decides whether the hash's channel is valid.
  await refreshAll();
}

start();

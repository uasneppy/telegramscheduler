// The post list: tabs, filters and the view in the URL hash (e.g.
// #tab=scheduled&channel=-1001&q=news), rows, loading and infinite scroll, the
// counts and channels from /api/me, and the banner, toast, announcer and signed-out view.
import { ApiError, getJson, request } from './api.js';
import { state, el } from './state.js';
import { dayKey, dayLabel, hasIsoDate, hhmm, relativeDay } from './time.js';

const TABS = ['scheduled', 'queued', 'failed', 'posted'];
// Tabs grouped under day headers, and the time field each one groups by.
const DAY_FIELD = { scheduled: 'scheduled_time', posted: 'posted_at' };
const SEARCH_DELAY_MS = 300;
const TOAST_MS = 5000;
const ACTION_TOAST_MS = 10000; // a toast with a button (Undo) stays longer
const ANNOUNCE_DELAY_MS = 100;
// Media types that have a thumbnail; every other type shows its icon.
export const THUMB_TYPES = new Set(['photo', 'document_image']);
export const MEDIA_TYPES = {
  photo: ['image', 'Photo'],
  document_image: ['image', 'Image sent as a file'],
  video: ['video', 'Video'],
  document_video: ['video', 'Video sent as a file'],
  animation: ['play', 'Animation'],
  audio: ['audio', 'Audio'],
  document: ['file', 'File'],
  album: ['album', 'Album'],
};
export const KNOWN_STATUSES = new Set(['queued', 'scheduled', 'sending', 'failed', 'posted']);
// What each status allows (spec 6.2), for the list rows only: list items carry no
// `can`. The edit panel goes by the detail's `can` flags instead.
const EDITABLE = new Set(['queued', 'scheduled', 'failed']);
const DELETABLE = new Set(['queued', 'scheduled', 'failed', 'posted']);
const CHANNEL_TONES = 6;
export const ROW_THUMB_W = 160;
// The tab a post lists under, by its board status (spec 6.1).
export const TAB_OF_STATUS = { queued: 'queued', scheduled: 'scheduled', sending: 'scheduled', failed: 'failed', posted: 'posted' };

let sentinelObserver = null;
// The list item each row was drawn from (see rowItem).
const rowItems = new WeakMap();
// Whether the pointer rests on the toast or focus is in it; its countdown waits meanwhile.
const toastHold = { pointer: false, focus: false };

// Registered by main.js. The panel and actions modules import this one, so the list
// reaches them through hooks instead of importing them back. rowsChanged runs after
// rows were drawn, added or taken out, so the selection can follow.
export const hooks = { dropPanel: () => {}, rowsChanged: () => {} };

function postsUrl(cursor) {
  // An empty limit is refused, so limit is never sent; empty filters are left out too.
  const params = new URLSearchParams({ status: state.tab });
  if (state.channel) params.set('channel', state.channel);
  if (state.q) params.set('q', state.q);
  if (cursor) params.set('cursor', cursor);
  return `/api/posts?${params}`;
}

// --- view state in the URL hash -----------------------------------------------------

export function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const tab = params.get('tab');
  state.tab = TABS.includes(tab) ? tab : 'scheduled';
  state.channel = params.get('channel') || '';
  state.q = (params.get('q') || '').trim();
}

export function writeHash() {
  const params = new URLSearchParams({ tab: state.tab });
  if (state.channel) params.set('channel', state.channel);
  if (state.q) params.set('q', state.q);
  history.replaceState(null, '', `#${params}`);
}

// --- small DOM helpers --------------------------------------------------------------

export function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function icon(name) {
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
export function channelTone(channelId) {
  let hash = 0;
  for (const char of String(channelId ?? '')) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
  return `tone-${hash % CHANNEL_TONES}`;
}

export function thumbUrl(postId, index, width) {
  const version = state.thumbVersions.get(postId);
  return `/api/posts/${postId}/thumb?i=${index}&w=${width}${version ? `&v=${version}` : ''}`;
}

export function mediaUrl(postId, index) {
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
export function rowLabel(item) {
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

export function badge(text, kind) {
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
  select.checked = !readOnly && state.selected.has(item.id); // a selection outlives a redraw
  select.setAttribute('aria-label', `Select post, ${rowLabel(item)}`);
  // Time and meta share a wrapper: on desktop it is display:contents (both stay grid
  // columns), on phones it is one wrapping line, so the channel never squeezes into
  // the space a long time leaves.
  const head = make('div', 'row-head');
  head.append(timeCell(item), metaCell(item));
  row.append(select, head, mediaCell(item), bodyCell(item), actionsCell(item));
  rowItems.set(row, item);
  return row;
}

// The list item a row was drawn from, or null.
export function rowItem(rowNode) {
  return rowItems.get(rowNode) || null;
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
  hooks.rowsChanged();
}

export function findRow(postId) {
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

// The nearest row before or after a row (`step` is 'previousElementSibling' or
// 'nextElementSibling'), past day headers; null when there is none.
function siblingRow(rowNode, step) {
  let node = rowNode[step];
  while (node && !node.classList.contains('post-row')) node = node[step];
  return node;
}

// Take a post's row out of the list, with its day header when that day is now empty.
// When focus was in the row, it moves on to the next row, else the previous one, else
// the tab. `moveFocus` says focus was there even if it has left meanwhile (a button
// disabled during a request drops its focus).
export function removeRow(postId, { moveFocus = false } = {}) {
  const rowNode = findRow(postId);
  if (!rowNode) return;
  const focus = moveFocus || focusedRowControl() !== null;
  const nextRow = siblingRow(rowNode, 'nextElementSibling');
  const previousRow = siblingRow(rowNode, 'previousElementSibling');
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
  if (focus) (nextRow || previousRow || selectedTab()).focus();
  updateListFooter();
  watchSentinel();
  hooks.rowsChanged();
}

// Bring the list in line with a post the panel changed: drop the row when the post
// no longer belongs to this view, reload the rows (keeping the place) when its
// position may have moved, else replace the row where it is.
export function refreshRow(before, item) {
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
  hooks.rowsChanged();
}

function updateListFooter() {
  const busy = state.request !== null;
  el.list.setAttribute('aria-busy', String(busy));
  const empty = !busy && state.loaded && state.ids.size === 0 && state.cursor === null;
  el.empty.hidden = !empty;
  if (empty) renderEmptyState();
  if (busy) el.status.textContent = 'Loading…';
  else if (state.loaded && state.ids.size > 0 && state.cursor === null) el.status.textContent = 'End of list';
  else el.status.textContent = '';
  el.refresh.classList.toggle('is-spinning', busy);
}

function renderEmptyState() {
  if (state.channel || state.q) {
    el.empty.textContent = 'No posts match these filters.';
  } else if (activeChannels().length === 0) {
    el.empty.replaceChildren('No channels yet. Add one in Telegram with ', make('code', '', '/start'), '.');
  } else {
    el.empty.textContent = 'Nothing here yet.';
  }
}

// Runs one list request; a newer request supersedes (aborts) an older one. Resolves to
// whether this request finished and its rows are the ones shown.
async function runListRequest(work) {
  if (state.request) state.request.abort();
  const controller = new AbortController();
  state.request = controller;
  updateListFooter();
  let finished = false;
  try {
    await work(controller.signal, () => state.request === controller);
    if (state.request === controller) {
      reportSuccess('list');
      finished = true;
    }
  } catch (error) {
    if (state.request === controller) reportFailure('list', error);
  } finally {
    if (state.request === controller) {
      state.request = null;
      updateListFooter();
      watchSentinel();
    }
  }
  return finished;
}

// Load the current tab from its start. keepRows refetches at least as many rows
// as are shown (and at least `atLeast`) and swaps them in at once, so a refresh
// keeps the reader's place. Resolves to whether the rows shown are the fresh ones.
function reloadList({ keepRows = false, atLeast = 0 } = {}) {
  // The hash's channel can only be checked against /api/me's channels, so until
  // /api/me has answered once the list waits, and asking for it retries /api/me.
  if (!state.me) return refreshAll();
  const wanted = keepRows ? Math.max(state.ids.size, atLeast) : 0;
  if (!keepRows) {
    clearList();
    state.selected.clear(); // another view starts with nothing selected
    hooks.rowsChanged();
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

export function activeChannels() {
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
export async function loadMe() {
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

// --- refresh, banner, toast, announcer, signed out -----------------------------------

// Refetch /api/me and the list (Refresh, Retry, coming back to the browser tab),
// keeping at least the rows shown, or `atLeast` rows when that is more (posts just
// restored come back among them). Resolves to whether the rows shown are fresh.
export async function refreshAll({ atLeast = 0 } = {}) {
  if (state.signedOut) return false;
  if (!state.me) {
    // Never answered yet: the list waits for the channel check.
    await loadMe();
    if (state.me) return reloadList();
    updateListFooter();
    return false;
  }
  const channelBefore = state.channel;
  const [, fresh] = await Promise.all([loadMe(), reloadList({ keepRows: true, atLeast })]);
  if (state.channel === channelBefore) return fresh;
  reloadList();
  return false;
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

// Say what just happened. `action` ({label, className, run, hint}) adds a button, such
// as Undo, that works once, and keeps the toast for ACTION_TOAST_MS instead of TOAST_MS.
// A newer toast replaces the one shown. Screen readers hear it through #announcer, with
// the action's hint when it has one. Returns the action's button, or null.
export function showToast(text, action = null) {
  let button = null;
  const parts = [make('span', 'toast-text', text)];
  if (action) {
    button = make('button', `toast-action ${action.className || ''}`.trim(), action.label);
    button.type = 'button';
    button.addEventListener('click', () => {
      if (button.disabled) return;
      button.disabled = true;
      action.run();
    });
    parts.push(button);
  }
  el.toast.replaceChildren(...parts);
  el.toast.hidden = false;
  toastHold.focus = el.toast.contains(document.activeElement); // a replaced button took its focus along
  state.toastMs = action ? ACTION_TOAST_MS : TOAST_MS;
  restartToastTimer();
  const seconds = ACTION_TOAST_MS / 1000;
  announce(action ? `${text} ${action.hint || `${action.label} is available for`} ${seconds} seconds.` : text);
  return button;
}

function restartToastTimer() {
  clearTimeout(state.toastTimer);
  if (el.toast.hidden || toastHold.pointer || toastHold.focus) return;
  state.toastTimer = setTimeout(() => {
    el.toast.hidden = true;
    el.toast.replaceChildren();
  }, state.toastMs);
}

// The countdown waits while a mouse rests on the toast or focus is in it (a touch only
// passes), so its Undo does not vanish while someone is reaching for it.
function bindToastHold() {
  el.toast.addEventListener('pointerenter', (event) => {
    if (event.pointerType !== 'mouse') return;
    toastHold.pointer = true;
    clearTimeout(state.toastTimer);
  });
  el.toast.addEventListener('pointerleave', (event) => {
    if (event.pointerType !== 'mouse') return;
    toastHold.pointer = false;
    restartToastTimer();
  });
  el.toast.addEventListener('focusin', () => {
    toastHold.focus = true;
    clearTimeout(state.toastTimer);
  });
  el.toast.addEventListener('focusout', (event) => {
    if (el.toast.contains(event.relatedTarget)) return;
    toastHold.focus = false;
    restartToastTimer();
  });
}

// Read `text` to screen readers. #announcer is a live region that is always in the page
// (a region shown together with its text may go unread); it is emptied first and filled a
// moment later, so the same text twice in a row is read twice.
export function announce(text) {
  clearTimeout(state.announceTimer);
  el.announcer.textContent = '';
  state.announceTimer = setTimeout(() => { el.announcer.textContent = text; }, ANNOUNCE_DELAY_MS);
}

export function showSignedOut() {
  if (state.signedOut) return;
  state.signedOut = true;
  if (state.panel) hooks.dropPanel(); // nothing can be saved any more
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

export function selectedTab() {
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

export function syncControls() {
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

export function bindControls() {
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
  el.refresh.addEventListener('click', () => refreshAll());
  el.bannerRetry.addEventListener('click', () => refreshAll());
  el.signout.addEventListener('click', signOut);
  bindToastHold();
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

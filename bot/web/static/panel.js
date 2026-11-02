// The edit panel.
//
// One post at a time, opened from its row; while it is open the page behind it is
// inert. What it offers comes only from the detail's `can` flags. The fields start
// from the stored post (the baseline) and Save sends only the fields that differ,
// then uploads a chosen replacement file. Post now and Retry save pending changes
// the same way first, so an edit is never left behind. A finished action closes
// the panel and refreshes the post's row and the tab counts.
import { POSTED_DELETE_QUESTION, offerUndo } from './actions.js';
import { ApiError, apiJson, getJson, sendJson } from './api.js';
import { shortcutLetter } from './keys.js';
import {
  KNOWN_STATUSES, MEDIA_TYPES, ROW_THUMB_W, THUMB_TYPES, activeChannels, badge, channelTone, findRow,
  icon, loadMe, make, mediaUrl, refreshRow, removeRow, selectedTab, showToast, thumbUrl,
} from './list.js';
import { state, el } from './state.js';
import { dayKey, dayLabel, hasIsoDate, hhmm, localTimeValue, relativeDay, whenText } from './time.js';

const PREVIEW_DELAY_MS = 250;
const CAPTION_LIMIT = 1024;
const UPLOAD_MAX_BYTES = 50 * 1024 * 1024;
const PANEL_THUMB_W = 512;
const STRIP_THUMB_W = 240;
const STATUS_LABELS = { queued: 'Queued', scheduled: 'Scheduled', sending: 'Sending', failed: 'Failed', posted: 'Posted' };
// Ctrl/Cmd + key in the caption editor, by the key's letter on any layout (keys.js).
const SHORTCUTS = { b: 'b', i: 'i', u: 'u', k: 'link' };
// Media types of a post sent as a file: new media for it is sent as a file too, unless
// "Send as a file" is unticked.
const SENT_AS_FILE = new Set(['document', 'document_image', 'document_video']);
const URL_SCHEME = /^[a-z][a-z0-9+.-]*:/i;
const LINK_SCHEMES = /^(https?|tg):\/\//i;
const DISCARD_QUESTION = 'Discard your unsaved changes to this post?';
const READ_ONLY_NOTES = {
  posted: 'Already posted, so it can no longer be edited. Deleting it removes this record only; '
    + 'the message stays in the channel.',
  sending: 'This post is being sent right now, so it cannot be changed.',
};
const UNKNOWN_NOTE = 'This post has an unknown status, so the board cannot change it.';

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
  const edits = keepEdits && panel.baseline
    ? { changes: changedFields(panel), file: panel.file, asFile: el.asFile.checked }
    : null;
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
  el.asFile.checked = SENT_AS_FILE.has(detail.media_type); // starts as the post is sent now
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
    if (can.media) {
      panel.file = edits.file;
      el.asFile.checked = edits.asFile; // the file keeps the way it was to be sent
    }
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
  const action = SHORTCUTS[shortcutLetter(event)];
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

// A save, upload, post now, retry or delete is in flight (a detail load is not a write).
function isWriting(panel) {
  return panel.busy !== '' && panel.busy !== 'load';
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

// What saving with no time does. A failed post stays Failed without a time (spec 6.1);
// any other post goes to, or stays in, Queued.
function noTimeHint(panel) {
  const { detail } = panel;
  if (detail.status === 'failed') {
    return detail.can.retry
      ? 'No time: saving keeps the post in Failed. Retry sends it right away.'
      : 'No time: saving keeps the post in Failed.';
  }
  return panel.baseline.time
    ? 'No time: saving moves the post to Queued.'
    : 'No time: the post waits in Queued. Pick a time to schedule it.';
}

function renderTimeHint(panel) {
  const { detail } = panel;
  const parts = [];
  if (detail.can && detail.can.edit) {
    const noTime = el.timeInput.value === '' && !timeIncomplete();
    if (noTime) parts.push(noTimeHint(panel));
    else if (detail.can.retry) parts.push('Retry sends it at this time if that is still ahead, otherwise right away.');
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
  const writing = isWriting(panel);

  // A write in flight holds the panel open and its form still, so a failure lands
  // here with every input as it was sent.
  el.panelClose.disabled = writing;
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

  el.captionInput.readOnly = !can.edit || writing;
  el.toolbar.hidden = !can.edit;
  for (const tool of el.tools) tool.disabled = writing;
  if (!can.edit || writing) closeLinkRow();
  el.channelSelect.disabled = !can.edit || writing;
  el.timeInput.disabled = !can.edit || writing;
  el.unschedule.hidden = !can.edit;
  el.unschedule.disabled = writing || (el.timeInput.value === '' && !timeIncomplete());
  el.mediaReplace.hidden = !can.media;
  el.mediaInput.disabled = writing;
  el.mediaUnstage.disabled = writing;
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
    // The panel cannot be closed during a write, so only signing out gets here:
    // never show this answer in a panel that holds another post.
    showToast(message);
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
// leaves the time out, it sets its own), then the chosen file. Everything it sends
// is read from the form here, before the first await, and never again. The
// baseline follows each step, so a later failure never sends a saved change twice.
// Resolves to the server's notice about the upload ('' when there is none).
async function saveChanges(panel, { withTime }) {
  const postId = panel.id;
  const changes = changedFields(panel);
  if (!withTime) delete changes.scheduled_time;
  const { file } = panel;
  const asFile = el.asFile.checked;
  if (Object.keys(changes).length > 0) {
    panel.detail = await sendJson(`/api/posts/${postId}`, 'PATCH', changes);
    if ('caption' in changes) panel.baseline.caption = changes.caption;
    if ('channel_id' in changes) panel.baseline.channel = changes.channel_id;
    if ('scheduled_time' in changes) panel.baseline.time = changes.scheduled_time || '';
  }
  if (!file) return '';
  const form = new FormData();
  form.append('as_file', asFile ? '1' : '0'); // before the file: the server reads it only there
  form.append('file', file, file.name);
  const { notice, ...detail } = await apiJson(`/api/posts/${postId}/media`, { method: 'PUT', body: form }, 'complete');
  panel.detail = detail;
  if (panel.file === file) panel.file = null;
  mediaReplaced(detail);
  // Said when a photo too big for Telegram was stored as a file (spec 6.6).
  return typeof notice === 'string' ? notice : '';
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
    offerUndo([before], 'Post deleted.'); // Undo puts it back (POST /api/posts/{id}/restore)
  });
}

// --- panel: closing

// Close the panel (Esc, the scrim, Close), asking first when there are unsaved
// changes. While a write is in flight it stays open, so a failure is shown here
// with the input intact; `force` is for the panel's own closing after an action.
// Returns whether it closed.
function closePanel({ force = false } = {}) {
  const panel = state.panel;
  if (!panel) return true;
  if (!force) {
    if (isWriting(panel)) return false;
    if (isDirty(panel) && !window.confirm(DISCARD_QUESTION)) return false;
  }
  dropPanel();
  returnFocus(panel);
  return true;
}

// Hide the panel and give the page behind it back.
export function dropPanel() {
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

export function bindPanel() {
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

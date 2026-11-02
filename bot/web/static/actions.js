// Acting on posts from the list: selecting rows and the bulk bar (Delete, Move to
// channel, Shift), deleting with Undo, and the summary of the posts a bulk action skipped.
//
// A selection lasts as long as the view (tab, channel, search): a refresh keeps it,
// another view starts with none. It holds at most BULK_MAX posts, the server's limit for
// one bulk request. Every delete, from a row, the edit panel or the bar, takes the rows
// out at once and shows a toast with Undo for 10 seconds; Undo restores the same posts
// (POST /api/posts/{id}/restore after a single delete, bulk restore with the same ids
// after a bulk delete). A restored post can come back to another tab than the one it
// left (a pending post whose time has passed returns to Queued), so the list and the
// counts are fetched again after it.
import { ApiError, sendJson } from './api.js';
import {
  TAB_OF_STATUS, activeChannels, announce, findRow, loadMe, make, refreshAll, removeRow, rowItem, rowLabel,
  selectedTab, showToast,
} from './list.js';
import { state, el } from './state.js';

const BULK_MAX = 500;
const LIMIT_REFUSED = `A selection holds up to ${BULK_MAX} posts, the most the board changes at once.`;
const LIMIT_CAPPED = `Selected the first ${BULK_MAX} posts shown: the board changes up to ${BULK_MAX} at once.`;
const GONE_MESSAGE = 'This post no longer exists; it may have been deleted elsewhere.';
export const POSTED_DELETE_QUESTION = 'Delete this posted post from the board? '
  + 'Removes this record only; the message stays in the channel.';
const SHIFT_NOTE = 'Moves every selected post by this many hours or days; a negative number moves them earlier.';
const SHIFT_SCHEDULED_ONLY = 'Only scheduled posts can be shifted. Open the Scheduled tab to shift posts.';
const MOVE_POSTED = 'A posted post stays in the channel it was posted to.';
const UNIT_WORDS = { hours: ['hour', 'hours'], days: ['day', 'days'] };
const DONE_WORDS = { delete: 'deleted', restore: 'restored', move_channel: 'moved', shift: 'shifted' };

let moveChoicesKey = '';

function posts(count) {
  return `${count} ${count === 1 ? 'post' : 'posts'}`;
}

function errorMessage(error) {
  if (!(error instanceof ApiError)) console.error(error); // a bug in this page, not a network or server problem
  return error instanceof ApiError ? error.message : 'Something went wrong on this page. Try again.';
}

function quietFailure(error) {
  return error.name === 'AbortError' || error.status === 401; // 401: the signed-out view says it all
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

function tabName(tab) {
  const button = el.tabs.find((candidate) => candidate.dataset.tab === tab);
  return button ? button.querySelector('.tab-label').textContent : tab;
}

// Focus was on something that is gone or hidden now (a bar button, an Undo button).
function focusLost(container = null) {
  const active = document.activeElement;
  return !active || active === document.body || !active.isConnected || Boolean(container && container.contains(active));
}

// --- selection

function rowBox(rowNode) {
  return rowNode.querySelector('.row-select');
}

function selectableRows() {
  return [...el.list.querySelectorAll('.post-row')].filter((rowNode) => !rowBox(rowNode).disabled);
}

function setSelected(rowNode, selected) {
  const id = Number(rowNode.dataset.id);
  rowBox(rowNode).checked = selected;
  if (selected) state.selected.set(id, rowItem(rowNode));
  else state.selected.delete(id);
}

function onSelectChange(event) {
  const box = event.target;
  if (!(box instanceof HTMLInputElement) || !box.classList.contains('row-select')) return;
  if (box.checked && state.selected.size >= BULK_MAX) {
    box.checked = false;
    showToast(LIMIT_REFUSED);
    return;
  }
  const first = state.selected.size === 0;
  setSelected(box.closest('.post-row'), box.checked);
  renderBar();
  if (first && box.checked) announce('1 selected. The actions for the selection follow the list.');
}

function selectAllShown() {
  let capped = false;
  for (const rowNode of selectableRows()) {
    if (rowBox(rowNode).checked) continue;
    if (state.selected.size >= BULK_MAX) {
      capped = true;
      break;
    }
    setSelected(rowNode, true);
  }
  renderBar();
  if (capped) showToast(LIMIT_CAPPED);
  else announce(`${state.selected.size} selected.`);
}

function clearSelection() {
  for (const rowNode of el.list.querySelectorAll('.post-row')) rowBox(rowNode).checked = false;
  state.selected.clear();
  renderBar();
}

// list.js's rowsChanged hook: after rows were drawn again, added or taken out, the
// selection keeps the posts still shown and selectable, as their rows show them now.
export function syncSelection() {
  for (const id of [...state.selected.keys()]) {
    const rowNode = findRow(id);
    if (rowNode && !rowBox(rowNode).disabled) state.selected.set(id, rowItem(rowNode));
    else state.selected.delete(id);
  }
  renderBar();
}

// --- the bar

function renderMoveChoices() {
  const channels = activeChannels();
  const key = JSON.stringify(channels.map((channel) => [channel.channel_id, channel.channel_name]));
  if (key === moveChoicesKey) return;
  moveChoicesKey = key;
  const chosen = el.bulkMove.value;
  const options = channels.map((channel) => {
    const option = make('option', '', channel.channel_name || channel.channel_id);
    option.value = channel.channel_id;
    return option;
  });
  el.bulkMove.replaceChildren(el.bulkMove.options[0], ...options); // keeps "Move to…"
  el.bulkMove.value = channels.some((channel) => channel.channel_id === chosen) ? chosen : '';
}

// The shift as a whole, non-zero number of hours or days; null when the field holds none.
function shiftAmount() {
  const text = el.bulkShiftAmount.value.trim();
  if (!/^-?\d{1,5}$/.test(text)) return null;
  const amount = Number(text);
  return amount === 0 ? null : amount;
}

// Show the bar while posts are selected and enable what the selection allows: Move
// everywhere but Posted, Shift on Scheduled only (spec 6.7).
function renderBar() {
  const count = state.selected.size;
  el.bulkBar.hidden = count === 0;
  document.body.classList.toggle('has-selection', count > 0);
  if (count === 0) return;
  const busy = state.bulkBusy !== '';
  setText(el.bulkCount, `${count} selected`);
  // At the limit the button says so, instead of offering more.
  const full = count >= BULK_MAX;
  el.bulkSelectAll.disabled = busy || full || selectableRows().every((rowNode) => rowBox(rowNode).checked);
  setText(el.bulkSelectAll, full ? `Up to ${BULK_MAX} at once` : 'Select all shown');
  renderMoveChoices();

  el.bulkDelete.disabled = busy;
  setText(el.bulkDeleteLabel, state.bulkBusy === 'delete' ? 'Deleting…' : 'Delete');

  const movable = state.tab !== 'posted';
  el.bulkMove.disabled = busy || !movable;
  el.bulkMoveApply.disabled = el.bulkMove.disabled || el.bulkMove.value === '';
  setText(el.bulkMoveApply, state.bulkBusy === 'move_channel' ? 'Moving…' : 'Move');
  el.bulkMoveGroup.title = movable ? '' : MOVE_POSTED;

  const shiftable = state.tab === 'scheduled';
  el.bulkShiftAmount.disabled = busy || !shiftable;
  el.bulkShiftUnit.disabled = busy || !shiftable;
  el.bulkShiftApply.disabled = busy || !shiftable || shiftAmount() === null;
  setText(el.bulkShiftApply, state.bulkBusy === 'shift' ? 'Shifting…' : 'Shift');
  el.bulkShiftAmount.setAttribute('aria-invalid', String(shiftable && shiftAmount() === null));
  el.bulkShiftGroup.title = shiftable ? '' : SHIFT_SCHEDULED_ONLY;
  setText(el.bulkShiftNote, shiftable ? SHIFT_NOTE : SHIFT_SCHEDULED_ONLY);
}

// When the bar went away with focus in it, focus moves to the summary when it is up,
// else to the tab.
function focusAfterBar() {
  if (state.panel || !focusLost(el.bulkBar)) return;
  (el.bulkSummary.hidden ? selectedTab() : el.bulkSummary).focus();
}

// Run one bulk action on the selection. `after(doneIds)` brings the list up to date and
// returns what to say about the posts that changed ('' for none). Posts the server
// skipped are listed in the summary with its reasons; the rest of the batch stands.
async function runBulk(action, extra, after) {
  if (state.bulkBusy || state.selected.size === 0 || state.signedOut) return;
  const items = new Map(state.selected);
  const ids = [...items.keys()];
  hideSummary(); // it was about the previous action
  state.bulkBusy = action;
  renderBar();
  let results;
  try {
    ({ results } = await sendJson('/api/posts/bulk', 'POST', { action, ids, ...extra }));
  } catch (error) {
    state.bulkBusy = '';
    renderBar(); // the selection stays, so the action can be tried again
    if (!quietFailure(error)) showToast(errorMessage(error));
    return;
  }
  state.bulkBusy = '';
  // The posts sent leave the selection, changed or skipped; any picked meanwhile stay.
  for (const id of ids) {
    state.selected.delete(id);
    const rowNode = findRow(id);
    if (rowNode) rowBox(rowNode).checked = false;
  }
  renderBar();
  const done = results.filter((result) => result.ok).map((result) => result.id);
  const skipped = results.filter((result) => !result.ok);
  const said = await after(done, items);
  if (skipped.length > 0) showSummary(said || `No posts were ${DONE_WORDS[action]}.`, skipped, items);
  else if (said && action !== 'delete') showToast(said); // a delete's toast carries Undo, see offerUndo
  focusAfterBar();
}

function postedBulkQuestion(count) {
  return `Delete ${count} posted posts from the board? `
    + 'Removes these records only; the messages stay in the channel.';
}

function bulkDelete() {
  if (el.bulkDelete.disabled) return;
  const selected = [...state.selected.values()];
  // A posted post's message stays in the channel; only the board's record goes. Say so first.
  const posted = selected.filter((item) => item.status === 'posted').length;
  if (posted > 0) {
    const question = selected.length === 1 ? POSTED_DELETE_QUESTION : postedBulkQuestion(posted);
    if (!window.confirm(question)) return;
  }
  runBulk('delete', {}, (done, items) => {
    for (const id of done) removeRow(id);
    loadMe();
    if (done.length === 0) return '';
    const said = done.length === 1 ? 'Post deleted.' : `Deleted ${posts(done.length)}.`;
    offerUndo(done.map((id) => items.get(id)), said, { bulk: true });
    return said;
  });
}

function bulkMove() {
  if (el.bulkMoveApply.disabled) return;
  const channelId = el.bulkMove.value;
  const channel = activeChannels().find((candidate) => candidate.channel_id === channelId);
  const name = channel ? channel.channel_name || channel.channel_id : channelId;
  runBulk('move_channel', { channel_id: channelId }, async (done) => {
    el.bulkMove.value = '';
    await refreshAll(); // rows on another channel may leave a filtered list
    return done.length > 0 ? `Moved ${posts(done.length)} to ${name}.` : '';
  });
}

function bulkShift() {
  const amount = shiftAmount();
  if (el.bulkShiftApply.disabled || amount === null) return;
  const unit = el.bulkShiftUnit.value;
  runBulk('shift', { shift: { unit, amount } }, async (done) => {
    await refreshAll(); // shifted rows move to other days
    if (done.length === 0) return '';
    const size = Math.abs(amount);
    const words = UNIT_WORDS[unit] || [unit, unit];
    return `Shifted ${posts(done.length)} ${size} ${words[size === 1 ? 0 : 1]} ${amount > 0 ? 'later' : 'earlier'}.`;
  });
}

// --- the summary of skipped posts

function showSummary(said, skipped, items) {
  const count = skipped.length;
  setText(el.bulkSummaryTitle, `${said} ${count === 1 ? '1 post was' : `${count} posts were`} skipped:`);
  el.bulkSkipped.replaceChildren(...skipped.map((result) => {
    const item = items.get(result.id);
    const entry = make('li', 'skipped-post');
    entry.append(
      make('span', 'skipped-label', item ? rowLabel(item) : `Post ${result.id}`),
      make('span', 'skipped-reason', result.error || 'It was not changed.'),
    );
    return entry;
  }));
  el.bulkSkipped.scrollTop = 0;
  el.bulkSummary.hidden = false;
  announce(el.bulkSummaryTitle.textContent);
}

function hideSummary() {
  if (el.bulkSummary.hidden) return;
  const hadFocus = el.bulkSummary.contains(document.activeElement);
  el.bulkSummary.hidden = true;
  el.bulkSkipped.replaceChildren();
  if (hadFocus) (state.panel ? el.panel : selectedTab()).focus();
}

// --- deleting with Undo

// The toast after a delete: Undo restores the posts of `items` (list items or details),
// one post with its own restore, a bulk delete's posts with bulk restore (spec 6.7).
export function offerUndo(items, said, { bulk = false } = {}) {
  showToast(said, { label: 'Undo', className: 'undo', run: () => restorePosts(items, bulk) });
}

async function restorePosts(items, bulk) {
  const ids = items.map((item) => item.id);
  let said = '';
  let restored = ids;
  try {
    if (!bulk) {
      const detail = await sendJson(`/api/posts/${ids[0]}/restore`, 'POST');
      const tab = TAB_OF_STATUS[detail.status];
      said = tab && tab !== state.tab ? `Post restored to ${tabName(tab)}.` : 'Post restored.';
    } else {
      const { results } = await sendJson('/api/posts/bulk', 'POST', { action: 'restore', ids });
      restored = results.filter((result) => result.ok).map((result) => result.id);
      const skipped = results.filter((result) => !result.ok);
      if (restored.length > 0) said = `Restored ${posts(restored.length)}.`;
      if (skipped.length > 0) {
        showSummary(said || 'No posts were restored.', skipped, new Map(items.map((item) => [item.id, item])));
      }
    }
  } catch (error) {
    if (quietFailure(error)) return;
    showToast(errorMessage(error));
    if (error.status === 404 || error.status === 409) refreshAll(); // show the posts as they are now
    return;
  }
  await refreshAll(); // a restored post may belong to another tab now
  const away = bulk ? restored.filter((id) => !findRow(id)).length : 0;
  if (!said) said = 'No posts were restored.';
  else if (away > 0) said += ` ${away === 1 ? '1 of them is' : `${away} of them are`} on another tab now.`;
  showToast(said); // replaces the toast whose Undo was used
  if (state.panel || !focusLost()) return;
  const first = restored.map(findRow).find(Boolean);
  (first || (el.bulkSummary.hidden ? selectedTab() : el.bulkSummary)).focus();
}

// The row's delete button: no question unless the post was already posted.
async function deleteFromRow(button) {
  const rowNode = button.closest('.post-row');
  const item = rowNode ? rowItem(rowNode) : null;
  if (!item || button.disabled || state.signedOut) return;
  if (item.status === 'posted' && !window.confirm(POSTED_DELETE_QUESTION)) return;
  button.disabled = true;
  try {
    await sendJson(`/api/posts/${item.id}`, 'DELETE');
  } catch (error) {
    if (button.isConnected) button.disabled = false;
    rowDeleteFailed(item, error);
    return;
  }
  removeRow(item.id);
  loadMe();
  offerUndo([item], 'Post deleted.');
}

function rowDeleteFailed(item, error) {
  if (quietFailure(error)) return;
  if (error.status === 404) {
    removeRow(item.id);
    loadMe();
    showToast(GONE_MESSAGE);
    return;
  }
  showToast(errorMessage(error));
  if (error.status === 409) refreshAll(); // the post changed (it is being sent, say): show it as it is now
}

export function bindActions() {
  el.list.addEventListener('change', onSelectChange);
  el.list.addEventListener('click', (event) => {
    const button = event.target instanceof Element ? event.target.closest('.row-delete') : null;
    if (button) deleteFromRow(button);
  });
  el.bulkSelectAll.addEventListener('click', selectAllShown);
  el.bulkClear.addEventListener('click', () => {
    clearSelection();
    focusAfterBar();
  });
  el.bulkDelete.addEventListener('click', bulkDelete);
  el.bulkMove.addEventListener('change', renderBar);
  el.bulkMoveApply.addEventListener('click', bulkMove);
  el.bulkShiftAmount.addEventListener('input', renderBar);
  el.bulkShiftAmount.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') bulkShift();
  });
  el.bulkShiftApply.addEventListener('click', bulkShift);
  el.bulkSummaryClose.addEventListener('click', hideSummary);
  el.bulkSummary.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    event.stopPropagation(); // Esc here closes the summary, not an open edit panel
    hideSummary();
  });
}

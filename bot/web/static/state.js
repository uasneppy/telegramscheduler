// The board's shared state: two objects every module imports. `state` is the view
// (tab, filters), the list, the selection and the open edit panel; `el` holds the
// page's elements and is filled in by main.js once the page is ready. They are
// objects, not re-assigned `let`s, because a module cannot assign to a binding it imported.

export const state = {
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
  toastMs: 0,        // how long the toast shown now stays
  announceTimer: 0,
  // The selected posts: id -> the list item as its row last showed it, in the order they
  // were selected. A refresh keeps it; another tab, channel or search clears it.
  selected: new Map(),
  bulkBusy: '',      // the bulk action in flight ('delete', 'move_channel', 'shift'), else ''
  panel: null,       // the open edit panel (see openPanel); null while it is closed
  // Post id -> a counter bumped each time the board replaces the post's media. It
  // goes into thumbnail URLs: a thumbnail is cached for an hour under the post's
  // URL, so the same URL would keep showing the old picture.
  thumbVersions: new Map(),
};

export const el = {};

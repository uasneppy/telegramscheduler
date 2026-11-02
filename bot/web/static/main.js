// Post board: tabs, filters, the post list and the edit panel.
//
// Plain browser JavaScript, no framework and no build step. Text from the API is
// only ever written with textContent or setAttribute; the one exception is the
// server's sanitized caption preview HTML (setPreviewHtml). Times are cut out of
// the server's Kyiv ISO strings ("2026-11-02T10:00:00+02:00") and sent back as
// Kyiv wall-clock "YYYY-MM-DDTHH:MM"; the browser's own time zone is never
// consulted. The view (tab, channel, search) lives in the URL hash, e.g.
// #tab=scheduled&channel=-1001&q=news.
//
// The code is ES modules, loaded from this file (no bundler):
//   state.js  the shared `state` and `el` objects
//   api.js    fetch with the board's rules, the JSON helpers, ApiError
//   time.js   Kyiv ISO strings -> the times and days shown
//   list.js   tabs, filters, hash, rows, loading, counts, banner, toast
//   panel.js  the edit panel
//   main.js   this file: wires the modules together and starts the board
import { setSignedOutHandler } from './api.js';
import { bindControls, hooks, readHash, refreshAll, showSignedOut, syncControls, writeHash } from './list.js';
import { bindPanel, dropPanel } from './panel.js';
import { el } from './state.js';

setSignedOutHandler(showSignedOut);
hooks.dropPanel = dropPanel;

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
    tools: [...document.querySelectorAll('#caption-toolbar [data-fmt]')],
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

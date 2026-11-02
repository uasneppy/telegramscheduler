// The board's API calls: fetch with the board's rules, and the JSON helpers on top.
// This module knows nothing of the page. What a 401 does (swap in the signed-out
// view) is registered by main.js through setSignedOutHandler.

let signedOutHandler = () => {};

export function setSignedOutHandler(handler) {
  signedOutHandler = handler;
}

export class ApiError extends Error {
  constructor(status, message, body = null, { unreadable = false } = {}) {
    super(message);
    this.status = status; // 0 for a network failure
    this.body = body;     // the JSON error body, when there was one (a 409 carries `status`)
    // A 2xx whose body could not be read (the connection dropped mid-answer, say): the
    // server did act, but what it did is unknown.
    this.unreadable = unreadable;
  }
}

// fetch with the board's rules: X-Board on every state-changing request, and a
// 401 from anywhere swaps the page for the signed-out view.
export async function request(path, options = {}) {
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
    signedOutHandler();
    throw new ApiError(401, 'Not signed in.');
  }
  return response;
}

// The JSON answer of a request; anything else is an ApiError carrying the server's
// message (for a 4xx) and its error body. `verb` words the message for a 5xx. A 2xx
// whose body cannot be read is an ApiError marked `unreadable`.
export async function apiJson(path, options = {}, verb = 'load') {
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
    throw new ApiError(response.status, message, body, { unreadable: response.ok });
  }
  return body;
}

export function getJson(path, signal) {
  return apiJson(path, { signal });
}

// A state-changing call with an optional JSON body (request() adds X-Board).
export function sendJson(path, method, body) {
  const options = { method };
  if (body !== undefined) {
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify(body);
  }
  return apiJson(path, options, 'complete');
}

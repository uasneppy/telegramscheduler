// Times. All of them are cut out of the server's Kyiv ISO strings
// ("2026-11-02T10:00:00+02:00"); the browser's own time zone is never consulted.
import { state } from './state.js';

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const RELATIVE_DAYS = { '-1': 'Yesterday', 0: 'Today', 1: 'Tomorrow' };

export function hasIsoDate(iso) {
  return typeof iso === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(iso);
}

// "2026-11-02" from a Kyiv ISO string; '' when there is no usable time.
export function dayKey(iso) {
  return hasIsoDate(iso) ? iso.slice(0, 10) : '';
}

export function hhmm(iso) {
  return iso.slice(11, 16);
}

function dayNumber(key) {
  const [year, month, day] = key.split('-').map(Number);
  return Date.UTC(year, month - 1, day) / 86400000;
}

// "Mon 02.11"; the weekday comes from the date parts alone, in UTC.
export function dayLabel(key) {
  if (!key) return 'No date';
  const weekday = WEEKDAYS[new Date(dayNumber(key) * 86400000).getUTCDay()];
  return `${weekday} ${key.slice(8, 10)}.${key.slice(5, 7)}`;
}

// "Today", "Tomorrow" or "Yesterday" by the server's Kyiv clock, else ''.
export function relativeDay(key) {
  if (!key || !state.me || !hasIsoDate(state.me.now)) return '';
  return RELATIVE_DAYS[dayNumber(key) - dayNumber(dayKey(state.me.now))] || '';
}

// "Mon 02.11 10:00" ("Today 10:00" when it is today in Kyiv), from a Kyiv ISO string.
export function whenText(iso) {
  if (!hasIsoDate(iso)) return '';
  const key = dayKey(iso);
  return `${relativeDay(key) || dayLabel(key)} ${hhmm(iso)}`;
}

// The value of a datetime-local input for a Kyiv ISO string: its wall-clock
// "YYYY-MM-DDTHH:MM", exactly as the server stored it; '' when there is no time.
export function localTimeValue(iso) {
  return hasIsoDate(iso) ? iso.slice(0, 16) : '';
}

// Keyboard shortcuts on any keyboard layout.
//
// A Ctrl/Cmd shortcut is named by a Latin letter (Ctrl+Z, Ctrl+B), but `event.key` is
// the letter the layout types: on the Ukrainian layout Ctrl+Z arrives as key 'я', code
// 'KeyZ'. So the letter comes from `event.key` when that is a Latin letter (QWERTY,
// AZERTY, Dvorak: the key printed Z is Ctrl+Z wherever it sits), and from the key's
// position, `event.code`, only when the layout types a letter of another alphabet
// there (Cyrillic, Greek, Hebrew…). A key that types a digit or a sign (Dvorak's ';'
// sits where QWERTY has Z) names no shortcut.

const LATIN_LETTER = /^[a-z]$/i;
const LETTER_KEY = /^Key([A-Z])$/;
const LETTERS = /^[\p{L}\p{M}]+$/u;

// What a layout of another alphabet types: letters (and their marks), none of them Latin
// A-Z. Named keys such as 'Process' or 'Unidentified' are spelled in Latin letters, so
// they do not count.
function letterOfAnotherAlphabet(key) {
  return LETTERS.test(key) && !/[a-z]/i.test(key);
}

// The lowercase Latin letter a key press stands for in a shortcut, or '' for none.
export function shortcutLetter(event) {
  const key = typeof event.key === 'string' ? event.key : '';
  if (LATIN_LETTER.test(key)) return key.toLowerCase();
  const position = LETTER_KEY.exec(typeof event.code === 'string' ? event.code : '');
  return position && letterOfAnotherAlphabet(key) ? position[1].toLowerCase() : '';
}

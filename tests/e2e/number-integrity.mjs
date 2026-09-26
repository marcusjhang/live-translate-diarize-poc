// Unit test for the client's number-integrity and translation-quality guards.
// Pulls the real functions out of static/index.html so the test cannot drift from
// what ships, and runs without a browser, a server, or an API key.
//
//   node tests/e2e/number-integrity.mjs
//
// Exit code 0 = all cases pass.

import fs from 'node:fs';
import path from 'node:path';

const html = fs.readFileSync(new URL('../../static/index.html', import.meta.url), 'utf8');

// Everything from the number tables down to (but not including) renderPanel is a
// self-contained block of pure functions — the same code the page runs.
const start = html.indexOf('var NUMBER_WORDS');
const end = html.indexOf('function renderPanel');
if (start < 0 || end < 0 || end <= start) {
  console.error('could not locate the guard functions in static/index.html');
  process.exit(2);
}
const block = html.slice(start, end);
const mod = await import('data:text/javascript,' + encodeURIComponent(
  block + '\nexport { numbersIn, missingNumbers, numberMismatch, looksUnreliable, toSimplified };'
));
const { missingNumbers, numberMismatch, looksUnreliable, toSimplified } = mod;

let pass = 0, fail = 0;
function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  ok ? pass++ : fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : `  got=${JSON.stringify(got)} want=${JSON.stringify(want)}`}`);
}

// --- number integrity -----------------------------------------------------
// A spoken number that did not survive translation must be named.
const NUM = [
  ['decimal written 38度五',      'My body temperature is 38.5.',                 '我的体温是38度五。',            false],
  ['decimal written 38度5',       'My body temperature is 38.5.',                 '我的体温是38度5。',             false],
  ['reverse decimal',             '我的体温是38度5。',                             'My body temperature is 38.5.',  false],
  ['units + frequency preserved', 'Take 500 milligrams three times a day for seven days.', '服用 500毫克，每天三次，连服七天。', false],
  ['frequency dropped',           'Take 500 milligrams three times a day for seven days.', '每天服用500毫克,连服七天。',     true ],
  ['dose duration dropped',       'Take one tablet twice a day for five days.',    '每天服用一片，连服五天。',        true ],
  ['negation, no numbers',        'Do not take this on an empty stomach.',         '请不要空腹服用。',              false],
  ['question, no numbers',        'Does it hurt when you breathe?',                '你呼吸时会痛吗？',              false],
  ['wrong number',                'Yesterday my temperature was 38.5',             '昨天我的体温是37度',            true ],
  ['article "one" is not counted','Come back in one week if the fever continues.', '如果一周后发烧还没好，就回来。', false],
];
for (const [name, src, tgt, want] of NUM) check(`number: ${name}`, numberMismatch(src, tgt), want);

// The missing values must be identified, not just counted.
check('number: names the dropped value',
  missingNumbers('Take 500 milligrams three times a day for seven days.', '每天服用500毫克,连服七天。'), ['3']);

// --- collapsed / transliterated output ------------------------------------
// The failure mode both users said they could not live with: a whole clause
// emitted as one bare word instead of translated.
const UNREL = [
  ['collapsed to one word',   'Me duele la cabeza.',                    'Midway',                        true ],
  ['proper English',          'Me duele la cabeza.',                    'I have a headache.',            false],
  ['CJK target is exempt',    'I have a cough and fever for five days.','我咳嗽发烧已经五天了。',        false],
  ['Spanish target',          'Where does it hurt?',                    '¿Dónde te duele?',              false],
  ['short but complete',      'Show me where it hurts.',                '告诉我哪里疼。',                false],
  ['empty translation',       'Do you have a fever?',                   '',                              false],
];
for (const [name, src, tgt, want] of UNREL) check(`unreliable: ${name}`, looksUnreliable(src, tgt), want);

// --- Traditional -> Simplified --------------------------------------------
const SIMP = [
  ['mixed script normalised', '昨天兩體溫有38度5', '昨天两体温有38度5'],
  ['clinical characters',     '這個藥一天要吃幾次', '这个药一天要吃几次'],
  ['unchanged when already simplified', '这个药一天要吃几次', '这个药一天要吃几次'],
];
for (const [name, src, want] of SIMP) check(`simplified: ${name}`, toSimplified(src), want);

console.log(`\n${pass}/${pass + fail} cases pass`);
process.exit(fail === 0 ? 0 : 1);

import fs from 'node:fs';

// --- harness paths -------------------------------------------------------
// The CDP endpoint of a Superset browser pane carries an auth token, so it is
// never committed. Point CDP_FILE at a file holding that URL, e.g.
//   superset browser cdp --workspace <id> --pane <paneId> --json | jq -r .url > /tmp/cdp.txt
// Fixtures come from `python3 tests/e2e/gen-clips.py`.
const CDP_FILE = process.env.CDP_FILE || "/tmp/cdp.txt";
const CLIPS = process.env.CLIPS || new URL("./clips/", import.meta.url).pathname;
// ------------------------------------------------------------------------


const cdpUrl = fs.readFileSync(CDP_FILE, 'utf8').trim();
const ws = new WebSocket(cdpUrl);
let id = 0; const pending = new Map();
function send(m, p = {}) { return new Promise((res, rej) => { const i = ++id; const t = setTimeout(() => { pending.delete(i); res({ __timeout: true }); }, 15000); pending.set(i, { res: (v) => { clearTimeout(t); res(v); }, rej: (e) => { clearTimeout(t); rej(e); } }); ws.send(JSON.stringify({ id: i, method: m, params: p })); }); }
ws.addEventListener('message', (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); } });
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
await send('Page.enable'); await send('Runtime.enable'); await send('Page.bringToFront');

// Preflight: a closed or stale pane must fail immediately and clearly. Without this
// the harness degrades into 15s timeouts per call and appears to hang for half an hour.
{
  const probe = await send('Runtime.evaluate', { expression: '1+1', returnByValue: true });
  if (!probe || probe.__timeout || !probe.result) {
    console.error('CDP endpoint is not answering. Is the browser pane still open?');
    console.error('See tests/e2e/README.md -> "Get the CDP endpoint".');
    process.exit(2);
  }
}
const ev = async (e, a = false) => { const r = await send('Runtime.evaluate', { expression: e, returnByValue: true, awaitPromise: a }); return r.exceptionDetails ? 'ERR' : (r && r.result ? r.result.value : 'TIMEOUT'); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const b64 = (p) => fs.readFileSync(`${CLIPS}${p}.wav`).subarray(44).toString('base64');
const btn = (s) => `document.querySelector('.panel[data-role="${s}"] button.talk')`;

const S = [
  { n: '01 single doctor turn (en->zh)', langs: ['en', 'zh'],
    turns: [['doctor', 's01-d1', 'Show me where it hurts.']],
    want: { handover: true, noError: true, lines: 2 } },

  { n: '02 single patient turn (zh->en)', langs: ['en', 'zh'],
    turns: [['patient', 's02-p1', '我这里一直疼。']],
    want: { handover: true, noError: true, lines: 2 } },

  { n: '03 long utterance (20 words)', langs: ['en', 'zh'],
    turns: [['doctor', 's04-d1', 'Tell me about the pain, when it started, what makes it worse, and whether it spreads anywhere else.']],
    want: { handover: true, noError: true } },

  { n: '04 very short utterance', langs: ['en', 'zh'],
    turns: [['doctor', 's05-d1', 'Any pain?']],
    want: { handover: true, noError: true, question: true } },

  { n: '05 same speaker twice in a row', langs: ['en', 'zh'],
    turns: [['doctor', 's06-d1', 'How long has this been going on?'], ['doctor', 's06-d2', 'And does it hurt at night?']],
    want: { handover: true, noError: true, lines: 4 } },

  { n: '06 dosage numbers (500mg x3 x7d)', langs: ['en', 'zh'],
    turns: [['doctor', 's07-d1', 'Take 500 milligrams three times a day for seven days.']],
    want: { handover: true, noError: true, expectFlag: false } },

  { n: '07 decimal number in Chinese (38度5)', langs: ['en', 'zh'],
    turns: [['patient', 's08-p1', '我的体温是38度5。']],
    want: { handover: true, noError: true, expectFlag: false } },

  { n: '08 negation ("do not")', langs: ['en', 'zh'],
    turns: [['doctor', 's09-d1', 'Do not take this on an empty stomach.']],
    want: { handover: true, noError: true, containsAny: ['不要', '别', '不能', '勿', '不得'] } },

  { n: '09 question stays a question', langs: ['en', 'zh'],
    turns: [['doctor', 's10-d1', 'Does it hurt when you breathe?']],
    want: { handover: true, noError: true, question: true } },

  { n: '10 thinking pause mid-sentence (2s)', langs: ['en', 'zh'],
    turns: [['doctor', 's11-d1', 'I want to ask you ... about your sleep.']],
    want: { handover: true, noError: true } },

  { n: '11 rapid double toggle', langs: ['en', 'zh'], rapid: true,
    turns: [['doctor', 's12-d1', 'I am going to examine you now.']],
    want: { handover: true, noError: true } },

  { n: '12 english -> spanish', langs: ['en', 'es'],
    turns: [['doctor', 's13-d1', 'Where does it hurt?']],
    want: { handover: true, noError: true, containsAny: ['duele', 'dolor', '¿'] } },

  { n: '13 spanish -> english', langs: ['en', 'es'],
    turns: [['patient', 's13-p1', 'Me duele la cabeza.']],
    want: { handover: true, noError: true, containsAny: ['head', 'ache', 'hurts', 'pain'] } },

  { n: '14 same language both sides (en/en)', langs: ['en', 'en'],
    turns: [['doctor', 's14-d1', 'Do you have a fever?']],
    want: { handover: true, noError: true } },

  { n: '15 long silence before speaking (4s)', langs: ['en', 'zh'],
    turns: [['doctor', 's15-d1', 'Hello there.']],
    want: { handover: true, noError: true }, preSilence: true },
];

const results = [];

async function reset(langs) {
  await ev(`location.href='http://localhost:8000'`); await sleep(2000);
  // The app persists the language pair in localStorage and then SKIPS the setup
  // screen entirely, so clear it to exercise the real setup path each scenario.
  await ev(`try{localStorage.removeItem('interpret.cfg')}catch(e){}; location.reload(); 'cleared'`);
  await sleep(2400);
  await ev(`(async function(){
    navigator.mediaDevices=navigator.mediaDevices||{};
    navigator.mediaDevices.getUserMedia=async function(){ if(window.__mic)return window.__mic.stream;
      var c=new AudioContext({sampleRate:16000}); if(c.state==='suspended')await c.resume();
      var d=c.createMediaStreamDestination(); window.__mic={ctx:c,dst:d,stream:d.stream}; return d.stream;};
    window.__say=function(b,pre){var bin=atob(b),by=new Uint8Array(bin.length);for(var i=0;i<bin.length;i++)by[i]=bin.charCodeAt(i);
      var pcm=new Int16Array(by.buffer),c=window.__mic.ctx,buf=c.createBuffer(1,pcm.length,16000),ch=buf.getChannelData(0);
      for(var i=0;i<pcm.length;i++)ch[i]=pcm[i]/32768;var s=c.createBufferSource();s.buffer=buf;s.connect(window.__mic.dst);
      s.start(c.currentTime+(pre?4:0));return 'ok';};
    var o=AudioNode.prototype.connect; AudioNode.prototype.connect=function(d){try{if(d&&d.constructor&&d.constructor.name==='AudioDestinationNode'){
      var c=this.context;if(!c.__m){var g=c.createGain();g.gain.value=0;o.call(g,d);c.__m=g;}return o.apply(this,[c.__m].concat([].slice.call(arguments,1)));}}catch(e){}
      return o.apply(this,arguments);}; return 'ok';})()`, true);
  await ev(`(function(){document.getElementById('doctor-lang').value=${JSON.stringify(langs[0])};
                       document.getElementById('patient-lang').value=${JSON.stringify(langs[1])};return 'langs set';})()`);
  await ev(`(function(){var c=document.getElementById('consent'); if(c&&!c.checked) c.click(); document.getElementById('start').click();})()`); await sleep(1500);
}

const snap = () => ev(`(function(){var d=document.querySelector('.panel[data-role="doctor"]'),p=document.querySelector('.panel[data-role="patient"]');
  return JSON.stringify({s:d.querySelector('.status').textContent.replace('Retry','').trim(),
    d:d.querySelector('.conversation').innerText, lines:document.querySelectorAll('.line').length,
    flags:document.querySelectorAll('.flag').length, db:d.querySelector('button.talk').disabled, pb:p.querySelector('button.talk').disabled});})()`);

for (const sc of S) {
  const t0 = Date.now();
  await reset(sc.langs);
  let sawError = false, allHandover = true, err = null;
  try {
    for (const [side, clip, text] of sc.turns) {
      await ev(`window.__clip=${JSON.stringify(b64(clip))}`);
      if (sc.rapid) {
        await sleep(120);
        await ev(`window.__say(window.__clip, false)`);
        await ev(`${btn(side)}.click()`); await sleep(400);
        await ev(`${btn(side)}.click()`); await sleep(400);
        await ev(`${btn(side)}.click()`);
      } else {
        const tapped = await ev(`(function(){var b=${btn(side)}; if(b.disabled) return 'DISABLED'; b.click(); return 'ok';})()`);
        if (tapped === 'DISABLED') { allHandover = false; continue; }
        // Speak only AFTER the tap: the app builds its mic AudioContext in a promise,
        // so a clip played before the tap is consumed by nothing.
        await sleep(700);
        await ev(`window.__say(window.__clip, ${sc.preSilence ? 'true' : 'false'})`);
      }
      let done = false;
      for (let k = 0; k < 90; k++) {
        await sleep(500);
        const st = await ev(`document.querySelector('.panel[data-role="doctor"] .status').textContent.replace('Retry','').trim()`);
        if (/Your turn/.test(st)) { done = true; break; }
        if (/Nothing was translated/.test(st)) { sawError = true; break; }
      }
      if (!done) allHandover = false;
    }
  } catch (e) { err = String(e).slice(0, 120); }
  const raw = await snap(); const o = JSON.parse(raw);
  const checks = [];
  if (sc.want.handover) checks.push(['handed over', allHandover]);
  if (sc.want.noError) checks.push(['no error state', !sawError]);
  if (sc.want.expectFlag === false) checks.push(['no false flag', o.flags === 0]);
  if (sc.want.lines !== undefined) checks.push([`${sc.want.lines} line(s)`, o.lines === sc.want.lines]);
  if (sc.want.question) checks.push(['question preserved', /[?？]/.test(o.d)]);
  if (sc.want.containsAny) checks.push([`contains any of ${sc.want.containsAny.join('/')}`,
      sc.want.containsAny.some(x => o.d.includes(x))]);
  checks.push(['buttons re-enabled', o.db === false && o.pb === false]);
  const failed = checks.filter(([, ok]) => !ok);
  results.push({ name: sc.n, pass: failed.length === 0, failed, secs: ((Date.now() - t0) / 1000).toFixed(0),
    flags: o.flags, lines: o.lines, transcript: o.d.replace(/\n+/g, ' | ') });
  console.log(`${failed.length === 0 ? 'PASS' : 'FAIL'}  ${sc.n}  (${((Date.now() - t0) / 1000).toFixed(0)}s, ${o.lines} lines, ${o.flags} flags)`);
  if (failed.length) console.log(`      failed: ${failed.map(([n]) => n).join('; ')}`);
  console.log(`      ${o.d.replace(/\n+/g, ' | ').slice(0, 200)}`);
}

console.log('\n================ SUMMARY ================');
const p = results.filter(r => r.pass).length;
console.log(`${p}/${results.length} scenarios pass\n`);
console.log('scenario'.padEnd(42), 'result', 'time', 'lines', 'flags');
for (const r of results) console.log(r.name.padEnd(42), (r.pass ? 'PASS' : 'FAIL').padEnd(6), r.secs.padStart(4) + 's', String(r.lines).padStart(5), String(r.flags).padStart(5));
fs.writeFileSync('/tmp/e2e-suite-a-results.json', JSON.stringify(results, null, 2));
console.log('\nfull transcripts -> /tmp/e2e/results.json');
ws.close();

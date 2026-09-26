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
function send(m, p = {}) {
  return new Promise((res, rej) => {
    const i = ++id;
    const t = setTimeout(() => { pending.delete(i); res({ __timeout: true }); }, 15000);
    pending.set(i, { res: (v) => { clearTimeout(t); res(v); }, rej: (e) => { clearTimeout(t); rej(e); } });
    ws.send(JSON.stringify({ id: i, method: m, params: p }));
  });
}
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
const ev = async (e, a = false) => { const r = await send('Runtime.evaluate', { expression: e, returnByValue: true, awaitPromise: a }); return (r && r.result ? r.result.value : 'TIMEOUT'); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const b64 = (p) => fs.readFileSync(`${CLIPS}${p}.wav`).subarray(44).toString('base64');
const btn = (s) => `document.querySelector('.panel[data-role="${s}"] button.talk')`;

const S = [
  { n: 'B01 four-turn alternation (d,p,d,p)', langs: ['en', 'zh'],
    turns: [['doctor', 'c-d1'], ['patient', 'c-p1'], ['doctor', 'c-d2'], ['patient', 'c-p2']],
    want: { handover: true, noError: true, lines: 8 } },

  { n: 'B02 eight-turn conversation', langs: ['en', 'zh'],
    turns: [['doctor', 'c-d1'], ['patient', 'c-p1'], ['doctor', 'c-d2'], ['patient', 'c-p2'],
            ['doctor', 'c-d1'], ['patient', 'c-p1'], ['doctor', 'c-d2'], ['patient', 'c-p2']],
    want: { handover: true, noError: true, lines: 16 } },

  { n: 'B03 same speaker three times', langs: ['en', 'zh'],
    turns: [['doctor', 'b03-d1'], ['doctor', 'b03-d2'], ['doctor', 'b03-d3']],
    want: { handover: true, noError: true, lines: 6 } },

  { n: 'B04 clinical units (500mg / 10ml)', langs: ['en', 'zh'],
    turns: [['doctor', 'b04-d1']], want: { handover: true, noError: true, expectFlag: false } },

  { n: 'B05 duration + interval (every 8h / 3d)', langs: ['en', 'zh'],
    turns: [['doctor', 'b05-d1']], want: { handover: true, noError: true, expectFlag: false } },

  { n: 'B06 negation + number (not more than 2)', langs: ['en', 'zh'],
    turns: [['doctor', 'b06-d1']], want: { handover: true, noError: true, expectFlag: false,
      containsAny: ['不要', '别', '不能', '勿', '不得'] } },

  { n: 'B07 enumeration (fever, cough, rash)', langs: ['en', 'zh'],
    turns: [['doctor', 'b07-d1']], want: { handover: true, noError: true } },

  { n: 'B08 two sentences in one turn', langs: ['en', 'zh'],
    turns: [['doctor', 'b08-d1']], want: { handover: true, noError: true } },

  { n: 'B09 question containing a number', langs: ['en', 'zh'],
    turns: [['doctor', 'b09-p1']], want: { handover: true, noError: true, question: true } },

  { n: 'B10 emotional / soft language', langs: ['en', 'zh'],
    turns: [['doctor', 'b10-p1']], want: { handover: true, noError: true } },

  { n: 'B11 english -> spanish (longer)', langs: ['en', 'es'],
    turns: [['doctor', 'b11-d1']], want: { handover: true, noError: true } },

  { n: 'B12 english -> filipino', langs: ['en', 'fil'],
    turns: [['doctor', 'b12-d1']], want: { handover: true, noError: true } },

  { n: 'B13 very long utterance (26 words)', langs: ['en', 'zh'],
    turns: [['doctor', 'b13-d1']], want: { handover: true, noError: true } },

  { n: 'B14 8s pause before speaking', langs: ['en', 'zh'],
    turns: [['patient', 'b14-p1']], want: { handover: true, noError: true } },

  { n: 'B15 interrupt attempt mid-turn', langs: ['en', 'zh'], interrupt: true,
    turns: [['doctor', 'c-d1']], want: { handover: true, noError: true } },
];

const results = [];

async function reset(langs) {
  await ev(`location.href='http://localhost:8000'`); await sleep(2000);
  await ev(`try{localStorage.removeItem('interpret.cfg')}catch(e){}; location.reload(); 'cleared'`);
  await sleep(2400);
  await ev(`(async function(){
    navigator.mediaDevices=navigator.mediaDevices||{};
    navigator.mediaDevices.getUserMedia=async function(){ if(window.__mic)return window.__mic.stream;
      var c=new AudioContext({sampleRate:16000}); if(c.state==='suspended')await c.resume();
      var d=c.createMediaStreamDestination(); window.__mic={ctx:c,dst:d,stream:d.stream}; return d.stream;};
    window.__say=function(b){var bin=atob(b),by=new Uint8Array(bin.length);for(var i=0;i<bin.length;i++)by[i]=bin.charCodeAt(i);
      var pcm=new Int16Array(by.buffer),c=window.__mic.ctx,buf=c.createBuffer(1,pcm.length,16000),ch=buf.getChannelData(0);
      for(var i=0;i<pcm.length;i++)ch[i]=pcm[i]/32768;var s=c.createBufferSource();s.buffer=buf;s.connect(window.__mic.dst);s.start();return 'ok';};
    var o=AudioNode.prototype.connect; AudioNode.prototype.connect=function(d){try{if(d&&d.constructor&&d.constructor.name==='AudioDestinationNode'){
      var c=this.context;if(!c.__m){var g=c.createGain();g.gain.value=0;o.call(g,d);c.__m=g;}return o.apply(this,[c.__m].concat([].slice.call(arguments,1)));}}catch(e){}
      return o.apply(this,arguments);}; return 'ok';})()`, true);
  await ev(`(function(){document.getElementById('doctor-lang').value=${JSON.stringify(langs[0])};
                       document.getElementById('patient-lang').value=${JSON.stringify(langs[1])};return 'ok';})()`);
  await ev(`(function(){var c=document.getElementById('consent'); if(c&&!c.checked) c.click(); document.getElementById('start').click();})()`);
  await sleep(1600);
}

const snap = () => ev(`(function(){var d=document.querySelector('.panel[data-role="doctor"]'),p=document.querySelector('.panel[data-role="patient"]');
  return JSON.stringify({s:d.querySelector('.status').textContent.replace('Retry','').trim(),
    d:d.querySelector('.conversation').innerText, lines:document.querySelectorAll('.line').length,
    flags:document.querySelectorAll('.flag').length, db:d.querySelector('button.talk').disabled, pb:p.querySelector('button.talk').disabled});})()`);

for (const sc of S) {
  const t0 = Date.now();
  await reset(sc.langs);
  let sawError = false, allHandover = true, interruptResult = 'n/a';
  try {
    for (const [side, clip] of sc.turns) {
      await ev(`window.__clip=${JSON.stringify(b64(clip))}`);
      const tapped = await ev(`(function(){var b=${btn(side)}; if(b.disabled) return 'DISABLED'; b.click(); return 'ok';})()`);
      if (tapped === 'DISABLED') { allHandover = false; continue; }
      await sleep(700);
      await ev(`window.__say(window.__clip)`);
      if (sc.interrupt) {
        // 1s into the doctor's turn, the other person tries to grab the mic.
        await sleep(1000);
        interruptResult = await ev(`(function(){
          var other=${btn(side === 'doctor' ? 'patient' : 'doctor')};
          var wasDisabled = other.disabled;
          other.click();
          return JSON.stringify({wasDisabled:wasDisabled, stillDisabled:other.disabled,
            doctorSpeaking:document.querySelector('.panel[data-role="doctor"] .status').textContent.replace('Retry','').trim()});
        })()`);
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
  } catch (e) { allHandover = false; }
  const raw = await snap(); let o; try { o = JSON.parse(raw); } catch { o = null; }
  if (!o) { console.log(`FAIL  ${sc.n}  (no snapshot)`); results.push({ name: sc.n, pass: false, failed: ['no snapshot'] }); continue; }
  const checks = [];
  if (sc.want.handover) checks.push(['handed over', allHandover]);
  if (sc.want.noError) checks.push(['no error state', !sawError]);
  if (sc.want.expectFlag === false) checks.push(['no flag', o.flags === 0]);
  if (sc.want.lines !== undefined) checks.push([`${sc.want.lines} lines`, o.lines === sc.want.lines]);
  if (sc.want.question) checks.push(['question preserved', /[?？]/.test(o.d)]);
  if (sc.want.containsAny) checks.push(['negation present', sc.want.containsAny.some(x => o.d.includes(x))]);
  checks.push(['buttons re-enabled', o.db === false && o.pb === false]);
  const failed = checks.filter(([, ok]) => !ok);
  results.push({ name: sc.n, pass: failed.length === 0, failed: failed.map(f => f[0]), secs: ((Date.now() - t0) / 1000).toFixed(0), flags: o.flags, lines: o.lines, transcript: o.d.replace(/\n+/g, ' | '), interrupt: interruptResult });
  console.log(`${failed.length === 0 ? 'PASS' : 'FAIL'}  ${sc.n}  (${((Date.now() - t0) / 1000).toFixed(0)}s, ${o.lines} lines, ${o.flags} flags)`);
  if (failed.length) console.log(`      failed: ${failed.map(f => f[0]).join('; ')}`);
  console.log(`      ${(o.d.replace(/\n+/g, ' | ')).slice(0, 210)}`);
  if (sc.interrupt) console.log(`      interrupt probe: ${interruptResult}`);
}

console.log('\n================ SUITE B SUMMARY ================');
console.log(`${results.filter(r => r.pass).length}/${results.length} scenarios pass`);
for (const r of results) console.log('  ', (r.pass ? 'PASS' : 'FAIL').padEnd(5), r.name.padEnd(44), r.secs + 's', 'flags=' + r.flags);
fs.writeFileSync('/tmp/e2e-suite-b-results.json', JSON.stringify(results, null, 2));
ws.close();

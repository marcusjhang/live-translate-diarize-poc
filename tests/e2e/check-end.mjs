// Checks the End-consultation flow and the footer's current shape.
//
//   node tests/e2e/check-end.mjs
//
// Replaces the old check-interpreter.mjs: the "Call interpreter" control was
// removed, and ending a consultation took its place in the footer.
//
// Exit code 0 only if every check passes.

import fs from 'node:fs';

const CDP_FILE = process.env.CDP_FILE || '/tmp/cdp.txt';
const CLIPS = process.env.CLIPS || new URL('./clips/', import.meta.url).pathname;
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

// Preflight: a closed or stale pane must fail immediately and clearly.
{
  const probe = await send('Runtime.evaluate', { expression: '1+1', returnByValue: true });
  if (!probe || probe.__timeout || !probe.result) {
    console.error('CDP endpoint is not answering. Is the browser pane still open?');
    process.exit(2);
  }
}

const ev = async (expr, awaitPromise = false) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
  if (r && r.exceptionDetails) return undefined;
  return (r && r.result ? r.result.value : 'TIMEOUT');
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const status = () => ev(`document.querySelector('.panel[data-role="doctor"] .status').textContent.replace('Retry','').trim()`);
const convo = () => ev(`document.querySelector('.panel[data-role="doctor"] .conversation').innerText`);

let pass = 0, fail = 0;
const check = (name, got, want) => {
  const ok = got === want;
  ok ? pass++ : fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : `  got=${JSON.stringify(got)} want=${JSON.stringify(want)}`}`);
};

const b64 = fs.readFileSync(`${CLIPS}s01-d1.wav`).subarray(44).toString('base64');

// --- fresh page, mic stubbed so a real turn can run without a human -----------
await ev(`location.href='http://localhost:8000'`); await sleep(2000);
await ev(`try{localStorage.removeItem('interpret.cfg')}catch(e){}; location.reload()`); await sleep(2400);
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

// --- footer shape ------------------------------------------------------------
check('footer offers exactly the three actions',
  await ev(`[...document.querySelectorAll('.panel[data-role="doctor"] .footer-actions button')].map(b=>b.textContent).join('|')`),
  'Save transcript|Change languages|End consultation');
check('"Call interpreter" is gone', await ev(`document.body.innerText.includes('Call interpreter')`), false);
check('no duplicate footer disclaimer', await ev(`/not a medical interpreter/i.test(document.querySelector('.panel[data-role="doctor"] .footer').innerText)`), false);
check('consent gate still carries the disclaimer', await ev(`/not a medical interpreter/i.test(document.getElementById('setup').innerText)`), true);

// --- one genuine turn --------------------------------------------------------
await ev(`(function(){var c=document.getElementById('consent'); if(c&&!c.checked) c.click(); document.getElementById('start').click();})()`);
await sleep(1600);
check('connected', await status(), 'Ready');
await ev(`window.__clip=${JSON.stringify(b64)}`);
await ev(`document.querySelector('.panel[data-role="doctor"] button.talk').click()`);
await sleep(700);
await ev(`window.__say(window.__clip)`);
let completed = false;
for (let i = 0; i < 60; i++) { await sleep(500); if (/Your turn/.test(await status())) { completed = true; break; } }
check('turn completed', completed, true);
const before = await convo();
check('transcript has content', before.trim().length > 0, true);

// --- cancel leaves the session alone ----------------------------------------
await ev(`document.querySelector('.end').click()`); await sleep(300);
check('End asks first', await ev(`!document.getElementById('endbox').classList.contains('hidden')`), true);
check('offers save / plain / cancel',
  await ev(`[...document.querySelectorAll('#endbox button')].map(b=>b.textContent).join('|')`),
  'Save transcript & end|End without saving|Cancel');
await ev(`document.getElementById('end-cancel').click()`); await sleep(350);
check('cancel closes the prompt', await ev(`document.getElementById('endbox').classList.contains('hidden')`), true);
check('cancel keeps the session', await status(), 'Your turn');

// --- ending ------------------------------------------------------------------
await ev(`document.querySelector('.end').click()`); await sleep(300);
await ev(`document.getElementById('end-nosave').click()`); await sleep(1200);
check('status becomes "Session ended"', await status(), 'Session ended');
check('TALK is disabled after ending', await ev(`document.querySelector('.panel[data-role="doctor"] button.talk').disabled`), true);
check('transcript survives ending', (await convo()).trim(), before.trim());
check('prompt closed', await ev(`document.getElementById('endbox').classList.contains('hidden')`), true);

// --- recovery ----------------------------------------------------------------
await ev(`document.querySelector('.langs').click()`); await sleep(2400);
check('Change languages returns to setup', await ev(`!document.getElementById('setup').classList.contains('hidden')`), true);
check('Start is gated again', await ev(`document.getElementById('start').disabled`), true);

console.log(`\n${pass}/${pass + fail} checks pass`);
ws.close();
process.exit(fail === 0 ? 0 : 1);

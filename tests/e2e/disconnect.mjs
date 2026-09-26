import fs from 'node:fs';

// --- harness paths -------------------------------------------------------
// The CDP endpoint of a Superset browser pane carries an auth token, so it is
// never committed. Point CDP_FILE at a file holding that URL, e.g.
//   superset browser cdp --workspace <id> --pane <paneId> --json | jq -r .url > /tmp/cdp.txt
// Fixtures come from `python3 tests/e2e/gen-clips.py`.
const CDP_FILE = process.env.CDP_FILE || "/tmp/cdp.txt";
const CLIPS = process.env.CLIPS || new URL("./clips/", import.meta.url).pathname;
// ------------------------------------------------------------------------

const mode = process.argv[2];
const cdpUrl = fs.readFileSync(CDP_FILE, 'utf8').trim();
const ws = new WebSocket(cdpUrl);
let id = 0; const pending = new Map();
function send(m, p = {}) { return new Promise((res, rej) => { const i = ++id; const timer = setTimeout(() => { pending.delete(i); res({ __timeout: true }); }, 15000); pending.set(i, { res: (v) => { clearTimeout(timer); res(v); }, rej: (e) => { clearTimeout(timer); rej(e); } }); ws.send(JSON.stringify({ id: i, method: m, params: p })); }); }
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
const b64 = (p) => fs.readFileSync(p).subarray(44).toString('base64');
const btn = (s) => `document.querySelector('.panel[data-role="${s}"] button.talk')`;
const st = () => ev(`document.querySelector('.panel[data-role="doctor"] .status').textContent.replace('Retry','').trim()`);

await ev(`location.href='http://localhost:8000'`); await sleep(2200);
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
await ev(`(function(){var c=document.getElementById('consent'); if(c&&!c.checked) c.click(); document.getElementById('start').click();})()`); await sleep(1500);

await ev(`window.__clip=${JSON.stringify(b64(`${CLIPS}turn-d2.wav`))}`);
await ev(`window.__say(window.__clip)`);
await ev(`${btn('doctor')}.click()`);

if (mode === 'e3') {
  // Wait until the turn actually enters the translating/playback phase, then probe.
  let entered = null;
  for (let k = 0; k < 120; k++) { await sleep(150); const s = await st(); if (/^Speaking to/.test(s)) { entered = k * 0.15; break; } }
  console.log(`E3 entered "Speaking to" after ${entered}s`);
  const r = await ev(`(function(){var p=${btn('patient')},d=${btn('doctor')};
    var pWas=p.disabled,dWas=d.disabled; p.click(); var pAfter=p.disabled;
    return JSON.stringify({phase:document.querySelector('.panel[data-role="doctor"] .status').textContent.replace('Retry','').trim(),
      patientWasDisabled:pWas, doctorWasDisabled:dWas, patientAfterClick:pAfter, patientCls:p.className});})()`);
  console.log(`   probe: ${r}`);
  const o = JSON.parse(r);
  console.log(`   -> other button ${o.patientWasDisabled && !o.patientCls.includes('listening') ? 'BLOCKED during playback (both locked)' : 'USABLE — PROBLEM'}`);
} else {
  console.log('E5: turn started; waiting for the server to be killed...');
  let saw = null;
  for (let k = 0; k < 120; k++) {
    await sleep(250);
    const s = await st();
    if (/Nothing was translated/.test(s)) { saw = (k * 0.25).toFixed(1) + 's'; break; }
  }
  await sleep(500);
  const final = await ev(`(function(){var d=document.querySelector('.panel[data-role="doctor"]');
    return JSON.stringify({s:d.querySelector('.status').textContent.replace('Retry','').trim(),
      retryVisible: !!d.querySelector('.status .retry') && getComputedStyle(d.querySelector('.status .retry')).display !== 'none',
      db:d.querySelector('button.talk').disabled, pb:document.querySelector('.panel[data-role="patient"] button.talk').disabled});})()`);
  console.log(`   error state reached: ${saw || 'NEVER'}`);
  console.log(`   final: ${final}`);
}
ws.close();

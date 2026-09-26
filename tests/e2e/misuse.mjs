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
const ev = async (e, a = false) => { const r = await send('Runtime.evaluate', { expression: e, returnByValue: true, awaitPromise: a }); return r.exceptionDetails ? 'ERR ' + String(r.exceptionDetails.exception?.description).slice(0, 160) : (r && r.result ? r.result.value : 'TIMEOUT'); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const b64 = (p) => fs.readFileSync(p).subarray(44).toString('base64');
const btn = (side) => `document.querySelector('.panel[data-role="${side}"] button.talk')`;
const state = () => ev(`(function(){var d=document.querySelector('.panel[data-role="doctor"]'),p=document.querySelector('.panel[data-role="patient"]');
  return JSON.stringify({s:d.querySelector('.status').textContent.replace('Retry','').trim(),
   db:d.querySelector('button.talk').disabled, pb:p.querySelector('button.talk').disabled,
   dcls:d.querySelector('button.talk').className, pcls:p.querySelector('button.talk').className});})()`);

async function reset() {
  await ev(`location.href='http://localhost:8000'`); await sleep(2200);
  await ev(`(async function(){
    navigator.mediaDevices=navigator.mediaDevices||{};
    navigator.mediaDevices.getUserMedia=async function(){
      if(window.__mic) return window.__mic.stream;
      var ctx=new AudioContext({sampleRate:16000}); if(ctx.state==='suspended')await ctx.resume();
      var dst=ctx.createMediaStreamDestination(); window.__mic={ctx:ctx,dst:dst,stream:dst.stream}; return dst.stream; };
    window.__say=function(b){var bin=atob(b),by=new Uint8Array(bin.length);for(var i=0;i<bin.length;i++)by[i]=bin.charCodeAt(i);
      var pcm=new Int16Array(by.buffer),c=window.__mic.ctx,buf=c.createBuffer(1,pcm.length,16000),ch=buf.getChannelData(0);
      for(var i=0;i<pcm.length;i++)ch[i]=pcm[i]/32768;var s=c.createBufferSource();s.buffer=buf;s.connect(window.__mic.dst);s.start();return 'ok';};
    var o=AudioNode.prototype.connect; AudioNode.prototype.connect=function(d){try{if(d&&d.constructor&&d.constructor.name==='AudioDestinationNode'){
      var c=this.context;if(!c.__m){var g=c.createGain();g.gain.value=0;o.call(g,d);c.__m=g;}return o.apply(this,[c.__m].concat([].slice.call(arguments,1)));}}catch(e){}
      return o.apply(this,arguments);}; return 'ok';})()`, true);
  await ev(`(function(){var c=document.getElementById('consent'); if(c&&!c.checked) c.click(); document.getElementById('start').click();})()`); await sleep(1500);
}

console.log('\n############ EDGE CASES ############');

// ---- E1: other person taps while I am speaking -------------------------------
await reset();
await ev(`window.__clip=${JSON.stringify(b64(`${CLIPS}turn-d1.wav`))}`);
await ev(`window.__say(window.__clip)`);
await ev(`${btn('doctor')}.click()`);
await sleep(1200);
let before = JSON.parse(await state());
const e1click = await ev(`(function(){var b=${btn('patient')}; var was=b.disabled; b.click(); return JSON.stringify({wasDisabled:was, afterStatus:document.querySelector('.panel[data-role="doctor"] .status').textContent.replace('Retry','').trim()});})()`);
await sleep(600);
let after = JSON.parse(await state());
console.log(`\nE1 patient taps while doctor speaks`);
console.log(`   before: doctor disabled=${before.db} patient disabled=${before.pb} status="${before.s}"`);
console.log(`   click : ${e1click}`);
console.log(`   after : patient disabled=${after.pb} status="${after.s}"  -> ${after.s.startsWith('Listening') ? 'BLOCKED (doctor still holds the turn)' : 'STATE CHANGED'}`);

// ---- E2: both tap at the same instant ---------------------------------------
await sleep(9000);
await reset();
const e2 = await ev(`(function(){
  var d=${btn('doctor')}, p=${btn('patient')};
  d.click(); p.click();
  var dd=document.querySelector('.panel[data-role="doctor"]'), pp=document.querySelector('.panel[data-role="patient"]');
  return JSON.stringify({dcls:d.className, pcls:p.className, db:d.disabled, pb:p.disabled,
    s:dd.querySelector('.status').textContent.replace('Retry','').trim()});})()`);
console.log(`\nE2 both tap in the same tick`);
console.log(`   ${e2}`);
const e2o = JSON.parse(e2);
const winners = [e2o.dcls.includes('talk--listening') && 'doctor', e2o.pcls.includes('talk--listening') && 'patient'].filter(Boolean);
console.log(`   -> ${winners.length === 1 ? `exactly one winner: ${winners[0]}` : `PROBLEM: ${winners.length} winners`}`);
await sleep(9000);
await reset();

// ---- E3: tap during playback ------------------------------------------------
await ev(`window.__clip=${JSON.stringify(b64(`${CLIPS}turn-d2.wav`))}`);
await ev(`window.__say(window.__clip)`);
await ev(`${btn('doctor')}.click()`);
await sleep(6500);   // let it auto-stop into the translating/playback phase
const e3 = await ev(`(function(){var d=document.querySelector('.panel[data-role="doctor"]'),p=document.querySelector('.panel[data-role="patient"]');
  var before=p.querySelector('.status').textContent.replace('Retry','').trim();
  var b=${btn('patient')}; var was=b.disabled; b.click();
  return JSON.stringify({phaseStatus:before, otherWasDisabled:was, otherAfter:b.disabled, otherCls:b.className});})()`);
console.log(`\nE3 tap during the translating/playback phase`);
console.log(`   ${e3}`);
const e3o = JSON.parse(e3);
console.log(`   -> ${e3o.otherWasDisabled && !e3o.otherCls.includes('talk--listening') ? 'BLOCKED (both locked while speaking)' : 'PROBLEM: other button was usable'}`);

// ---- E4: very short turn, double tap with no speech --------------------------
await sleep(9000);
await reset();
const e4 = await ev(`(function(){var b=${btn('doctor')}; b.click(); return b.className;})()`);
await sleep(120);
const e4b = await ev(`(function(){var b=${btn('doctor')}; b.click(); return JSON.stringify({cls:b.className, s:document.querySelector('.panel[data-role="doctor"] .status').textContent.replace('Retry','').trim()});})()`);
console.log(`\nE4 double tap, no speech (${e4} -> ${e4b})`);
let e4final = 'no close observed';
for (let k = 0; k < 40; k++) { await sleep(500); const o = JSON.parse(await state()); if (/Your turn|Nothing was translated/.test(o.s)) { e4final = o.s + ' after ' + ((k + 1) * 0.5) + 's'; break; } }
console.log(`   -> ${e4final}`);
ws.close();

import fs from 'node:fs';

// Suite C — the continuous-input contract (docs/starter-alignment-plan.md).
// Real app, real browser, real Gemini. The fake mic carries a -60 dBFS noise floor
// so "silence" looks like a real microphone, never digital zero.
//
//   tests/e2e/launch-chrome.sh && APP_URL=http://localhost:8010 node tests/e2e/suite-c.mjs
const CDP_FILE = process.env.CDP_FILE || '/tmp/cdp.txt';
const APP_URL = process.env.APP_URL || 'http://localhost:8000';
const CLIPS = process.env.CLIPS || new URL('./clips/', import.meta.url).pathname;

const ws = new WebSocket(fs.readFileSync(CDP_FILE, 'utf8').trim());
let id = 0; const pending = new Map();
const send = (method, params = {}) => new Promise((res) => {
  const i = ++id; const t = setTimeout(() => { pending.delete(i); res({ __timeout: true }); }, 15000);
  pending.set(i, (v) => { clearTimeout(t); res(v); });
  ws.send(JSON.stringify({ id: i, method, params }));
});
ws.addEventListener('message', (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m.error ? { error: m.error } : m.result); pending.delete(m.id); } });
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
await send('Page.enable'); await send('Runtime.enable');
const ev = async (expr, awaitPromise = false) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
  if (!r || r.__timeout) { console.error('CDP not answering'); process.exit(2); }
  return r.exceptionDetails ? 'ERR ' + String(r.exceptionDetails.exception?.description).slice(0, 200) : r.result?.value;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const b64 = (name) => fs.readFileSync(`${CLIPS}${name}.wav`).subarray(44).toString('base64');

let pass = 0, fail = 0;
const ONLY = (process.env.ONLY || 'C1,C2,C3,C4').split(',');
const want = (c) => ONLY.includes(c);
const check = (name, ok, detail = '') => { ok ? pass++ : fail++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : '  ' + detail}`); };

// Mic stub with a noise floor, an output silencer, and a spy on what the page sends.
const STUB = `(async function(){
  navigator.mediaDevices=navigator.mediaDevices||{};
  navigator.mediaDevices.getUserMedia=async function(){
    if(window.__mic) return window.__mic.stream;
    var ctx=new AudioContext({sampleRate:16000}); if(ctx.state==='suspended') await ctx.resume();
    var dst=ctx.createMediaStreamDestination();
    var nb=ctx.createBuffer(1,16000,16000),nd=nb.getChannelData(0);
    for(var i=0;i<nd.length;i++){var u=Math.random()||1e-9,v=Math.random();nd[i]=0.001*Math.sqrt(-2*Math.log(u))*Math.cos(6.2832*v);}
    var ns=ctx.createBufferSource(); ns.buffer=nb; ns.loop=true; ns.connect(dst); ns.start();
    window.__mic={ctx:ctx,dst:dst,stream:dst.stream}; return dst.stream;
  };
  window.__say=function(b64,gain){
    var bin=atob(b64),bytes=new Uint8Array(bin.length); for(var i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);
    var pcm=new Int16Array(bytes.buffer),ctx=window.__mic.ctx,g=gain||1;
    var buf=ctx.createBuffer(1,pcm.length,16000),ch=buf.getChannelData(0);
    for(var i=0;i<pcm.length;i++)ch[i]=g*pcm[i]/32768;
    var src=ctx.createBufferSource(); src.buffer=buf; src.connect(window.__mic.dst); src.start();
    return pcm.length/16000;
  };
  var orig=AudioNode.prototype.connect;
  AudioNode.prototype.connect=function(dest){
    try{ if(dest&&dest.constructor&&dest.constructor.name==='AudioDestinationNode'){
      var c=this.context; if(!c.__m){var g=c.createGain();g.gain.value=0;orig.call(g,dest);c.__m=g;}
      return orig.apply(this,[c.__m].concat([].slice.call(arguments,1))); } }catch(e){}
    return orig.apply(this,arguments);
  };
  // Record every translated-speech playback interval (24 kHz context) in page time.
  window.__play=[];
  var ostart=AudioBufferSourceNode.prototype.start;
  AudioBufferSourceNode.prototype.start=function(when){
    try{ var c=this.context; if(c.sampleRate===24000&&this.buffer){
      var t0=performance.now()+Math.max(0,((when||0)-c.currentTime))*1000;
      window.__play.push([t0,t0+this.buffer.duration*1000]); } }catch(e){}
    return ostart.apply(this,arguments);
  };
  window.__samples=[]; window.__audioN=0;
  setInterval(function(){ var st=document.querySelector('.panel[data-role="doctor"] .status');
    window.__samples.push({t:performance.now(),status:st?st.textContent:'',n:window.__audioN}); },100);
  window.__sent=[];
  var osend=WebSocket.prototype.send;
  WebSocket.prototype.send=function(d){
    try{var m=JSON.parse(d); if(m.type==='audio') window.__audioN++; window.__sent.push({t:performance.now(),type:m.type,side:m.side,
      status:document.querySelector('.panel[data-role="doctor"] .status').textContent});}catch(e){}
    return osend.apply(this,arguments);
  };
  return 'ok';
})()`;

const status = () => ev(`document.querySelector('.panel[data-role="doctor"] .status').textContent.replace('Retry','').trim()`);
const lines = () => ev(`JSON.stringify([...document.querySelectorAll('.panel[data-role="doctor"] .conversation .line')].map(l=>l.innerText))`).then(JSON.parse);

async function fresh() {
  await ev(`location.href='${APP_URL}'`); await sleep(2000);
  await ev(STUB, true);
  await ev(`(function(){try{localStorage.removeItem('interpret.cfg')}catch(e){};var c=document.getElementById('consent'); if(!c.checked) c.click(); document.getElementById('start').click();})()`);
  for (let i = 0; i < 20 && (await status()) !== 'Ready'; i++) await sleep(250);
}

async function turn(side, clip, gain = 1) {
  await ev(`window.__clip=${JSON.stringify(b64(clip))}`);
  await ev(`document.querySelector('.panel[data-role="${side}"] button.talk').click()`);
  await sleep(300);
  const dur = await ev(`window.__say(window.__clip, ${gain})`);
  for (let i = 0; i < 90; i++) {
    await sleep(500);
    const s = await status();
    if (/Your turn|Nothing was translated/.test(s)) return { status: s, dur };
  }
  return { status: 'TIMEOUT', dur };
}

let r, L, sent;
if (want('C1')) {
// ---------------------------------------------------------------------------
console.log('C1 continuous upload, routing and feedback guard');
await fresh();
await ev(`window.__sent.length=0`);
await sleep(3000);
sent = JSON.parse(await ev(`JSON.stringify(window.__sent)`));
const idleRate = sent.filter((m) => m.type === 'audio').length / 3;
check('mic streams ~10 chunks/s before anyone talks', idleRate >= 8 && idleRate <= 12, `rate=${idleRate}`);

await ev(`window.__sent.length=0`);
const r1 = await turn('doctor', 'turn-d1');
check('doctor turn completes', r1.status === 'Your turn', r1.status);
sent = JSON.parse(await ev(`JSON.stringify(window.__sent)`));
const routes = sent.filter((m) => m.type === 'route').map((m) => m.side ?? null);
check('TALK routes, release un-routes', JSON.stringify(routes) === JSON.stringify(['doctor', null]), JSON.stringify(routes));
const tRelease = sent.find((m) => m.type === 'route' && m.side == null)?.t;
const during = sent.filter((m) => m.type === 'audio' && m.t > tRelease);
const routedAudio = sent.filter((m) => m.type === 'audio' && m.t < tRelease);
check('audio streamed continuously while routed', routedAudio.length >= Math.floor(r1.dur * 10),
  `${routedAudio.length} chunks for ${r1.dur}s`);
// Playback windows: merge back-to-back chunks (gaps < 60 ms), then shrink each
// merged window by 150 ms per side for scheduling jitter.
const play = JSON.parse(await ev(`JSON.stringify(window.__play)`)).filter(([a]) => a > tRelease)
  .sort((x, y) => x[0] - y[0]);
const merged = [];
for (const iv of play) { const l = merged[merged.length - 1]; if (l && iv[0] <= l[1] + 60) l[1] = Math.max(l[1], iv[1]); else merged.push([...iv]); }
for (const iv of merged) { iv[0] += 150; iv[1] -= 150; }
const playedMs = merged.reduce((t, [a, b]) => t + Math.max(0, b - a), 0);
const inPlayback = sent.filter((m) => m.type === 'audio' && merged.some(([a, b]) => m.t >= a && m.t <= b));
check('translated speech actually played (>= 0.5 s)', playedMs >= 500, `${playedMs.toFixed(0)} ms`);
check('no mic upload while translated speech plays (pre-roll cannot capture it)',
  inPlayback.length === 0, `${inPlayback.length} chunks during ${(playedMs / 1000).toFixed(1)} s of playback`);
await sleep(1500);
const idle2 = JSON.parse(await ev(`JSON.stringify(window.__sent.slice(-12))`)).filter((m) => m.type === 'audio').length;
check('streaming resumes after playback', idle2 >= 8, `last 12 msgs had ${idle2} audio`);

}
if (want('C2')) {
// ---------------------------------------------------------------------------
console.log('\nC2 attribution and a thinking pause');
await fresh();
r = await turn('doctor', 's11-d1');  // "I want to ask you ... (2 s pause) ... about your sleep."
check('2 s mid-sentence pause: one turn', r.status === 'Your turn', r.status);
L = await lines();
check('pause: still one line', L.length === 1, JSON.stringify(L));
check('pause: both halves translated (睡眠/睡)', /睡/.test(L[0] || ''), JSON.stringify(L));
r = await turn('patient', 'c-p1');
L = await lines();
check('patient line is separate and translated', L.length === 2 && /allerg/i.test(L[1] || ''), JSON.stringify(L));
check('doctor line did not absorb patient output', !/allerg/i.test(L[0] || ''), JSON.stringify(L));


}
if (want('C3')) {
// ---------------------------------------------------------------------------
console.log('\nC3 quiet speaker (-30 dB) is not filtered out');
await fresh();
const tq = Date.now();
r = await turn('doctor', 's09-d1', 0.0316);
const quietSecs = (Date.now() - tq) / 1000;
check('quiet speaker auto-releases promptly (model transcript counts as speech)', quietSecs < 20, `${quietSecs}s`);
L = await lines();
check('quiet turn completes', r.status === 'Your turn', r.status);
check('quiet speaker translated (空腹/空肚子/吃东西)', /空腹|空肚|吃东西|进食|饭/.test(L[0] || ''), JSON.stringify(L));
check('no "Nothing captured" flag', !/Nothing captured/.test(L[0] || ''), JSON.stringify(L));


}
if (want('C4')) {
// ---------------------------------------------------------------------------
console.log('\nC4 final clause survives (reordering languages)');
await fresh();
let tailOk = 0;
for (const clip of ['turn-d5', 'b13-d1']) {
  r = await turn('doctor', clip);
  L = await lines();
  const last = L[L.length - 1] || '';
  const ok = clip === 'turn-d5' ? /回来|复诊|再来|回诊/.test(last) : /睡|晚上|夜/.test(last);
  tailOk += ok;
  check(`${clip}: final clause translated`, ok, last);
}

}

console.log(`\n${pass}/${pass + fail} checks pass`);
ws.close();
process.exit(fail ? 1 : 0);

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
function send(method, params = {}) {
  return new Promise((res, rej) => {
    const i = ++id;
    const t = setTimeout(() => { pending.delete(i); res({ __timeout: true }); }, 15000);
    pending.set(i, { res: (v) => { clearTimeout(t); res(v); }, rej: (e) => { clearTimeout(t); rej(e); } });
    ws.send(JSON.stringify({ id: i, method, params }));
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
const ev = async (expr, awaitPromise = false) => { const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise }); return r.exceptionDetails ? 'ERR ' + String(r.exceptionDetails.exception?.description).slice(0, 200) : (r && r.result ? r.result.value : 'TIMEOUT'); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const b64 = (p) => fs.readFileSync(p).subarray(44).toString('base64');

const TURNS = [
  ['doctor', `${CLIPS}turn-d1.wav`, 'Good morning, Mrs. Chen. Tell me what has been going on.'],
  ['patient', `${CLIPS}turn-p1.wav`, '我咳嗽发烧已经五天了。'],
  ['doctor', `${CLIPS}turn-d2.wav`, 'How many days have you had this cough and fever?'],
  ['patient', `${CLIPS}turn-p2.wav`, '昨天量体温有三十八度五。'],
  ['doctor', `${CLIPS}turn-d3.wav`, 'Take one tablet twice a day for five days.'],
  ['patient', `${CLIPS}turn-p3.wav`, '这个药一天要吃几次？'],
  ['doctor', `${CLIPS}turn-d4.wav`, 'Do not skip any doses, even if you start feeling better.'],
  ['patient', `${CLIPS}turn-p4.wav`, '我怕这药吃了会犯困。'],
  ['doctor', `${CLIPS}turn-d5.wav`, 'Come back in one week if the fever continues.'],
  ['patient', `${CLIPS}turn-p5.wav`, '谢谢医生，麻烦您了。'],
];

console.log('navigating...');
await ev(`location.href='http://localhost:8000'`); await sleep(2500);

// Persistent mic: one destination we can feed clip by clip (the app only calls
// getUserMedia once and then reuses its context).
await ev(`(async function(){
  navigator.mediaDevices=navigator.mediaDevices||{};
  navigator.mediaDevices.getUserMedia=async function(){
    if(window.__mic) return window.__mic.stream;
    var ctx=new AudioContext({sampleRate:16000});
    if(ctx.state==='suspended') await ctx.resume();
    var dst=ctx.createMediaStreamDestination();
    window.__mic={ctx:ctx,dst:dst,stream:dst.stream};
    return dst.stream;
  };
  window.__say=function(b64){
    var bin=atob(b64),bytes=new Uint8Array(bin.length);
    for(var i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);
    var pcm=new Int16Array(bytes.buffer),ctx=window.__mic.ctx;
    var buf=ctx.createBuffer(1,pcm.length,16000),ch=buf.getChannelData(0);
    for(var i=0;i<pcm.length;i++)ch[i]=pcm[i]/32768;
    var src=ctx.createBufferSource();src.buffer=buf;src.connect(window.__mic.dst);src.start();
    return (pcm.length/16000).toFixed(2)+'s';
  };
  // silence playback so the room stays quiet during the test
  var orig=AudioNode.prototype.connect;
  AudioNode.prototype.connect=function(dest){
    try{ if(dest&&dest.constructor&&dest.constructor.name==='AudioDestinationNode'){
      var c=this.context; if(!c.__m){var g=c.createGain();g.gain.value=0;orig.call(g,dest);c.__m=g;}
      return orig.apply(this,[c.__m].concat([].slice.call(arguments,1))); } }catch(e){}
    return orig.apply(this,arguments);
  };
  return 'mic + silencer ready';
})()`, true);

await ev(`(function(){var c=document.getElementById('consent'); if(c&&!c.checked) c.click(); document.getElementById('start').click();})()`); await sleep(1800);
console.log('started:', await ev(`document.querySelector('.panel[data-role="doctor"] .status').textContent`));

const snap = () => ev(`(function(){var d=document.querySelector('.panel[data-role="doctor"]'),p=document.querySelector('.panel[data-role="patient"]');
  return JSON.stringify({d:d.querySelector('.conversation').innerText,p:p.querySelector('.conversation').innerText,
    s:d.querySelector('.status').textContent.replace('Retry','').trim(),
    db:d.querySelector('button.talk').disabled,pb:p.querySelector('button.talk').disabled});})()`);

const results = [];
for (let i = 0; i < TURNS.length; i++) {
  const [side, clip, text] = TURNS[i];
  const label = `${String(i + 1).padStart(2)} ${side.toUpperCase()}`;
  console.log(`\n--- ${label}  "${text}"`);
  await ev(`window.__clip=${JSON.stringify(b64(clip))}`);
  const tapped = await ev(`(function(){var b=document.querySelector('.panel[data-role="${side}"] button.talk'); if(b.disabled) return 'BUTTON DISABLED'; b.click(); return 'tapped';})()`);
  if (tapped === 'BUTTON DISABLED') { console.log('    BUTTON DISABLED'); results.push({ label, text, status: 'COULD NOT TAP', transcript: '' }); continue; }
  // Speak only AFTER the tap: the app builds its mic AudioContext inside a promise,
  // so a clip played before the tap is consumed by nothing.
  await sleep(700);
  const played = await ev(`window.__say(window.__clip)`);
  console.log(`    tap: ${tapped} | clip ${played}`);

  let status = '', done = false;
  for (let k = 0; k < 80; k++) {
    await sleep(500);
    const raw = await snap(); let o; try { o = JSON.parse(raw); } catch { continue; }
    status = o.s;
    if (/Your turn/.test(status)) { done = true; break; }
    if (/Nothing was translated/.test(status)) break;
  }
  const raw = await snap(); const o = JSON.parse(raw);
  const lines = o.d.split('\n').filter(Boolean);
  results.push({ label, text, status, done, lines });
  console.log(`    -> ${status}${done ? '' : '  *** DID NOT HAND OVER ***'}`);
}

const final = JSON.parse(await snap());
console.log('\n================ DOCTOR PANEL (English large / Chinese small) ================');
console.log(final.d);
console.log('\n================ PATIENT PANEL (Chinese large / English small) ================');
console.log(final.p);
console.log(`\nfinal status: ${final.s} | doctor btn disabled=${final.db} patient btn disabled=${final.pb}`);
const flags = await ev(`document.querySelectorAll('.flag').length`);
console.log(`\nnumber-integrity flags raised: ${flags}`);

const shot = await send('Page.captureScreenshot', { format: 'png', fromSurface: true });
fs.writeFileSync('/tmp/e2e-session-final.png', Buffer.from(shot.data, 'base64'));

console.log('\n================ PER-TURN SUMMARY ================');
for (const r of results) console.log(`${r.label}  ${r.done ? 'ok  ' : 'FAIL'}  ${r.status.padEnd(20)} "${r.text}"`);
ws.close();

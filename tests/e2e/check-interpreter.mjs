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
const ws = new WebSocket(cdpUrl); let id=0; const pend=new Map();
const send=(m,p={})=>new Promise((res,rej)=>{const i=++id;const t=setTimeout(()=>{pend.delete(i);res({__timeout:true});},15000);pend.set(i,{res:v=>{clearTimeout(t);res(v)},rej:e=>{clearTimeout(t);rej(e)}});ws.send(JSON.stringify({id:i,method:m,params:p}));});
ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id&&pend.has(m.id)){const p=pend.get(m.id);pend.delete(m.id);m.error?p.rej(new Error(JSON.stringify(m.error))):p.res(m.result);}});
await new Promise(r=>ws.addEventListener('open',r,{once:true}));
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
const ev=async(e,a=false)=>{const r=await send('Runtime.evaluate',{expression:e,returnByValue:true,awaitPromise:a});return r.exceptionDetails?'ERR':(r && r.result ? r.result.value : 'TIMEOUT');};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
await ev(`location.href='http://localhost:8000'`); await sleep(2200);
await ev(`try{localStorage.removeItem('interpret.cfg')}catch(e){}; location.reload()`); await sleep(2400);
await ev(`(function(){var c=document.getElementById('consent'); if(c&&!c.checked) c.click(); document.getElementById('start').click();})()`); await sleep(1500);
console.log('before click -> overlay visible:', await ev(`!document.getElementById('interpreter').classList.contains('hidden')`));
await ev(`document.querySelector('.call').click()`); await sleep(400);
console.log('after  click -> overlay visible:', await ev(`!document.getElementById('interpreter').classList.contains('hidden')`));
console.log('overlay heading:', await ev(`document.querySelector('#interpreter h1').textContent`));
console.log('has both languages:', await ev(`document.querySelectorAll('#interpreter .note').length === 2`));
await ev(`document.getElementById('interp-continue').click()`); await sleep(400);
console.log('continue -> overlay visible:', await ev(`!document.getElementById('interpreter').classList.contains('hidden')`), '| session still up:', await ev(`document.querySelector('.panel[data-role="doctor"] .status').textContent.replace('Retry','').trim()`));
ws.close();

const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function dashboard() {
  let next = 0;
  const intervals = new Map(), timeouts = new Map(), calls = [], sockets = [];
  const elements = new Map();
  const context = vm.createContext({
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, {value:'s', classList:{add(){},remove(){},toggle(){}}, innerHTML:''});
        return elements.get(id);
      },
      createElement: () => ({}),
    },
    location:{protocol:'http:',host:'test'},
    localStorage:{getItem:()=>null},
    setInterval(fn, ms){ const id=++next; intervals.set(id,{fn,ms});return id; },
    clearInterval(id){ intervals.delete(id); },
    setTimeout(fn, ms){ const id=++next; timeouts.set(id,{fn,ms});return id; },
    clearTimeout(id){ timeouts.delete(id); },
    WebSocket: class {
      constructor(){ sockets.push(this); }
      close(){this.onclose?.();}
      send(){}
    },
    async fetch(path){
      calls.push(path);
      const data=path.includes('auth/status')?{authenticated:true}:path.includes('meta')?{profiles:{},version:'4.1.0'}:{sessions:[]};
      return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>data};
    },
  });
  vm.runInContext(fs.readFileSync('static/app.js','utf8').replace(/boot\(\);\s*$/, ''),context);
  return {context, intervals, timeouts, calls, sockets, run: code=>vm.runInContext(code,context)};
}

test('event storms produce no HTTP requests; repeated login and logout clean timers', async()=>{
  const d=dashboard();
  await d.run('boot()');
  d.sockets.at(-1).onopen();
  const initial=d.calls.length;
  for(let i=0;i<1000;i++)d.sockets.at(-1).onmessage({data:JSON.stringify({kind:'event',event:{type:'like',session_id:'s'}})});
  d.run('currentTab="monitor"');
  for(let i=0;i<20;i++){
    d.sockets.at(-1).onmessage({data:JSON.stringify({kind:'event',event:{type:'perception',session_id:'s',timestamp:0,safety:{level:'ALERT'}}})});
    d.sockets.at(-1).onmessage({data:JSON.stringify({kind:'status',session:{session_id:'s',viewers:i}})});
  }
  assert.equal(d.calls.length,initial);
  await d.run('boot()');
  d.sockets.at(-1).onopen();
  assert.equal([...d.intervals.values()].filter(t=>t.ms===5000).length,1);
  assert.equal([...d.intervals.values()].filter(t=>t.ms===30000).length,1);
  d.sockets.at(-1).onclose();
  assert.equal(d.timeouts.size,1);
  await d.run('logout()');
  assert.equal(d.intervals.size,0);
  assert.equal(d.timeouts.size,0);
  const count=d.calls.length;
  await d.run('refresh()');
  assert.equal(d.calls.length,count);
});

test('expired authentication stops polling and websocket reconnection',async()=>{
  const d=dashboard();
  await d.run('boot()');
  d.sockets.at(-1).onopen();
  d.context.fetch=async()=>({status:401});
  await d.run('refresh()');
  assert.equal(d.intervals.size,0);
  assert.equal(d.timeouts.size,0);
});

test('slow refresh has at most one HTTP request in flight',async()=>{
  const d=dashboard();
  await d.run('boot()');
  let release, count=0;
  d.context.fetch=()=>{count++;return new Promise(resolve=>{release=()=>resolve({ok:true,headers:{get:()=> 'application/json'},json:async()=>({sessions:[]})});});};
  const first=d.run('refresh()');
  const second=d.run('refresh()');
  assert.equal(count,1);
  release();
  await Promise.all([first,second]);
});

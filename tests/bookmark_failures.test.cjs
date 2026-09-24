const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const sources = ['failures.js', 'folder-downloads.js'].map(file => fs.readFileSync(`${__dirname}/../frontend/bookmarks/${file}`, 'utf8'));
const flush = () => new Promise(resolve => setImmediate(resolve));
function harness(server) {
 const elements = new Map(), events = {}, intervals = [];
 function element() {
  return {children:[], hidden:true, setAttribute(){}, addEventListener(name, fn){this[name]=fn;}, append(...children){for(const child of children){this.children.push(child);child.parent=this;}}, remove(){this.parent.children=this.parent.children.filter(child=>child!==this);}};
 }
 for(const id of ['bookmark-failures','bookmark-failure-list','dismiss-bookmark-failures','show-downloaded-pages']) elements.set(id, element());
 const context = {
  document:{hidden:false,getElementById:id=>elements.get(id),createElement:element,querySelectorAll:()=>[],addEventListener:(name,fn)=>events[name]=fn},
  localStorage:{getItem:()=>null,setItem:()=>{}},
  setInterval:fn=>intervals.push(fn),setTimeout,clearTimeout,render:()=>{},
  fetch:async(url,options)=>{
   if(url.endsWith('/dismiss')) {
    const value=JSON.parse(options.body);
    const row=server.rows.find(row=>row.folder_key===value.folder_key && row.updated_at===value.updated_at);
    if(row) row.dismissed_at=row.updated_at;
    return {ok:true};
   }
   return {ok:true,json:async()=>server.rows.map(row=>({...row}))};
  },
 };
 context.window=context;
 vm.createContext(context);sources.forEach(source=>vm.runInContext(source,context));
 return {elements, async poll(){intervals[0]();await flush();}, start:async()=>{events.DOMContentLoaded();await flush();}};
}
const failure = stamp => ({folder_key:'["https://wiki.test","Team"]',status:'failed',error:'HTTP 500',updated_at:stamp,dismissed_at:''});

test('one current failure per folder; retry, recovery and deletion remove old messages', async()=>{
 const server={rows:[failure('first')]}, app=harness(server), list=app.elements.get('bookmark-failure-list');
 await app.start();assert.equal(list.children.length,1);
 await app.poll();assert.equal(list.children.length,1);
 server.rows=[failure('second')];await app.poll();assert.equal(list.children.length,1);
 server.rows[0].status='running';await app.poll();assert.equal(list.children.length,0);
 server.rows=[failure('third')];await app.poll();assert.equal(list.children.length,1);
 server.rows[0].status='completed';await app.poll();assert.equal(list.children.length,0);
 server.rows=[failure('fourth')];await app.poll();server.rows=[];await app.poll();assert.equal(list.children.length,0);
});
test('dismiss-all persists remotely and stays hidden in a fresh browser; new failures reappear',async()=>{
 const server={rows:[failure('first')]}, app=harness(server);
 await app.start();app.elements.get('dismiss-bookmark-failures').onclick();await flush();
 assert.equal(server.rows[0].dismissed_at,'first');
 const fresh=harness(server);await fresh.start();
 assert.equal(fresh.elements.get('bookmark-failure-list').children.length,0);
 server.rows=[failure('second')];await fresh.poll();
 assert.equal(fresh.elements.get('bookmark-failure-list').children.length,1);
});

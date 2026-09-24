const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const {matchesUrl} = require('../frontend/bookmarks/search.js');
const source = fs.readFileSync(require.resolve('../frontend/bookmarks/metadata.js'), 'utf8');

for (const resolved of [false, true]) test(`saved bookmark is revealed ${resolved ? 'after resolving an alias' : 'without a network request'}`, async () => {
 const item = {id:7, title:'Cloud guide', page_id:'123', url:'https://wiki.test/spaces/BEP/pages/123/Cloud', confluenceBaseUrl:'https://wiki.test'};
 const input = {value:resolved ? 'https://wiki.test/x/alias' : 'https://wiki.test/pages/viewpage.action?pageId=123&title=Cloud'};
 const button = {disabled:false, hidden:false};
 const branch = {dataset:{branch:'folder'}, open:false, matches:()=>true, parentElement:null};
 let focused = false, scrolled = false, shown, requests = 0, renders = 0, submit;
 const row = {parentElement:branch,querySelector:()=>({focus:()=>focused=true}),scrollIntoView:()=>scrolled=true};
 const context = {
  window:{bookmarkDatabaseReady:true},
  document:{querySelector:selector=>selector==='#bookmark-search-form' ? {addEventListener:(_,fn)=>submit=fn} : selector==='#bookmark-search' ? input : selector==='#add-bookmark' ? button : row},
  fetch:async()=>{requests++;return {ok:true,json:async()=>item};},
  parseBookmarkUrl:value=>new URL(value),bookmarkMatchesUrl:matchesUrl,bookmarks:[item],
  collapsedBranches:new Set(['folder']),render:()=>renders++,showPageDetails:id=>shown=id,
  toast:()=>{},showBookmarkFailure:message=>assert.fail(message),
  view:'favorites',domain:'other.test',selectedDomainGroup:'group',selectedPerson:'someone',query:input.value,
 };
 context.window=context;
 context.bookmarkDatabaseReady=true;
 vm.runInNewContext(source, context);
 await submit({preventDefault(){}});
 assert.equal(requests,resolved ? 1 : 0);
 assert.equal(context.bookmarks.length,1);
 assert.equal(context.view,'all');
 for (const key of ['domain','selectedDomainGroup','selectedPerson','query']) assert.equal(context[key],'');
 assert.equal(input.value,'');assert.equal(button.hidden,true);assert.equal(button.disabled,false);
 assert.equal(shown,7);assert.equal(branch.open,true);assert.equal(context.collapsedBranches.size,0);
 assert.equal(focused,true);assert.equal(scrolled,true);assert.equal(renders,1);
});

for (const scenario of ['saved', 'new', 'missing']) test(`numeric Confluence page ID: ${scenario}`, async()=>{
 const item={id:7,title:'Cloud guide',page_id:'1600383846',url:'https://wiki.test/pages/viewpage.action?pageId=1600383846',sourceType:'confluence',confluenceBaseUrl:'https://wiki.test'};
 const input={value:' 1600383846 '},button={disabled:false};
 let submit,persisted=0,error='',sent;
 const context={
  document:{querySelector:selector=>selector==='#bookmark-search-form'?{addEventListener:(_,fn)=>submit=fn}:selector==='#bookmark-search'?input:selector==='#add-bookmark'?button:null},
  fetch:async(_,options)=>{sent=JSON.parse(options.body);return {ok:scenario!=='missing',json:async()=>scenario==='missing'?{detail:'Confluence HTTP 404'}:item};},
  parseBookmarkUrl:()=>null,bookmarkMatchesUrl:matchesUrl,
  bookmarks:scenario==='saved'?[item]:[],bookmarkDatabaseReady:true,
  render:()=>{},toast:()=>{},showBookmarkFailure:message=>error=message,
  nextBookmarkId:()=>8,persist:async()=>{persisted++;return true;},
  view:'all',domain:'',selectedDomainGroup:'',selectedPerson:'',query:input.value,
 };
 context.window=context;
 vm.runInNewContext(source,context);
 await submit({preventDefault(){}});
 assert.equal(sent.url,'1600383846');
 assert.equal(persisted,scenario==='new'?1:0);
 assert.equal(context.bookmarks.length,scenario==='missing'?0:1);
 assert.equal(button.disabled,false);
 if(scenario==='missing') assert.match(error,/404/);
 else {assert.equal(error,'');assert.equal(input.value,'');}
});

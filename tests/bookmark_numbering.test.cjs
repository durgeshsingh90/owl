const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const numbering = require('../frontend/bookmarks/tree-numbering.js');
const script = fs.readFileSync(`${__dirname}/../frontend/bookmarks/script.js`, 'utf8');
const renderer = script.slice(script.indexOf('function renderBookmarkTree('), script.indexOf('let selectedBookmarkId = null;'));
function fixture() {
 const items = [
  {id:1, title:'First site', domain:'first.test', added:1},
  {id:2, title:'Other folder', folderPath:['Team','Other'], added:2},
  {id:3, title:'Parent page', breadcrumb:['Target'], added:3},
  {id:4, title:'Needle page', folderPath:['Team','Target'], added:4},
  {id:5, title:'Child page', breadcrumb:['Target'], added:5},
 ].map(item=>({...item, url:'https://wiki.test/'+item.id, views:item.id, lastViewed:item.id}));
 const hierarchy={3:{space:'Team',parent:null},5:{space:'Team',parent:3}};
 const tree={innerHTML:''},sort={value:'added'};
 const context={...numbering,showSearchBranches:false,bookmarkInCurrentView:()=>true,matchesPerson:()=>true,bookmarks:items,pageHierarchy:hierarchy,view:'all',domain:'',selectedDomainGroup:'',query:'',selectedPerson:'',personRole:'any',treeFilterKey:'',collapsedBranches:new Set(),selectedBookmarks:new Set(),starredBookmarkFolders:new Set(),selectedBookmarkId:null,window:{},document:{querySelector:selector=>selector==='#bookmark-sort'?sort:tree},esc:value=>String(value),bookmarkAgeTag:()=>'',confluenceAgeBadge:()=>'',date:()=>''};
 vm.createContext(context);vm.runInContext(renderer,context);
 return {items,context,sort,render(filtered=items,query='',downloaded=[]){
  context.query=query;
  context.renderBookmarkTree([...filtered].sort((a,b)=>numbering.compareBookmarkOrder(a,b,sort.value)),downloaded,false);
  return new Map([...tree.innerHTML.matchAll(/data-bookmark-row="(\d+)"[\s\S]*?<span class="tree-number">([^<]+)<\/span>/g)].map(match=>[Number(match[1]),match[2]]));
 }};
}
test('search retains folder, sibling and parent-child numbers from the full tree',()=>{
 for(const sort of ['added','title','opens','viewed']) {
  const app=fixture();app.sort.value=sort;
  const full=app.render();
  for(const item of app.items) {
   const found=app.render([item],item.title);
   assert.equal(found.get(item.id),full.get(item.id),`${sort}: ${item.title}`);
  }
  assert.deepEqual(app.render(),full);
 }
});
test('adding a bookmark still renumbers the tree and search uses its new numbers',()=>{
 const app=fixture();const before=app.render();
 app.items.push({id:6,title:'New page',folderPath:['Team','Target'],added:100,views:0,url:'https://wiki.test/6'});
 const after=app.render();
 assert.notEqual(before.get(4),after.get(4));
 assert.equal(app.render([app.items[3]],'Needle').get(4),after.get(4));
});
test('downloaded search results cannot change saved bookmark numbers',()=>{
 const app=fixture();const full=app.render();
 const downloaded=[{id:'downloaded:1',title:'Extra',url:'https://wiki.test/99',folderPath:['Team','Target','New subfolder'],searchOnly:true}];
 assert.equal(app.render([app.items[3]],'Needle',downloaded).get(4),full.get(4));
});
test('search preserves the numbering of the current view without counting hidden bookmarks',()=>{
 const app=fixture();
 app.context.bookmarkInCurrentView=item=>item.id!==1;
 const visible=app.items.filter(app.context.bookmarkInCurrentView);
 const before=app.render(visible);
 assert.equal(app.render([app.items[3]],'Needle').get(4),before.get(4));
});
test('branch toggle reveals only matching branches and descendants and keeps their numbers',()=>{
 const app=fixture();
 app.items.push({id:6,title:'Nested context',folderPath:['Team','Target','Subfolder'],added:6,url:'https://wiki.test/6',views:0});
 app.items.push({id:7,title:'Unrelated',folderPath:['Team','Other','Elsewhere'],added:7,url:'https://wiki.test/7',views:0});
 const full=app.render();
 assert.deepEqual([...app.render([app.items[3]],'Needle').keys()],[4]);
 app.context.showSearchBranches=true;
 const expanded=app.render([app.items[3]],'Needle');
 assert.deepEqual([...expanded.keys()].sort(),[3,4,5,6]);
 for(const [id,number] of expanded) assert.equal(number,full.get(id));
 assert.match(app.context.document.querySelector('#bookmark-tree').innerHTML,/Branch context/);
 app.context.showSearchBranches=false;
 assert.deepEqual([...app.render([app.items[3]],'Needle').keys()],[4]);
});
test('branch context respects active filters and does not count as search matches',()=>{
 const app=fixture();app.context.showSearchBranches=true;
 app.context.bookmarkInCurrentView=item=>item.id!==3 && item.id!==5;
 assert.deepEqual([...app.render([app.items[3]],'Needle').keys()],[4]);
 let searched;
 app.context.window.searchDownloadedBookmarkPages=items=>searched=items;
 app.context.renderBookmarkTree([app.items[3]]);
 assert.deepEqual(Array.from(searched,item=>item.id),[4]);
});
test('downloaded context gets distinct numbers without renumbering saved bookmarks',()=>{
 const app=fixture(), numbers=numbering.bookmarkTreeNumbers(app.items,app.context.pageHierarchy,'added');
 const before=new Map(numbers.pages);
 numbering.extendBookmarkTreeNumbers(numbers,[{id:'downloaded',folderPath:['Team','Target','Extra']}],app.context.pageHierarchy);
 for(const [id,number] of before) assert.equal(numbers.pages.get(id),number);
 const values=[...numbers.pages.values(),...numbers.folders.values()];
 assert.equal(new Set(values).size,values.length);
});

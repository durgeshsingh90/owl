const test = require('node:test');
const assert = require('node:assert/strict');
const transfer = require('../frontend/bookmarks/json-transfer.js');
const legacy = {page_id:'2135277690',title:'AWS — Azure',url:'https://wiki.example.test/spaces/BEP/pages/2135277690/AWS+Cloud+Services#section',space:'Cloud',space_key:'BEP',version:40,modified:'2026-08-04T14:13:50.833-05:00',breadcrumb:['Home','Cloud'],favorite:true,notes:'AWS\nAzure ✓',saved_at:'2026-08-05T11:34:48',views:12,tree_number:'26.1.1'};
test('legacy metadata and unicode survive round trip',()=>{
 const item=transfer.parse(JSON.stringify([legacy])).items[0];
 assert.equal(item.page_id,'2135277690'); assert.equal(item.spaceKey,'BEP'); assert.equal(item.notes,legacy.notes); assert.equal(item.views,12); assert.equal(item.favorite,true); assert.equal(item.confluenceUpdatedAt,legacy.modified);
 assert.deepEqual(transfer.parse(JSON.stringify([item])).items[0],item);
});
test('deleted, unsafe and duplicate page URLs are skipped',()=>{
 const result=transfer.parse(JSON.stringify([legacy,{...legacy,url:'https://wiki.example.test/pages/viewpage.action?pageId=2135277690'},{...legacy,deleted:true},{url:'javascript:alert(1)'},null]));
 assert.equal(result.items.length,1); assert.equal(result.skipped,4);
});
test('path ID is recognized without explicit metadata and notes remapped from workspace',()=>{
 const result=transfer.parse(JSON.stringify({bookmarks:[{id:41,url:legacy.url}],notes:{41:'saved notes'}}));
 assert.equal(result.items[0].page_id,'2135277690'); assert.equal(result.items[0].notes,'saved notes');
});

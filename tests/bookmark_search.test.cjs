const test = require('node:test');
const assert = require('node:assert/strict');
const matches = require('../frontend/bookmarks/search.js');
const item = {title:'Cloud guide', url:'https://wiki.test/pages/123456789/Cloud', page_id:'123456789',contentText:'Azure platform documentation'};
test('separate words search any selected field, case insensitive',()=>{
 assert.equal(matches(item,'aws azure',['notes','content'],'separate','AWS operations'),true);
 assert.equal(matches(item,'AWS',['content'],'separate','AWS operations'),false);
 assert.equal(matches(item,'AWS',['notes'],'separate','AWS operations'),true);
});
test('phrase stays together within one selected field',()=>{
 assert.equal(matches(item,'aws azure',['notes','content'],'together','AWS operations'),false);
 assert.equal(matches(item,'aws azure',['notes'],'together','Plan AWS Azure migration'),true);
});
test('page IDs and URLs can be selected independently; blank search retains everything',()=>{
 assert.equal(matches(item,'123456789',['page_id']),true);
 assert.equal(matches(item,'wiki.test',['url']),true);
 assert.equal(matches(item,'123456789',['title']),false);
 assert.equal(matches(item,'cloud',[]),false);
 assert.equal(matches(item,'',[]),true);
});

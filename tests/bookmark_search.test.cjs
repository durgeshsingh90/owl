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

test('alternate Confluence URLs find the saved page in either matching mode', () => {
 const url = 'https://wiki.test/pages/viewpage.action?pageId=123456789&spaceKey=BEP&title=Cloud%2BGuide#section';
 for (const mode of ['separate', 'together']) {
  assert.equal(matches(item, url, ['url'], mode), true);
  assert.equal(matches(item, url, ['page_id'], mode), true);
  assert.equal(matches(item, url, ['notes'], mode), false);
 }
 assert.equal(matches.matchesUrl({...item, page_id:123456789}, url), true);
 assert.equal(matches.matchesUrl({...item, page_id:undefined}, url), true);
});
test('Confluence identity keeps sites, context paths and page IDs distinct', () => {
 const url = 'https://wiki.test/pages/viewpage.action?pageId=123456789';
 assert.equal(matches.matchesUrl(item, url.replace('wiki.test','other.test')), false);
 assert.equal(matches.matchesUrl(item, url.replace('/pages/', '/wiki/pages/')), false);
 assert.equal(matches.matchesUrl(item, url.replace('123456789','987654321')), false);
 assert.equal(matches.matchesUrl(item, 'not a URL'), false);
 const nested = {...item,url:'https://wiki.test/Wiki/spaces/BEP/pages/123456789/Cloud'};
 assert.equal(matches.matchesUrl(nested, 'https://wiki.test/Wiki/pages/viewpage.action?pageId=123456789'),true);
});

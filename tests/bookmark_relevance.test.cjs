const test = require('node:test');
const assert = require('node:assert/strict');
const {rankBookmarks, bookmarkSearchTerms, termQuality, termWindow} = require('../frontend/bookmarks/relevance.js');

const fields = ['title', 'notes', 'url', 'content'];
const page = (id, title, contentText = '', extra = {}) => ({id, title, contentText, url: `https://wiki.test/pages/${id}`, views: 0, ...extra});
const order = results => results.map(result => result.item.id);

test('filler words are ignored for "all words" but kept in the phrase', () => {
  assert.deepEqual(bookmarkSearchTerms('AWS for  IDE'), {phrase: 'aws for ide', words: ['aws', 'for', 'ide'], key: ['aws', 'ide']});
  assert.deepEqual(bookmarkSearchTerms('for').key, ['for']);
});

test('word quality separates whole words, word starts and fragments', () => {
  assert.equal(termQuality('aws ide setup', 'ide'), 1);
  assert.equal(termQuality('supported ides', 'ide'), 0.6);
  assert.equal(termQuality('user guide', 'ide'), 0.25);
  assert.equal(termQuality('user guide', 'azure'), 0);
});

test('word window finds the tightest span containing every term', () => {
  assert.equal(termWindow('aws toolkit for the ide', ['aws', 'ide']), 5);
  assert.equal(termWindow('ide notes aws ide', ['aws', 'ide']), 2);
  assert.equal(termWindow('only aws here', ['aws', 'ide']), Infinity);
});

test('"AWS for IDE": exact phrase, then all words, then partial matches', () => {
  const items = [
    page(1, 'AWS account list', 'Every team uses AWS.'),
    page(2, 'Developer setup', 'Install the IDE plugin. Later, configure AWS credentials in the console.'),
    page(3, 'Toolkits', 'How to use AWS for IDE integration in VS Code.'),
    page(4, 'AWS for IDE', 'Overview'),
    page(5, 'IDE shortcuts', 'Keyboard shortcuts for the IDE.'),
    page(6, 'AWS Toolkit in the IDE', 'Plugin'),
    page(7, 'AWS user guide', 'Not about editors at all.'),
  ];
  const ranked = rankBookmarks(items, 'AWS for IDE', fields);
  assert.deepEqual(order(ranked).slice(0, 2), [4, 3], 'phrase in title beats phrase in content');
  assert.equal(ranked[0].tier, 3);
  assert.equal(ranked[0].phraseField, 'title');
  assert.deepEqual(order(ranked).slice(2, 4), [6, 2], 'all words, close together and in the title, before scattered');
  assert.ok(ranked.slice(2, 4).every(result => result.tier === 2));
  const partial = ranked.slice(4);
  assert.ok(partial.every(result => result.tier === 1));
  assert.equal(partial[0].item.id, 5, 'the rare word "IDE" outranks the common word "AWS"');
  assert.deepEqual(partial[0].missing, ['aws']);
  const guide = partial.find(result => result.item.id === 7);
  assert.deepEqual(guide.missing, ['ide'], '"guide" does not count as the word IDE');
});

test('only the selected fields are ranked; ties prefer more opened bookmarks', () => {
  const items = [page(1, 'Notes page', '', {views: 0}), page(2, 'Other page', '', {views: 12})];
  const notes = item => item.id === 1 ? 'aws for ide' : 'aws for ide';
  assert.deepEqual(order(rankBookmarks(items, 'aws for ide', ['notes'], notes)), [2, 1]);
  assert.equal(rankBookmarks(items, 'aws for ide', ['title'], notes)[0].tier, 1);
  assert.deepEqual(rankBookmarks(items, '   ', fields), []);
});

test('single-word searches put whole-word matches first', () => {
  const items = [page(1, 'Guide'), page(2, 'IDE'), page(3, 'IDEs compared')];
  const ranked = rankBookmarks(items, 'ide', fields);
  assert.deepEqual(order(ranked), [2, 3, 1]);
  assert.deepEqual(ranked.map(result => result.tier), [3, 2, 1]);
});

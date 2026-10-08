const test = require('node:test');
const assert = require('node:assert/strict');
const shortCount = require('../frontend/home/short-count.js');
test('counts shorten to k, lakh and crore', () => {
 for (const [value, text] of [[0,'0'],[7,'7'],[999,'999'],[1000,'1k'],[1250,'1.2k'],[2000,'2k'],[45678,'45k'],[99999,'99k'],[100000,'1L'],[250000,'2.5L'],[1234567,'12L'],[10000000,'1Cr'],[25000000,'2.5Cr']]) assert.equal(shortCount(value), text);
});

const test = require('node:test');
const assert = require('node:assert/strict');
const dates = require('../frontend/confluence-tracker/ranges.js');

test('relative date ranges use local calendar dates and exclusive end', () => {
  const now = new Date(2026, 8, 24, 12);
  for (const [name, start, end] of [['today','2026-09-24','2026-09-25'],['yesterday','2026-09-23','2026-09-24'],['before-yesterday','2026-09-22','2026-09-23'],['week','2026-09-21','2026-09-25'],['month','2026-09-01','2026-09-25'],['year','2026-01-01','2026-09-25']]) {
    const range = dates.preset(name, now);
    assert.equal(dates.key(range.start), start);
    assert.equal(dates.key(range.end), end);
    assert.equal(dates.inRange(range.start, range), true);
    assert.equal(dates.inRange(range.end, range), false);
  }
});
test('custom dates are inclusive, reject impossible or reversed dates', () => {
  const range = dates.custom('2026-03-28', '2026-03-30');
  assert.equal(dates.inRange(new Date(2026,2,30,23,59),range),true);
  assert.equal(dates.inRange(new Date(2026,2,31),range),false);
  assert.throws(()=>dates.custom('2026-02-30','2026-03-01'));
  assert.throws(()=>dates.custom('2026-03-30','2026-03-28'));
  assert.equal(dates.inRange(null,dates.preset('all')),true);
  assert.equal(dates.inRange(null,range),false);
});
test('grouping keeps recent days, months, and older years distinct', () => {
  const now = new Date(2026,8,24,12);
  for (const [date,label] of [[24,'Today'],[23,'Yesterday'],[22,'Day before yesterday'],[21,'This week'],[1,'This month']]) assert.equal(dates.group(new Date(2026,8,date),now),label);
  assert.equal(dates.group(new Date(2025,8,1),now),'2025');
  assert.equal(dates.group(null,now),'Date unavailable');
});

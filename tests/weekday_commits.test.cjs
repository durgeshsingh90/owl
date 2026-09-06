const test=require('node:test'),assert=require('node:assert/strict');
const counts=require('../frontend/home/commit-stats.js');
const doc={projectId:'1',repo:'one',commitId:'a',committedAt:'2026-09-06T12:00:00Z'};
const day=value=>{const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Dublin',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date(value));const get=t=>Number(parts.find(p=>p.type===t).value);return Date.UTC(get('year'),get('month')-1,get('day'));};
test('deduplicates commits per repo and buckets Dublin weekdays',()=>{
const result=counts([doc,doc,{...doc,repo:'two'},{...doc,commitId:'b',committedAt:'2026-09-06T23:30:00Z'},{...doc,commitId:null}],-Infinity,Infinity,day);
assert.deepEqual(result,[2,1,0,0,0,0,0]);});
test('period boundaries exclude other dates',()=>{assert.deepEqual(counts([doc],Date.UTC(2026,8,7),Date.UTC(2026,8,13),day),[0,0,0,0,0,0,0]);});

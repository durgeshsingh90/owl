const test = require('node:test');
const assert = require('node:assert/strict');
const P = require('../frontend/json-visualizer/parse.js');
const S = require('../frontend/json-visualizer/search.js');
const M = require('../frontend/json-visualizer/model.js');
const C = require('../frontend/json-visualizer/cloud.js');

test('detects JSON, JSON Lines and JSON5', () => {
 assert.equal(P.parseDocument('{"a":1}').format, 'json');
 const lines = P.parseDocument('{"a":1}\n{"a":2}\n');
 assert.equal(lines.format, 'jsonl'); assert.deepEqual(lines.value, [{a:1},{a:2}]);
 const five = P.parseDocument("// note\n{a: 'x', b: [1, 2,], c: 0x10, d: Infinity,}");
 assert.equal(five.format, 'json5'); assert.deepEqual(five.value, {a:'x', b:[1,2], c:16, d:Infinity});
 assert.equal(P.parseDocument('{"a":1}', {name:'x.json5'}).format, 'json5');
});
test('JSON Lines keeps valid lines and lists bad ones with line numbers', () => {
 const text = ['{"a":1}', 'oops', '{"a":2}', '{bad', '', '{"a":3}'].join('\n');
 const result = P.parseDocument(text, {name:'x.jsonl'});
 assert.equal(result.value.length, 3);
 assert.deepEqual(result.errors.map(error => error.line), [2, 4]);
 assert.deepEqual(result.lines, [1, 3, 6]);
});
test('parse errors give line, column and a snippet', () => {
 assert.throws(() => P.parseDocument('{\n  "a": 1,\n  "b": ]\n}'), error => error.line === 3 && error.column >= 8 && error.snippet.includes('"b"'));
 assert.throws(() => P.parseJson5('{a: 1\n b: 2}'), error => error.line === 2);
});
test('skips CLI noise before the JSON and decodes BOM and UTF-16', () => {
 const result = P.parseDocument('WARNING: something\nanother line\n{"ok": true}');
 assert.deepEqual(result.value, {ok:true}); assert.match(result.notices[0], /Skipped 2 lines/);
 assert.equal(P.decodeBytes(new Uint8Array([0xef,0xbb,0xbf,0x7b,0x7d])).text, '{}');
 const utf16 = new Uint8Array([0xff,0xfe,0x7b,0,0x7d,0]);
 assert.equal(P.decodeBytes(utf16).text, '{}');
});
test('search finds keys and values with options', () => {
 const doc = {Reservations:[{Instances:[{InstanceId:'i-1', Tags:[{Key:'Name', Value:'web'}]}]}], Web:1};
 const all = S.searcher(doc, 'web').next();
 assert.equal(all.done, true);
 assert.deepEqual(all.matches.map(match => [match.path.join('/'), match.on]), [['Reservations/0/Instances/0/Tags/0/Value','value'], ['Web','key']]);
 assert.equal(S.searcher(doc, 'web', {caseSensitive:true}).next().matches.length, 1);
 assert.equal(S.searcher(doc, 'web', {scope:'keys'}).next().matches.length, 1);
 assert.equal(S.searcher(doc, '^i-\\d$', {regex:true}).next().matches.length, 1);
 assert.equal(S.searcher({a:'webserver'}, 'web', {wholeWord:true}).next().matches.length, 0);
 const sliced = S.searcher({list: Array.from({length: 100}, (_, i) => i)}, '5');
 const first = sliced.next(10); assert.equal(first.done, false);
});
test('paths format as JS, JMESPath and jq and parse back', () => {
 const path = ['Reservations', 0, 'Instances', 2, 'my-key'];
 assert.equal(M.formatPath(path, 'js'), 'Reservations[0].Instances[2]["my-key"]');
 assert.equal(M.formatPath(path, 'jmes'), 'Reservations[0].Instances[2]."my-key"');
 assert.equal(M.formatPath(path, 'jq'), '.Reservations[0].Instances[2].["my-key"]');
 assert.equal(M.formatPath([0, 'a'], 'jq'), '.[0].a');
 assert.equal(M.formatPath([], 'jmes'), '@');
 for (const syntax of ['js', 'jmes', 'jq']) assert.deepEqual(M.parsePath(M.formatPath(path, syntax)), path);
 assert.deepEqual(M.parsePath('a.b.0'), ['a', 'b', 0]);
});
test('values: copy text, kinds, filters and CSV', () => {
 assert.equal(M.copyText('x'), 'x'); assert.equal(M.copyText({a:1}), '{\n  "a": 1\n}'); assert.equal(M.copyText({a:1}, true), '{"a":1}');
 assert.equal(M.stringKind('2024-05-01T10:00:00Z'), 'date');
 assert.equal(M.stringKind('arn:aws:iam::123456789012:role/x'), 'arn');
 assert.equal(M.stringKind('i-0abc1234def567890'), 'resource');
 assert.equal(M.stringKind('10.0.0.1'), 'ip'); assert.equal(M.stringKind('0.0.0.0/0'), 'ip');
 assert.equal(M.stringKind('https://x.test/a'), 'url'); assert.equal(M.stringKind('hello'), 'string');
 assert.ok(M.filterTest('>5')(6)); assert.ok(!M.filterTest('>5')(4)); assert.ok(M.filterTest('=running')('Running'));
 assert.ok(M.filterTest('!=a')('b')); assert.ok(M.filterTest('1..3')(2)); assert.ok(M.filterTest('empty')(undefined)); assert.ok(M.filterTest('!empty')('x'));
 assert.ok(M.filterTest('web')('my-web-1'));
 assert.equal(M.toCsv(['a','b'], [['1','x,y']]), 'a,b\r\n1,"x,y"\r\n');
 assert.deepEqual(M.columnKeys([{a:1, s:{n:'x'}}, {a:2, b:3}]), ['a', 's.n', 'b']);
 assert.equal(M.getField({State:{Name:'running'}}, 'State.Name'), 'running');
});
test('cloud: detect, unwrap, names, tags, status, templates', () => {
 const ec2 = {Reservations:[{Instances:[{InstanceId:'i-1', InstanceType:'t3.micro', Tags:[{Key:'Name', Value:'web'}], State:{Name:'running'}}]},{Instances:[{InstanceId:'i-2', InstanceType:'t3.micro'}]}]};
 assert.equal(C.detectSource(ec2), 'aws');
 const unwrapped = C.unwrap(ec2);
 assert.equal(unwrapped.rows.length, 2); assert.deepEqual(unwrapped.rows[1].path, ['Reservations', 1, 'Instances', 0]);
 assert.equal(C.displayName(unwrapped.rows[0].value), 'web'); assert.equal(C.displayName(unwrapped.rows[1].value), 'i-2');
 assert.equal(C.template(unwrapped.rows.map(row => row.value)).name, 'EC2 instances');
 const azure = [{id:'/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1', name:'vm1', hardwareProfile:{vmSize:'B2s'}, tags:{env:'prod'}}];
 assert.equal(C.detectSource(azure), 'azure'); assert.equal(C.template(azure).name, 'Azure VMs');
 assert.deepEqual(C.tags(azure[0]), [{key:'env', value:'prod'}]);
 assert.equal(C.detectSource({apiVersion:'v1', kind:'List', items:[{apiVersion:'v1', kind:'Pod', metadata:{name:'p'}}]}), 'kubectl');
 assert.deepEqual(C.unwrap({Vpcs:[{VpcId:'vpc-1'}], NextToken:'x'}).path, ['Vpcs']);
 assert.equal(C.statusTone('Name', 'running'), 'good'); assert.equal(C.statusTone('State', 'stopped'), 'warn');
 assert.equal(C.statusTone('provisioningState', 'Failed'), 'bad'); assert.equal(C.statusTone('Description', 'running'), null);
 assert.match(C.consoleLink('arn:aws:s3:::bucket').url, /console\.aws\.amazon\.com/);
});
test('a JSON5 file that starts with a comment is not treated as CLI noise', () => {
 const result = P.parseDocument('// settings\n{a: 1}');
 assert.equal(result.format, 'json5'); assert.deepEqual(result.notices, []);
});
test('integers beyond 2^53 are kept exactly as text', () => {
 const result = P.parseDocument('{"id": 12345678901234567890, "small": 5, "list": [9007199254740993]}');
 assert.equal(result.value.id, '12345678901234567890'); assert.equal(result.value.small, 5); assert.equal(result.value.list[0], '9007199254740993');
 assert.match(result.notices.join(' '), /Big integers/);
 assert.deepEqual(P.parseDocument('{"a": 1}').notices, []);
});

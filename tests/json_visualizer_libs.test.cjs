const test = require('node:test');
const assert = require('node:assert/strict');
const J = require('../frontend/json-visualizer/jmespath.js');
const Q = require('../frontend/json-visualizer/jq.js');
const E = require('../frontend/json-visualizer/exporters.js');
const S = require('../frontend/json-visualizer/secrets.js');
const D = require('../frontend/json-visualizer/diff.js');

// ---------- JMESPath ----------

const ec2 = {Reservations: [
 {Instances: [
  {InstanceId: 'i-1', PrivateIpAddress: '10.0.0.1', State: {Name: 'running'}, Tags: [{Key: 'Name', Value: 'web'}]},
  {InstanceId: 'i-2', PrivateIpAddress: '10.0.0.2', State: {Name: 'stopped'}}]},
 {Instances: [{InstanceId: 'i-3', PrivateIpAddress: '10.0.0.3', State: {Name: 'running'}}]},
 {Instances: [{InstanceId: 'i-4', State: {Name: 'stopped'}}]}]};

test('JMESPath: AWS CLI style queries', () => {
 assert.deepEqual(J.search(ec2, "Reservations[].Instances[?State.Name=='running'].[InstanceId, PrivateIpAddress]"),
  [[['i-1', '10.0.0.1']], [['i-3', '10.0.0.3']], []]);
 assert.deepEqual(J.search(ec2, "Reservations[].Instances[] | [?State.Name=='running'].InstanceId"), ['i-1', 'i-3']);
 assert.deepEqual(J.search(ec2, 'Reservations[].Instances[].InstanceId'), ['i-1', 'i-2', 'i-3', 'i-4']);
 assert.deepEqual(J.search(ec2, 'Reservations[*].Instances[*].InstanceId'), [['i-1', 'i-2'], ['i-3'], ['i-4']]);
 assert.deepEqual(J.search(ec2, 'Reservations[].Instances[].{Id: InstanceId, Ip: PrivateIpAddress}')[3], {Id: 'i-4', Ip: null});
 assert.deepEqual(J.search(ec2, "Reservations[].Instances[].Tags[?Key=='Name'].Value[]"), ['web']);
 assert.equal(J.search(ec2, 'length(Reservations[].Instances[])'), 4);
 assert.deepEqual(J.compile("Reservations[0].Instances[?State.Name=='stopped'].InstanceId | [0]")(ec2), 'i-2');
});

test('JMESPath: projections, flatten, filters, slices, multiselect, pipes, literals', () => {
 const data = {foo: [{bar: 1}, {baz: 2}, {bar: 3}], nested: [[1, 2], [3, [4]]], obj: {a: {v: 1}, b: {v: 2}, c: {}}, n: [0, 1, 2, 3, 4, 5]};
 assert.deepEqual(J.search(data, 'foo[*].bar'), [1, 3]);
 assert.deepEqual(J.search(data, 'nested[]'), [1, 2, 3, [4]]);
 assert.deepEqual(J.search(data, 'nested[][]'), [1, 2, 3, 4]);
 assert.deepEqual(J.search(data, 'obj.*.v'), [1, 2]);
 assert.deepEqual(J.search(data, 'foo[?bar > `1`]'), [{bar: 3}]);
 assert.deepEqual(J.search(data, 'n[1:4]'), [1, 2, 3]);
 assert.deepEqual(J.search(data, 'n[::-2]'), [5, 3, 1]);
 assert.deepEqual(J.search(data, 'n[-2:]'), [4, 5]);
 assert.deepEqual(J.search(data, '{first: n[0], last: n[-1]}'), {first: 0, last: 5});
 assert.deepEqual(J.search(data, 'foo[*].bar | [0]'), 1);
 assert.deepEqual(J.search(data, '`[1, {"a": 2}]`'), [1, {a: 2}]);
 assert.equal(J.search(data, "'raw \\' string'"), "raw ' string");
 assert.equal(J.search(data, '`"json"`'), 'json');
 assert.deepEqual(J.search(data, '@.n[0]'), 0);
 assert.deepEqual(J.search(data, 'foo[?!bar]'), [{baz: 2}]);
 assert.deepEqual(J.search(data, 'foo[?bar == `1` || baz == `2`]'), [{bar: 1}, {baz: 2}]);
 assert.deepEqual(J.search(data, 'foo[?(bar || baz) && !baz]'), [{bar: 1}, {bar: 3}]);
});

test('JMESPath: functions', () => {
 const people = [{name: 'b', age: 30, tags: ['x']}, {name: 'a', age: 20, tags: []}, {name: 'c', age: 40, tags: ['y', 'z']}];
 assert.deepEqual(J.search(people, 'sort_by(@, &age)[].name'), ['a', 'b', 'c']);
 assert.deepEqual(J.search(people, 'sort_by(@, &name)[].age'), [20, 30, 40]);
 assert.equal(J.search(people, 'max_by(@, &age).name'), 'c');
 assert.equal(J.search(people, 'min_by(@, &age).name'), 'a');
 assert.deepEqual(J.search(people, '[?length(tags) > `0`].name'), ['b', 'c']);
 assert.deepEqual(J.search(people, "[?contains(tags, 'y')].name"), ['c']);
 assert.equal(J.search(people, "join(', ', [].name)"), 'b, a, c');
 assert.equal(J.search(people, 'to_string(length(@))'), '3');
 assert.deepEqual(J.search({b: 1, a: 2}, 'keys(@)'), ['b', 'a']);
 assert.deepEqual(J.search(null, 'merge(`{"a": 1, "b": 1}`, `{"b": 2}`)'), {a: 1, b: 2});
 assert.equal(J.search({x: null, y: 'v'}, 'not_null(x, missing, y)'), 'v');
 assert.deepEqual(J.search(people, 'map(&age, @)'), [30, 20, 40]);
 assert.equal(J.search(people, 'sum(map(&age, @))'), 90);
 assert.ok(J.functions.includes('sort_by') && J.functions.length === 26);
});

test('JMESPath: null propagation from the spec', () => {
 assert.equal(J.search({a: 1}, 'b.c.d'), null);
 assert.equal(J.search({a: 'x'}, 'a.b'), null);
 assert.equal(J.search({a: [1]}, 'a.b'), null);
 assert.equal(J.search({a: {b: 1}}, 'a[0]'), null);
 assert.equal(J.search({a: {b: 1}}, 'a[*]'), null);
 assert.equal(J.search({a: [1]}, 'a.*'), null);
 assert.equal(J.search({a: 1}, 'a[]'), null);
 assert.equal(J.search({a: 1}, 'a[?b]'), null);
 assert.equal(J.search(null, 'missing.[a, b]'), null);
 assert.equal(J.search(null, 'missing.{a: b}'), null);
 assert.deepEqual(J.search({foo: {bar: 'x'}}, 'foo.[bar, missing]'), ['x', null]);
 assert.equal(J.search(null, '`1` < \'a\''), null);
 assert.equal(J.search(undefined, '@'), null);
});

test('JMESPath: parse errors carry the position', () => {
 assert.throws(() => J.search({}, 'foo[?bar == ]'), error => error.message === "Unexpected token ']' at position 12" && error.position === 12);
 assert.throws(() => J.compile('foo.'), error => error.position === 4);
 assert.throws(() => J.compile('foo[8:2:0:1]'), error => typeof error.position === 'number');
 assert.throws(() => J.compile('foo[?a==`1`'), /position 11/);
 assert.throws(() => J.compile('unknown_function(`1`)'), /Unknown function: unknown_function\(\) at position 0/);
 assert.throws(() => J.compile('abs(`1`, `2`)'), /abs\(\) takes 1 argument/);
 assert.throws(() => J.compile('a = b'), /position 2/);
 assert.throws(() => J.search({foo: [1]}, 'foo[8:2:0]'), /step cannot be 0/);
 assert.throws(() => J.search({}, "abs('x')"), /abs\(\) expected argument 1 to be type number but received type string/);
});

// A sample of the official compliance suite (basic, current, indices, slice, filters,
// boolean, wildcard, multiselect, pipe, literal and functions).
const compliance = [
 [{foo: {bar: {baz: 'correct'}}}, [
  ['foo', {bar: {baz: 'correct'}}], ['foo.bar', {baz: 'correct'}], ['foo.bar.baz', 'correct'], ['foo\n.\nbar\n.baz', 'correct'],
  ['foo.bar.baz.bad', null], ['foo.bar.bad', null], ['foo.bad', null], ['bad', null], ['bad.morebad.morebad', null], ['@.foo.bar', {baz: 'correct'}]]],
 [['one', 'two', 'three'], [['one', null], ['two', null], ['one.two', null], ['[0]', 'one'], ['[-1]', 'three'], ['[-3]', 'one']]],
 [{foo: {'1': ['one', 'two', 'three'], '-1': 'bar'}}, [['foo."1"', ['one', 'two', 'three']], ['foo."1"[0]', 'one'], ['foo."-1"', 'bar']]],
 [{foo: {bar: ['zero', 'one', 'two']}}, [
  ['foo.bar[0]', 'zero'], ['foo.bar[1]', 'one'], ['foo.bar[2]', 'two'], ['foo.bar[3]', null], ['foo.bar[-1]', 'two'], ['foo.bar[-2]', 'one'],
  ['foo.bar[-3]', 'zero'], ['foo.bar[-4]', null]]],
 [{foo: [{bar: 'one'}, {bar: 'two'}, {bar: 'three'}, {notbar: 'four'}]}, [
  ['foo.bar', null], ['foo[0].bar', 'one'], ['foo[3].notbar', 'four'], ['foo[3].bar', null], ['foo[0]', {bar: 'one'}], ['foo[4]', null], ['foo[*].bar', ['one', 'two', 'three']]]],
 [{reservations: [{instances: [{foo: 1}, {foo: 2}]}]}, [
  ['reservations[].instances[].foo', [1, 2]], ['reservations[].instances[].bar', []], ['reservations[].notinstances[].foo', []]]],
 [{foo: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], bar: {baz: 1}}, [
  ['bar[0:10]', null], ['foo[0:10:1]', [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]], ['foo[0:10]', [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]], ['foo[0::1]', [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]],
  ['foo[:]', [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]], ['foo[1:9]', [1, 2, 3, 4, 5, 6, 7, 8]], ['foo[0:10:2]', [0, 2, 4, 6, 8]], ['foo[5:]', [5, 6, 7, 8, 9]],
  ['foo[5::2]', [5, 7, 9]], ['foo[::2]', [0, 2, 4, 6, 8]], ['foo[::-1]', [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]], ['foo[1::2]', [1, 3, 5, 7, 9]],
  ['foo[10:0:-1]', [9, 8, 7, 6, 5, 4, 3, 2, 1]], ['foo[10:5:-1]', [9, 8, 7, 6]], ['foo[8:2:-2]', [8, 6, 4]], ['foo[0:20]', [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]],
  ['foo[10:-20:-1]', [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]], ['foo[-4:-1]', [6, 7, 8]], ['foo[:-5:-1]', [9, 8, 7, 6]]]],
 [{foo: [{a: 1}, {a: 2}, {a: 3}], bar: [{a: {b: 1}}, {a: {b: 2}}, {a: {b: 3}}], baz: 50}, [
  ['foo[:2].a', [1, 2]], ['foo[:2].b', []], ['foo[:2].a.b', []], ['bar[::-1].a.b', [3, 2, 1]], ['bar[:2].a.b', [1, 2]], ['baz[:2].a', null]]],
 [{foo: [{first: 'foo', last: 'bar'}, {first: 'foo', last: 'foo'}, {first: 'foo', last: 'baz'}]}, [
  ['foo[?first == last]', [{first: 'foo', last: 'foo'}]], ['foo[?first == last].first', ['foo']]]],
 [{foo: [{age: 20}, {age: 25}, {age: 30}]}, [
  ['foo[?age > `25`]', [{age: 30}]], ['foo[?age >= `25`]', [{age: 25}, {age: 30}]], ['foo[?age > `30`]', []], ['foo[?age < `25`]', [{age: 20}]],
  ['foo[?age <= `25`]', [{age: 20}, {age: 25}]], ['foo[?age < `20`]', []], ['foo[?age == `20`]', [{age: 20}]], ['foo[?age != `20`]', [{age: 25}, {age: 30}]]]],
 [{foo: [{top: {first: 'foo', last: 'bar'}}, {top: {first: 'foo', last: 'foo'}}]}, [
  ['foo[?top.first == top.last]', [{top: {first: 'foo', last: 'foo'}}]], ['foo[?top == `{"first": "foo", "last": "bar"}`]', [{top: {first: 'foo', last: 'bar'}}]]]],
 [{foo: [{key: true}, {key: false}, {key: 0}, {key: 1}, {key: [0]}, {key: {bar: [0]}}, {key: null}, {key: [1]}, {key: {a: 2}}]}, [
  ['foo[?key == `true`]', [{key: true}]], ['foo[?key == `false`]', [{key: false}]], ['foo[?key == `0`]', [{key: 0}]], ['foo[?key == `[0]`]', [{key: [0]}]],
  ['foo[?key == `{"bar": [0]}`]', [{key: {bar: [0]}}]], ['foo[?key == `null`]', [{key: null}]], ['foo[?`true` == key]', [{key: true}]]]],
 [{reservations: [{instances: [{foo: 1, bar: 2}, {foo: 1, bar: 3}, {foo: 1, bar: 2}, {foo: 2, bar: 1}]}]}, [
  ['reservations[].instances[?bar==`1`]', [[{foo: 2, bar: 1}]]], ['reservations[*].instances[?bar==`1`]', [[{foo: 2, bar: 1}]]], ['reservations[].instances[?bar==`1`][]', [{foo: 2, bar: 1}]]]],
 [{foo: [{a: 1, b: {c: 'x'}}, {a: 1, b: {c: 'y'}}, {a: 1, b: {c: 'z'}}, {a: 2, b: {c: 'z'}}, {a: 1, baz: 2}]}, [['foo[?a==`1`].b.c', ['x', 'y', 'z']]]],
 [{foo: [{a: 1, b: 2, c: 3}, {a: 3, b: 4}]}, [
  ['foo[?c == `3` || a == `1` && b == `4`]', [{a: 1, b: 2, c: 3}]], ['foo[?b == `2` || a == `3` && b == `4`]', [{a: 1, b: 2, c: 3}, {a: 3, b: 4}]],
  ['foo[?a == `3` && b == `4` || b == `2`]', [{a: 1, b: 2, c: 3}, {a: 3, b: 4}]], ['foo[?(a == `3` && b == `4`) || b == `2`]', [{a: 1, b: 2, c: 3}, {a: 3, b: 4}]],
  ['foo[?a == `3` && (b == `4` || b == `2`)]', [{a: 3, b: 4}]], ['foo[?!(a == `1` || b == `2`)]', [{a: 3, b: 4}]]]],
 [{True: true, False: false, Number: 5, EmptyList: [], Zero: 0}, [
  ['True && False', false], ['False && True', false], ['True && True', true], ['False || True', true], ['Number && EmptyList', []], ['Number || EmptyList', 5],
  ['EmptyList || Number', 5], ['Zero || Number', 0], ['!True', false], ['!EmptyList', true], ['!Number', false], ['!Zero', false], ['!!Zero', true]]],
 [{foo: {bar: {baz: 'val'}, other: {baz: 'val'}}}, [['foo.*.baz', ['val', 'val']], ['foo.*', [{baz: 'val'}, {baz: 'val'}]], ['*.bar', [{baz: 'val'}]], ['foo[*]', null], ['foo.*.baz | [0]', 'val']]],
 [{foo: [{bar: [1, 2]}, {bar: [3]}]}, [['foo[*].bar[0]', [1, 3]], ['foo[*].bar | [0]', [1, 2]], ['foo[].bar[]', [1, 2, 3]], ['foo[].bar[] | length(@)', 3]]],
 [{foo: {bar: 'bar', baz: 'baz', qux: 'qux'}}, [
  ['foo.{bar: bar, baz: baz}', {bar: 'bar', baz: 'baz'}], ['foo.[bar, baz]', ['bar', 'baz']], ['foo.{"bar": bar}', {bar: 'bar'}], ['foo.[bar]', ['bar']],
  ['[foo.bar, foo.qux]', ['bar', 'qux']], ['{a: foo.bar, b: `1`}', {a: 'bar', b: 1}]]],
 [null, [['`foo`', 'foo'], ['`"\\u03a6"`', '\u03a6'], ['`[1, 2, 3]`[1]', 2], ['`{"a": {"b": 1}}`.a.b', 1], ["'foo'", 'foo'], ["'\\''", "'"], ['`true`', true]]],
 [{foo: -1, zero: 0, numbers: [-1, 3, 4, 5], array: [-1, 3, 4, 5, 'a', '100'], strings: ['a', 'b', 'c'], decimals: [1.01, 1.2, -1.5], str: 'Str', false: false, empty_list: [], empty_hash: {}, objects: {foo: 'bar', bar: 'baz'}, null_key: null}, [
  ['abs(foo)', 1], ['abs(`-24`)', 24], ['avg(numbers)', 2.75], ['ceil(`1.2`)', 2], ['ceil(decimals[2])', -1], ['contains(\'abc\', \'a\')', true], ['contains(\'abc\', \'d\')', false],
  ['contains(strings, \'a\')', true], ['contains(decimals, `1.01`)', true], ['contains(decimals, `false`)', false], ['ends_with(str, \'r\')', true], ['ends_with(str, \'SStr\')', false],
  ['floor(`1.2`)', 1], ['floor(foo)', -1], ['length(\'abc\')', 3], ['length(\'\u2713foo\')', 4], ['length(\'\')', 0], ['length(@)', 12], ['length(array)', 6], ['length(objects)', 2],
  ['max(numbers)', 5], ['max(decimals)', 1.2], ['max(strings)', 'c'], ['max(empty_list)', null], ['merge(`{}`)', {}], ['merge(`{"a": 1}`, `{"a": 2}`)', {a: 2}],
  ['merge(`{"a": 1, "b": 2}`, `{"a": 2, "c": 3}`, `{"d": 4}`)', {a: 2, b: 2, c: 3, d: 4}], ['min(numbers)', -1], ['min(decimals)', -1.5], ['min(strings)', 'a'], ['min(empty_list)', null],
  ['type(\'abc\')', 'string'], ['type(`1.0`)', 'number'], ['type(`true`)', 'boolean'], ['type(`null`)', 'null'], ['type(`[0]`)', 'array'], ['type(@)', 'object'],
  ['sort(keys(objects))', ['bar', 'foo']], ['keys(empty_hash)', []], ['sort(values(objects))', ['bar', 'baz']], ['join(\', \', strings)', 'a, b, c'], ['join(\',\', `["a", "b"]`)', 'a,b'],
  ['join(\'|\', decimals[].to_string(@))', '1.01|1.2|-1.5'], ['join(\'|\', empty_list)', ''], ['reverse(numbers)', [5, 4, 3, -1]], ['reverse(array)', ['100', 'a', 5, 4, 3, -1]],
  ['reverse(\'hello world\')', 'dlrow olleh'], ['starts_with(str, \'St\')', true], ['starts_with(str, \'String\')', false], ['sum(numbers)', 11], ['sum(array[].to_number(@))', 111],
  ['sum(`[]`)', 0], ['to_array(\'foo\')', ['foo']], ['to_array(`[1, 2, 3]`)', [1, 2, 3]], ['to_string(\'foo\')', 'foo'], ['to_string(`1.2`)', '1.2'], ['to_string(`[0, 1]`)', '[0,1]'],
  ['to_number(\'1.0\')', 1], ['to_number(\'1.1\')', 1.1], ['to_number(\'notanumber\')', null], ['to_number(`false`)', null], ['not_null(unknown_key, str)', 'Str'],
  ['not_null(unknown_key, null_key, empty_list, str)', []], ['not_null(all, expressions, are_null)', null], ['numbers[].to_string(@)', ['-1', '3', '4', '5']],
  ['array[].to_number(@)', [-1, 3, 4, 5, 100]], ['sort(numbers)', [-1, 3, 4, 5]], ['sort(strings)', ['a', 'b', 'c']], ['sort(decimals)', [-1.5, 1.01, 1.2]], ['sort(empty_list)', []]]],
 [{people: [{age: 20, age_str: '20', bool: true, name: 'a', extra: 'foo'}, {age: 40, age_str: '40', bool: false, name: 'b', extra: 'bar'}, {age: 30, age_str: '30', bool: true, name: 'c'},
  {age: 50, age_str: '50', bool: false, name: 'd'}, {age: 10, age_str: '10', bool: true, name: 3}]}, [
  ['sort_by(people, &age)[].age', [10, 20, 30, 40, 50]], ['sort_by(people, &age_str)[].age', [10, 20, 30, 40, 50]], ['sort_by(people, &to_number(age_str))[].age', [10, 20, 30, 40, 50]],
  ['sort_by(people, &age)[].name', [3, 'a', 'c', 'b', 'd']], ['max_by(people, &age).name', 'd'], ['max_by(people, &age_str).name', 'd'], ['min_by(people, &age).age', 10],
  ['map(&name, people)', ['a', 'b', 'c', 'd', 3]], ['map(&foo, people)', [null, null, null, null, null]], ['map(&[], `[[1, 2, 3, [4]], [5, 6, 7, [8, 9]]]`)', [[1, 2, 3, 4], [5, 6, 7, 8, 9]]]]]
];

const complianceErrors = [
 [{foo: -1, array: [1, 'a'], str: 'x', strings: ['a']}, ['abs(array)', 'abs(`false`)', 'avg(array)', 'avg(\'abc\')', 'ceil(\'string\')', 'contains(`false`, \'d\')', 'ends_with(str, `0`)',
  'length(`false`)', 'length(foo)', 'max(array)', 'keys(foo)', 'keys(strings)', 'join(\',\', `["a", 0]`)', 'join(`2`, strings)', 'sort(array)', 'sum(array)', 'not_null()',
  'to_string()', 'foo[8:2&]', 'foo[2:a:3]', 'foo..bar', '.foo', 'foo[', '"foo"(@)']],
 [{people: [{age: 20, name: 'a', bool: true}, {age: 30, name: 1, bool: false}]}, ['sort_by(people, &extra)', 'sort_by(people, &bool)', 'sort_by(people, &name)', 'sort_by(people, name)',
  'max_by(people, &bool)', 'max_by(people, &extra)']]
];

test('JMESPath: compliance sample', () => {
 let count = 0;
 for (const [data, cases] of compliance) {
  for (const [expression, expected] of cases) {
   assert.deepEqual(J.search(data, expression), expected, expression);
   count++;
  }
 }
 for (const [data, expressions] of complianceErrors) {
  for (const expression of expressions) {
   assert.throws(() => J.search(data, expression), Error, expression);
   count++;
  }
 }
 assert.ok(count >= 230, `${count} compliance cases`);
});

// ---------- jq ----------

const jqCases = [
 [{a: {b: 'x'}}, '.a.b', ['x']],
 [{a: {b: 'x'}}, '."a"."b"', ['x']],
 [{'a-b': 1}, '.["a-b"]', [1]],
 [[1, 2, 3], '.[0], .[-1]', [1, 3]],
 [[1, 2, 3, 4], '.[1:3]', [[2, 3]]],
 ['hello', '.[1:]', ['ello']],
 [[1, 2, 3], '.[]', [1, 2, 3]],
 [{a: 1, b: 2}, '.[]', [1, 2]],
 [5, '.a?', []],
 [5, '[.[]?]', [[]]],
 [{a: [1, {b: 2}]}, '[..|numbers]', [[1, 2]]],
 [[1, 2, 3], 'map(. * 10)', [[10, 20, 30]]],
 [[1, 5, 3], '[.[] | select(. > 2)]', [[5, 3]]],
 [{name: 'x', id: 1, extra: true}, '{name, id}', [{name: 'x', id: 1}]],
 [{k: 'key', v: 2}, '{(.k): .v, "lit": 1, $__loc__}', [{key: 2, lit: 1, __loc__: {file: '<stdin>', line: 1}}]],
 [{user: 'u', titles: ['a', 'b']}, '{user, title: .titles[]}', [{user: 'u', title: 'a'}, {user: 'u', title: 'b'}]],
 [null, '[1, "a", null] | length', [3]],
 [{name: 'web', n: 2}, '"\\(.name)-\\(.n + 1)"', ['web-3']],
 [null, '"tab\\tquote\\"u\\u00e9"', ['tab\tquote"u\u00e9']],
 [{a: null, b: false, c: 0}, '.a // "d", .b // "e", .c // "f"', ['d', 'e', 0]],
 [null, '(1, null, 2) // 3', [1, 2]],
 [5, 'if . < 3 then "small" elif . < 10 then "medium" else "large" end', ['medium']],
 [1, 'if . == 1 then "one" end', ['one']],
 [2, 'if . == 1 then "one" end', [2]],
 [[1, 2, 3, 4], 'reduce .[] as $x (0; . + $x)', [10]],
 [[[1, 2], [3, 4]], 'reduce .[] as [$a, $b] ({}; . + {($a | tostring): $b})', [{1: 2, 3: 4}]],
 [{a: 1, b: 2}, '.a as $x | .b as $y | $x + $y', [3]],
 [{a: {b: 5}}, '. as {a: {b: $v}} | $v', [5]],
 [{b: 1, a: 2}, 'keys, keys_unsorted, length', [['a', 'b'], ['b', 'a'], 2]],
 [[{n: 'b', v: 2}, {n: 'a', v: 1}, {n: 'c', v: 1}], 'sort_by(.v) | map(.n)', [['a', 'c', 'b']]],
 [[{n: 'b', v: 2}, {n: 'a', v: 1}, {n: 'c', v: 1}], 'sort_by(.v, .n) | map(.n)', [['a', 'c', 'b']]],
 [[{t: 'x', v: 1}, {t: 'y', v: 2}, {t: 'x', v: 3}], 'group_by(.t) | map({t: .[0].t, total: map(.v) | add})', [[{t: 'x', total: 4}, {t: 'y', total: 2}]]],
 [[{t: 'x', v: 1}, {t: 'y', v: 2}, {t: 'x', v: 3}], 'unique_by(.t) | map(.v)', [[1, 2]]],
 [[3, 1, 2, 1], 'unique, sort, min, max, reverse', [[1, 2, 3], [1, 1, 2, 3], 1, 3, [1, 2, 1, 3]]],
 [[{v: 1}, {v: 3}, {v: 2}], 'min_by(.v), max_by(.v)', [{v: 1}, {v: 3}]],
 [{a: 1, b: 2}, 'to_entries', [[{key: 'a', value: 1}, {key: 'b', value: 2}]]],
 [[{Key: 'Name', Value: 'web'}, {Key: 'Env', Value: 'prod'}], 'from_entries', [{Name: 'web', Env: 'prod'}]],
 [[{name: 'a', value: 1}, {k: 'b', v: 2}], 'from_entries', [{a: 1, b: 2}]],
 [{a: 1, b: 2}, 'with_entries(select(.value > 1) | .key |= ascii_upcase)', [{B: 2}]],
 ['test 123 abc 45', '[match("\\\\d+"; "g") | .string]', [['123', '45']]],
 ['test 123', 'test("\\\\d+"), test("ABC"; "i"), test("TEST"; "i")', [true, false, true]],
 ['2024-05-06', 'capture("(?<y>\\\\d+)-(?<m>\\\\d+)")', [{y: '2024', m: '05'}]],
 ['aXbXc', 'sub("X"; "-"), gsub("X"; "-"), gsub("(?<c>[a-c])"; "<\\(.c)>")', ['a-bXc', 'a-b-c', '<a>X<b>X<c>']],
 ['a,b,c', 'split(","), (split(",") | join("|"))', [['a', 'b', 'c'], 'a|b|c']],
 [['a', 1, null, true], 'join("-")', ['a-1--true']],
 [[1, 'a "q"', null, false], '@csv', ['1,"a ""q""",,false']],
 [['a\tb', 'c\\d', null], '@tsv', ['a\\tb\tc\\\\d\t']],
 [[{n: 'x', c: 1}, {n: 'y', c: 2}], '.[] | [.n, .c] | @csv', ['"x",1', '"y",2']],
 [{a: 1, b: {c: 2, d: 3}}, 'del(.b.c), del(.a, .b)', [{a: 1, b: {d: 3}}, {}]],
 [[1, 2, 3, 4], 'del(.[1, 2]), del(.[] | select(. > 2))', [[1, 4], [1, 2]]],
 [{a: [1, {b: null}]}, '[paths], [leaf_paths], [paths(type == "number")]', [[['a'], ['a', 0], ['a', 1], ['a', 1, 'b']], [['a', 0], ['a', 1, 'b']], [['a', 0]]]],
 [{a: {b: 1}}, 'getpath(["a", "b"]), getpath(["x", "y"]), setpath(["a", "c"]; 2), path(.a.b)', [1, null, {a: {b: 1, c: 2}}, ['a', 'b']]],
 [{a: {b: 1}, c: [1, 2]}, '.a.b |= . + 1 | .c[0] = 9 | .d += 1', [{a: {b: 2}, c: [9, 2], d: 1}]],
 [null, 'try error("boom") catch ., try (1 / 0) catch "div", (try error({x: 1}) catch .x)', ['boom', 'div', 1]],
 [{a: 'x'}, '[.a | try tonumber catch "nan"], [.[] | tonumber?]', [['nan'], []]],
 [3, 'def inc: . + 1; def addn($n): . + $n; def twice(f): f | f; inc, addn(10), twice(inc), twice(addn(2))', [4, 13, 5, 7]],
 [5, 'def fact: if . <= 1 then 1 else . * (. - 1 | fact) end; fact', [120]],
 [null, 'def f: if . < 5000 then . + 1 | f else . end; 0 | f', [5000]],
 [null, '[range(3)], [range(1; 7; 2)], [limit(2; range(10))], first(range(5; 9)), [first, last] | tostring', ['[0,1,2]', '[1,3,5]', '[0,1]', '5', '[null,null]']],
 [[1, [2, [3, [4]]]], 'flatten, flatten(1), length', [[1, 2, 3, 4], [1, 2, [3, [4]]], 2]],
 [[[1], [2, 3]], 'add, any(. == [1]), all(length > 1)', [[1, 2, 3], true, false]],
 [{a: 1}, 'has("a"), has("b"), ("a" | in({a: 1})), contains({a: 1}), ({a: 1} | inside({a: 1, b: 2}))', [true, false, true, true, true]],
 ['foobar', 'startswith("foo"), endswith("bar"), ltrimstr("foo"), rtrimstr("bar"), ascii_upcase, length, utf8bytelength', [true, true, 'bar', 'foo', 'FOOBAR', 6, 6]],
 [[1, 2], 'tojson, (tojson | fromjson), tostring, ("12" | tonumber), type', ['[1,2]', [1, 2], '[1,2]', 12, 'array']],
 [[1, 'a', null, {}, [], true], '[.[] | numbers], [.[] | strings], [.[] | nulls], [.[] | iterables | type], [.[] | scalars | type], [.[] | booleans]',
  [[1], ['a'], [null], ['object', 'array'], ['number', 'string', 'null', 'boolean'], [true]]],
 ['<a href="x">&\'', '@html, @uri, @base64, (@base64 | @base64d), @json, @text', ['&lt;a href=&quot;x&quot;&gt;&amp;&#39;', '%3Ca%20href%3D%22x%22%3E%26%27', 'PGEgaHJlZj0ieCI+Jic=', '<a href="x">&\'', '"<a href=\\"x\\">&\'"', '<a href="x">&\'']],
 [{x: 'a b'}, '@uri "q=\\(.x)", @base64 "\\(.x)"', ['q=a%20b', 'YSBi']],
 [1425599621, 'todate, (todate | fromdate), ("2015-03-05T23:53:41Z" | fromdate)', ['2015-03-05T23:53:41Z', 1425599621, 1425599621]],
 [{a: [1, 2]}, '[.a[] as $x | $x * 2], (.a | indices(2)), ("a,b, c" | index(","), rindex(","))', [[2, 4], [1], 1, 3]],
 [null, '{} + {a: 1}, [1] + [2], "a" + "b", null + 1, [1, 2, 1] - [1], {a: {b: 1}} * {a: {c: 2}}, 7 % 3, "a,b" / ","',
  [{a: 1}, [1, 2], 'ab', 1, [2], {a: {b: 1, c: 2}}, 1, ['a', 'b']]],
 [null, '[(1, 2) + (10, 20)], [1, 2] == [1, 2], (null < false), ([] < {}), ("a" < "b"), (1 != 1)', [[11, 12, 21, 22], true, true, true, true, false]],
 [true, 'not, (true and false), (false or true), ([] | not)', [false, false, true, false]],
 [{a: {b: {c: 1}}}, '[recurse | type], [recurse(.[]?; type == "object") | keys[0]]', [['object', 'object', 'object', 'number'], ['a', 'b', 'c']]],
 [[1, 2, 3], 'map_values(. + 1), (to_entries | map(.key)), ([foreach .[] as $x (0; . + $x)]), ([.[] | select(. != 2)] | length)', [[2, 3, 4], [0, 1, 2], [1, 3, 6], 2]],
 [[[1, 2], [3, 4]], 'transpose, (map(add) | add), (.[0] | first, last), nth(1; .[][])', [[[1, 3], [2, 4]], 10, 1, 2, 2]],
 [{a: 'x'}, '$ENV, env, (.a | ascii_downcase), ("abc" | explode | implode), ([["a", "b"], ["c"]] | [combinations | join("")])', [{}, {}, 'x', 'abc', ['ac', 'bc']]],
 [null, '[limit(3; repeat(1))], ([1, 2] | until(length > 4; . + .)), [3 | while(. > 0; . - 1)]', [[null, 1, 1], [1, 2, 1, 2, 1, 2, 1, 2], [3, 2, 1]]],
 [{a: [3, 1, 2]}, '.a | sort | walk(if type == "number" then . * 2 else . end), (.[1:] |= map(. * 10))', [[2, 4, 6], [1, 20, 30]]],
 [[1, 2, 3], '[.[] | (. as $n | if $n % 2 == 0 then "even" else empty end)], isempty(empty), isempty(1)', [['even'], true, false]]
];

test('jq: programs from the manual', () => {
 let count = 0;
 for (const [input, program, expected] of jqCases) {
  assert.deepEqual(Q.run(input, program), expected, program);
  count++;
 }
 assert.ok(count >= 70, `${count} jq cases`);
});

test('jq: errors, limits and AWS-shaped input', () => {
 assert.throws(() => Q.run({a: 1}, '.a.b'), /Cannot index number with "b"/);
 assert.throws(() => Q.run(5, '.[]'), /Cannot iterate over number \(5\)/);
 assert.throws(() => Q.run(null, '{} + 1'), /object \(\{\}\) and number \(1\) cannot be added/);
 assert.throws(() => Q.run(null, 'error("custom")'), error => error.message === 'custom' && error.value === 'custom');
 assert.throws(() => Q.run(null, '1 +'), error => error.position === 3 && /Unexpected end of program/.test(error.message));
 assert.throws(() => Q.run(null, 'nosuchfn(1)'), /nosuchfn\/1 is not defined at position 0/);
 assert.throws(() => Q.run(null, '$nope'), /\$nope is not defined/);
 assert.throws(() => Q.run(null, '.a | ]'), /Unexpected '\]' at position 5/);
 assert.throws(() => Q.run(null, '"unterminated'), /Unterminated string/);
 assert.throws(() => Q.run(null, 'def f: f; f'), /too deeply|too long/);
 const many = Q.run(null, 'range(200000)');
 assert.equal(many.length, 100000);
 assert.equal(many.truncated, true);
 assert.deepEqual(Q.run(null, '[limit(5; repeat(.))] | length'), [5]);
 assert.deepEqual(Q.run(ec2, '[.Reservations[].Instances[] | select(.State.Name == "running") | .InstanceId]'), [['i-1', 'i-3']]);
 assert.deepEqual(Q.run(ec2, '.Reservations[].Instances[] | select(.Tags) | (.Tags | from_entries).Name'), ['web']);
 assert.deepEqual(Q.run(ec2, '[.Reservations[].Instances[]] | group_by(.State.Name) | map({state: .[0].State.Name, count: length})'), [[{state: 'running', count: 2}, {state: 'stopped', count: 2}]]);
 assert.deepEqual(Q.compile('.Reservations | length')(ec2), [3]);
 assert.ok(Q.builtins.includes('to_entries') && Q.builtins.includes('gsub'));
});

// ---------- Exporters ----------

test('YAML quotes what needs quoting and keeps the rest plain', () => {
 const yaml = E.toYaml({
  plain: 'hello world', empty: '', yes: 'yes', no: 'No', truthy: 'true', nul: 'null', tilde: '~', num: '123', float: '1.5e3', hex: '0x1F', date: '2024-01-02',
  lead: ' x', trail: 'x ', colon: 'a: b', hash: 'a #b', dash: '-x', question: '?x', star: '*ref', amp: '&a', bang: '!tag', brace: '{x}', bracket: '[x]', quote: '"q"', pct: '%x', at: '@x',
  arn: 'arn:aws:s3:::bucket', url: 'https://example.com/a?b=c', ip: '10.0.0.1', number: 42, neg: -1.5, bool: false, nothing: null, list: [], obj: {},
  'needs: quote': 1, multi: 'line one\nline two', trailingNewline: 'a\nb\n', nested: {a: [1, {b: 'c', d: [true]}], e: [[1, 2]]}
 });
 const lines = yaml.split('\n');
 const has = line => assert.ok(lines.includes(line), `missing ${JSON.stringify(line)} in\n${yaml}`);
 has('plain: hello world'); has('empty: ""'); has('"yes": "yes"'); has('"no": "No"'); has('truthy: "true"'); has('nul: "null"'); has('tilde: "~"');
 has('num: "123"'); has('float: "1.5e3"'); has('hex: "0x1F"'); has('date: "2024-01-02"'); has('lead: " x"'); has('trail: "x "'); has('colon: "a: b"'); has('hash: "a #b"');
 has('dash: "-x"'); has('question: "?x"'); has('star: "*ref"'); has('amp: "&a"'); has('bang: "!tag"'); has('brace: "{x}"'); has('bracket: "[x]"'); has('quote: "\\"q\\""');
 has('pct: "%x"'); has('at: "@x"'); has('arn: arn:aws:s3:::bucket'); has('url: https://example.com/a?b=c'); has('ip: 10.0.0.1'); has('number: 42'); has('neg: -1.5');
 has('bool: false'); has('nothing: null'); has('list: []'); has('obj: {}'); has('"needs: quote": 1');
 has('multi: |-'); has('  line one'); has('  line two'); has('trailingNewline: |');
 assert.match(yaml, /nested:\n {2}a:\n {4}- 1\n {4}- b: c\n {6}d:\n {8}- true\n {2}e:\n {4}- - 1\n {6}- 2\n$/);
 assert.equal(E.toYaml([]), '[]\n');
 assert.equal(E.toYaml('true'), '"true"\n');
 assert.equal(E.toYaml(7), '7\n');
 assert.equal(E.toYaml([{a: 1, b: 2}, 'x']), '- a: 1\n  b: 2\n- x\n');
 assert.equal(E.toYaml({s: 'ctrl\u0001'}), 's: "ctrl\\u0001"\n');
 assert.equal(E.toYaml({s: 'cr\r\nlf'}), 's: "cr\\r\\nlf"\n');
});

test('Markdown tables escape pipes and newlines', () => {
 assert.equal(E.toMarkdownTable(['Name', 'Note'], [['a|b', 'line1\nline2'], [null, 3], [{x: 1}]]),
  '| Name | Note |\n| --- | --- |\n| a\\|b | line1<br>line2 |\n|  | 3 |\n| {"x":1} |  |\n');
});

function crc32(bytes) {
 let crc = ~0;
 for (const byte of bytes) {
  crc ^= byte;
  for (let k = 0; k < 8; k++) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
 }
 return (~crc) >>> 0;
}

function readZip(bytes) {
 const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
 const files = {};
 let at = 0;
 while (view.getUint32(at, true) === 0x04034b50) {
  const method = view.getUint16(at + 8, true), crc = view.getUint32(at + 14, true), size = view.getUint32(at + 18, true);
  const raw = view.getUint32(at + 22, true), nameLength = view.getUint16(at + 26, true), extra = view.getUint16(at + 28, true);
  const name = new TextDecoder().decode(bytes.subarray(at + 30, at + 30 + nameLength));
  const data = bytes.subarray(at + 30 + nameLength + extra, at + 30 + nameLength + extra + size);
  assert.equal(method, 0, `${name} is stored`);
  assert.equal(size, raw);
  assert.equal(crc32(data), crc, `CRC of ${name}`);
  files[name] = new TextDecoder().decode(data);
  at += 30 + nameLength + extra + size;
 }
 let end = bytes.length - 22;
 while (end >= 0 && view.getUint32(end, true) !== 0x06054b50) end--;
 assert.ok(end >= 0, 'end of central directory record');
 const entries = view.getUint16(end + 10, true), cdSize = view.getUint32(end + 12, true), cdOffset = view.getUint32(end + 16, true);
 assert.equal(cdOffset, at, 'central directory follows the entries');
 assert.equal(cdOffset + cdSize, end, 'central directory size');
 let cd = cdOffset;
 for (let i = 0; i < entries; i++) {
  assert.equal(view.getUint32(cd, true), 0x02014b50);
  const nameLength = view.getUint16(cd + 28, true), extra = view.getUint16(cd + 30, true), comment = view.getUint16(cd + 32, true);
  const name = new TextDecoder().decode(bytes.subarray(cd + 46, cd + 46 + nameLength));
  assert.ok(name in files, `${name} in central directory`);
  assert.equal(view.getUint32(cd + 16, true), crc32(new TextEncoder().encode(files[name])));
  cd += 46 + nameLength + extra + comment;
 }
 assert.equal(entries, Object.keys(files).length);
 return files;
}

test('xlsx is a valid STORE zip with a bold, frozen header row', () => {
 const bytes = E.toXlsx(['Name', 'Count', 'Note'], [['web <1>', 3, 'a & b'], ['db', 2.5, null], ['x\u0001y', '42', ' padded']], 'Inst[ances]');
 assert.ok(bytes instanceof Uint8Array);
 assert.equal(new DataView(bytes.buffer).getUint32(0, true), 0x04034b50);
 const files = readZip(bytes);
 assert.deepEqual(Object.keys(files).sort(), ['[Content_Types].xml', '_rels/.rels', 'xl/_rels/workbook.xml.rels', 'xl/styles.xml', 'xl/workbook.xml', 'xl/worksheets/sheet1.xml']);
 const sheet = files['xl/worksheets/sheet1.xml'];
 assert.match(sheet, /<c r="A1" s="1" t="inlineStr"><is><t>Name<\/t><\/is><\/c>/);
 assert.match(sheet, /<c r="B2"><v>3<\/v><\/c>/);
 assert.match(sheet, /<c r="B3"><v>2.5<\/v><\/c>/);
 assert.match(sheet, /<c r="B4" t="inlineStr"><is><t>42<\/t><\/is><\/c>/, 'numeric strings stay text');
 assert.match(sheet, /web &lt;1&gt;/);
 assert.match(sheet, /a &amp; b/);
 assert.match(sheet, /<t>xy<\/t>/, 'invalid XML characters are stripped');
 assert.match(sheet, /<t xml:space="preserve"> padded<\/t>/);
 assert.match(sheet, /<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"\/>/);
 assert.match(sheet, /<col min="1" max="1" width="\d+" customWidth="1"\/>/);
 assert.match(files['xl/styles.xml'], /<font><b\/>/);
 assert.match(files['xl/workbook.xml'], /<sheet name="Inst ances " sheetId="1" r:id="rId1"\/>|<sheet name="Inst ances" sheetId="1" r:id="rId1"\/>/);
 assert.match(files['[Content_Types].xml'], /worksheets\/sheet1.xml/);
 const wide = readZip(E.toXlsx(['h'], [['x'.repeat(500)]]))['xl/worksheets/sheet1.xml'];
 assert.match(wide, /width="60"/);
});

// ---------- Secrets ----------

test('secrets: every kind is detected', () => {
 const kind = (key, value) => S.detect(key, value)?.kind ?? null;
 assert.equal(kind('AccessKeyId', 'AKIAIOSFODNN7EXAMPLE'), 'aws-access-key');
 assert.equal(kind('note', 'key ASIAIOSFODNN7EXAMPLE here'), 'aws-access-key');
 assert.equal(kind('SecretAccessKey', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'), 'aws-secret-key');
 assert.equal(kind('aws_secret_access_key', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'), 'aws-secret-key');
 assert.equal(kind('SessionToken', 'AQoDYXdzEJr1K1ExampleTokenValue1234567890'), 'session-token');
 assert.equal(kind('anything', 'FwoGZXIvYXdzEBEaDExampleExample'), 'session-token');
 assert.equal(kind('anything', 'IQoJb3JpZ2luX2VjEJr//////////wEaCXVzLWVhc3QtMSJH'), 'session-token');
 assert.equal(kind('conn', 'DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=abc123==;EndpointSuffix=core.windows.net'), 'azure-connection-string');
 assert.equal(kind('bus', 'Endpoint=sb://ns.servicebus.windows.net/;SharedAccessKeyName=Root;SharedAccessKey=abc='), 'azure-connection-string');
 assert.equal(kind('url', 'https://acct.blob.core.windows.net/c/b?sv=2022-11-02&se=2025-01-01T00%3A00%3A00Z&sp=r&sig=AbC%2Fdef%3D'), 'sas-token');
 assert.equal(kind('pem', '-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----'), 'private-key');
 assert.equal(kind('pem', '-----BEGIN PRIVATE KEY-----\nMIIE'), 'private-key');
 assert.equal(kind('id_token', 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl'), 'jwt');
 assert.equal(kind('Authorization', 'Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig_part-1'), 'jwt');
 for (const key of ['password', 'Password', 'passwd', 'pwd', 'secret', 'client_secret', 'clientSecret', 'api_key', 'apiKey', 'ApiKey', 'access_token', 'auth_token', 'token', 'private_key', 'connectionString', 'MasterUserPassword', 'DbPassword', 'ClientSecret', 'githubToken', 'DB_PASSWORD']) {
  assert.equal(kind(key, 'hunter2!'), 'password', key);
 }
 assert.deepEqual(S.detect('password', 'x'), {kind: 'password', label: 'Password or secret'});
});

test('secrets: metadata keys, references and non-strings are not flagged', () => {
 for (const key of ['PasswordLastUsed', 'TokenEndpoint', 'KeyName', 'KeyId', 'SecretArn', 'PasswordPolicy', 'SecretId', 'SecretName', 'PasswordResetRequired',
  'TokenExpirationTime', 'SecretVersionId', 'PasswordLength', 'TokenUrl', 'SecretCreatedDate', 'MaxPasswordAge', 'PasswordReusePreventionEnabled', 'SessionTokenExpiration', 'NextToken', 'ClientToken']) {
  assert.equal(S.detect(key, 'some-value-1234567890abcdef'), null, key);
 }
 assert.equal(S.detect('Password', '****'), null);
 assert.equal(S.detect('password', 'arn:aws:secretsmanager:us-east-1:123456789012:secret:db-AbC'), null);
 assert.equal(S.detect('password', '{{resolve:secretsmanager:db}}'), null);
 assert.equal(S.detect('password', ''), null);
 assert.equal(S.detect('password', true), null);
 assert.equal(S.detect('token', 12345), null);
 assert.equal(S.detect('Description', 'the password is stored elsewhere'), null);
 assert.equal(S.detect('ImageId', 'ami-0abcdef1234567890'), null);
});

test('secrets: custom rules, scan, mask and redact', () => {
 const rules = [{keyPattern: '^internal_', label: 'Internal'}, {valuePattern: '^ghp_[A-Za-z0-9]{10,}$', label: 'GitHub token'}, {keyPattern: '(', label: 'bad regex'}, {}];
 assert.deepEqual(S.detect('internal_code', 'x1', rules), {kind: 'custom', label: 'Internal'});
 assert.deepEqual(S.detect('value', 'ghp_abcdefghij12345', rules), {kind: 'custom', label: 'GitHub token'});
 assert.equal(S.detect('value', 'plain', rules), null);
 assert.equal(S.detect('internal_code', 'x1'), null);
 const doc = {Users: [{UserName: 'a', PasswordLastUsed: '2024', AccessKeys: [{AccessKeyId: 'AKIAIOSFODNN7EXAMPLE', Status: 'Active'}]}],
  Config: {db: {password: 'pw', host: 'h'}, internal_x: 'y', n: 5, flags: [true, null]}, list: ['ghp_abcdefghij12345']};
 assert.deepEqual(S.scan(doc, rules), [
  {path: ['Users', 0, 'AccessKeys', 0, 'AccessKeyId'], kind: 'aws-access-key', label: 'AWS access key ID'},
  {path: ['Config', 'db', 'password'], kind: 'password', label: 'Password or secret'},
  {path: ['Config', 'internal_x'], kind: 'custom', label: 'Internal'},
  {path: ['list', 0], kind: 'custom', label: 'GitHub token'}]);
 assert.equal(S.scan(doc, rules, 2).length, 2);
 assert.equal(S.mask('AKIAIOSFODNN7EXAMPLE'), '••••••MPLE');
 assert.equal(S.mask('short'), '••••••');
 assert.equal(S.mask('123456789012'), '••••••9012');
 const before = JSON.stringify(doc);
 const clean = S.redact(doc, rules);
 assert.equal(JSON.stringify(doc), before, 'original untouched');
 assert.equal(clean.Users[0].AccessKeys[0].AccessKeyId, '[REDACTED aws-access-key]');
 assert.equal(clean.Config.db.password, '[REDACTED password]');
 assert.equal(clean.Config.internal_x, '[REDACTED custom]');
 assert.equal(clean.Config.db.host, 'h');
 assert.equal(clean.Users[0].PasswordLastUsed, '2024');
 assert.deepEqual(clean.Config.flags, [true, null]);
 assert.notEqual(clean.Config, doc.Config);
});

// ---------- Diff ----------

function instance(id, type, owner, extra = {}) {
 return {
  InstanceId: id, InstanceType: type, LaunchTime: '2024-01-01T00:00:00Z', State: {Name: 'running'},
  Tags: [{Key: 'Name', Value: `web-${id}`}, {Key: 'Owner', Value: owner}],
  SecurityGroups: [{GroupId: 'sg-1', GroupName: 'default'}],
  BlockDeviceMappings: [{DeviceName: '/dev/xvda', Ebs: {VolumeId: `vol-${id}`, AttachTime: '2024-01-01T00:00:00Z', Status: 'attached'}}],
  ...extra
 };
}

test('diff: EC2 snapshots', () => {
 const before = [instance('i-1', 't3.micro', 'alice'), instance('i-2', 't3.small', 'bob', {Ports: [22, 80]}), instance('i-3', 't3.small', 'carol'), instance('i-5', 't3.small', 'x')];
 const after = [instance('i-1', 't3.large', 'dave', {LaunchTime: '2024-02-02T00:00:00Z'}), instance('i-2', 't3.small', 'bob', {Ports: [80, 443]}), instance('i-4', 't3.nano', 'eve'), instance('i-5', 't3.small', 'x')];
 after[1].SecurityGroups.push({GroupId: 'sg-2', GroupName: 'web'});
 after[1].SecurityGroups[0].GroupName = 'renamed';
 after[0].BlockDeviceMappings[0].Ebs.AttachTime = '2024-02-02T00:00:00Z';
 after[0].BlockDeviceMappings[0].Ebs.Status = 'detaching';
 after[3].LaunchTime = 'later';

 const result = D.diffRecords(before, after, {ignore: ['LaunchTime', '*.AttachTime']});
 assert.deepEqual(result.summary, {added: 1, removed: 1, changed: 2, unchanged: 1});
 assert.equal(result.idKey, 'InstanceId');
 assert.deepEqual(result.changes.map(change => [change.kind, change.id, change.name]), [['changed', 'i-1', 'web-i-1'], ['changed', 'i-2', 'web-i-2'], ['added', 'i-4', 'web-i-4'], ['removed', 'i-3', 'web-i-3']]);
 assert.deepEqual(result.changes[0].fields, [
  {path: 'InstanceType', before: 't3.micro', after: 't3.large'},
  {path: 'Tags.Owner', before: 'alice', after: 'dave'},
  {path: 'BlockDeviceMappings[0].Ebs.Status', before: 'attached', after: 'detaching'}]);
 assert.deepEqual(result.changes[1].fields, [
  {path: 'SecurityGroups[sg-1].GroupName', before: 'default', after: 'renamed'},
  {path: 'SecurityGroups[+]', before: undefined, after: 'sg-2'},
  {path: 'Ports[+]', before: undefined, after: 443},
  {path: 'Ports[-]', before: 22, after: undefined}]);
 const added = result.changes[2];
 assert.deepEqual(added.fields.slice(0, 2), [{path: 'InstanceId', before: undefined, after: 'i-4'}, {path: 'InstanceType', before: undefined, after: 't3.nano'}]);
 assert.ok(added.fields.some(field => field.path === 'State.Name'));
 assert.ok(!added.fields.some(field => field.path === 'LaunchTime'), 'ignored fields are left out');
 assert.ok(added.fields.length <= 12);
 assert.equal(result.changes[3].fields[0].before, 'i-3');

 const noisy = D.diffRecords(before, after);
 assert.equal(noisy.summary.changed, 3, 'without ignore the LaunchTime change counts');
 assert.ok(noisy.changes[0].fields.some(field => field.path === 'BlockDeviceMappings[0].Ebs.AttachTime'));
 const tagsIgnored = D.diffRecords(before, after, {ignore: ['Tags', 'InstanceType', 'LaunchTime', 'BlockDeviceMappings']});
 assert.equal(tagsIgnored.changes.find(change => change.id === 'i-1'), undefined);
 const glob = D.diffRecords(before, after, {ignore: ['BlockDeviceMappings[*].Ebs.*', 'LaunchTime']});
 assert.ok(!glob.changes[0].fields.some(field => field.path.startsWith('BlockDeviceMappings')));
 const deep = D.diffRecords(before, after, {ignore: ['**.Status', 'LaunchTime', '*.AttachTime']});
 assert.ok(!deep.changes[0].fields.some(field => field.path.endsWith('Status')));

 const markdown = D.toMarkdown(result, {beforeLabel: 'Monday', afterLabel: 'Tuesday'});
 assert.match(markdown, /# Diff: Monday → Tuesday/);
 assert.match(markdown, /\*\*Summary:\*\* 2 changed, 1 added, 1 removed, 1 unchanged/);
 assert.match(markdown, /## Changed: web-i-1 \(`i-1`\)/);
 assert.match(markdown, /\| Path \| Monday \| Tuesday \|/);
 assert.match(markdown, /\| `InstanceType` \| `"t3.micro"` \| `"t3.large"` \|/);
 assert.match(markdown, /\| `Tags.Owner` \| `"alice"` \| `"dave"` \|/);
 assert.match(markdown, /\| `SecurityGroups\[\+\]` \| — \| `"sg-2"` \|/);
 assert.match(markdown, /## Added: web-i-4/);
 assert.match(markdown, /## Removed: web-i-3/);
});

test('diff: ids, positions and generic documents', () => {
 assert.equal(D.idOf({InstanceId: 'i-1', Arn: 'x'}), 'i-1');
 assert.equal(D.idOf({Arn: 'arn:aws:iam::1:role/r', RoleId: 'AROA'}), 'arn:aws:iam::1:role/r');
 assert.equal(D.idOf({id: '/subscriptions/1/x'}), '/subscriptions/1/x');
 assert.equal(D.idOf({GroupId: 'sg-1', VpcId: 'vpc-1'}), 'sg-1');
 assert.equal(D.idOf({SubnetId: 'subnet-1', VpcId: 'vpc-1', AvailabilityZoneId: 'use1-az1'}), 'subnet-1');
 assert.equal(D.idOf({VpcId: 'vpc-1', OwnerId: '123'}), 'vpc-1');
 assert.equal(D.idOf({RouteTableId: 'rtb-1', VpcId: 'vpc-1'}), 'rtb-1');
 assert.equal(D.idOf({Name: 'my-bucket', CreationDate: '2024'}), 'my-bucket');
 assert.equal(D.idOf({metadata: {uid: 'u-1', name: 'pod'}}), 'u-1');
 assert.equal(D.idOf({metadata: {namespace: 'ns', name: 'pod'}}), 'ns/pod');
 assert.equal(D.idOf({address: 'aws_instance.web', type: 'aws_instance'}), 'aws_instance.web');
 assert.equal(D.idOf({name: 'thing'}), 'thing');
 assert.equal(D.idOf({FunctionName: 'fn', Runtime: 'nodejs'}), 'fn');
 assert.equal(D.idOf({Value: 1}), null);
 assert.equal(D.idOf('text'), null);

 const positional = D.diffRecords([{v: 1}, {v: 2}], [{v: 1}, {v: 3}, {v: 4}]);
 assert.deepEqual(positional.summary, {added: 1, removed: 0, changed: 1, unchanged: 1});
 assert.deepEqual(positional.changes.map(change => change.id), ['#1', '#2']);
 assert.deepEqual(positional.changes[0].fields, [{path: 'v', before: 2, after: 3}]);

 const pods = D.diffRecords([{metadata: {namespace: 'a', name: 'p'}, status: {phase: 'Running'}}], [{metadata: {namespace: 'a', name: 'p'}, status: {phase: 'Failed'}}]);
 assert.equal(pods.changes[0].name, 'p');
 assert.deepEqual(pods.changes[0].fields, [{path: 'status.phase', before: 'Running', after: 'Failed'}]);

 const values = D.diffValues({a: [1, 2, 3], b: {c: 1, d: 'x'}, e: 1, Tags: [{Key: 'k', Value: 'v'}]}, {a: [3, 2, 4], b: {c: 2, d: 'x'}, f: true, Tags: []}, {ignore: ['e']});
 assert.deepEqual(values.fields, [
  {path: 'a[+]', before: undefined, after: 4},
  {path: 'a[-]', before: 1, after: undefined},
  {path: 'b.c', before: 1, after: 2},
  {path: 'Tags.k', before: 'v', after: undefined},
  {path: 'f', before: undefined, after: true}]);
 assert.deepEqual(D.diffValues([1, {a: 1}], [1, {a: 2}]).fields, [{path: '[1].a', before: 1, after: 2}]);
 assert.deepEqual(D.diffValues('x', 'y').fields, [{path: '', before: 'x', after: 'y'}]);
 assert.deepEqual(D.diffValues({a: 1}, {a: 1}).fields, []);
 assert.match(D.toMarkdown(values), /5 fields changed[\s\S]*\| `b.c` \| `1` \| `2` \|/);
 assert.match(D.toMarkdown(D.diffValues(1, 1)), /No differences/);
 assert.match(D.toMarkdown(D.diffRecords([], [])), /0 changed, 0 added, 0 removed, 0 unchanged/);
});

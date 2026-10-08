"use strict";
// Compare two snapshots of cloud CLI output: resources matched by id (InstanceId, Arn,
// metadata.uid...), field-level changes with dotted paths, tag lists compared as maps,
// primitive lists as sets, and ignore patterns for noisy fields like LaunchTime.
(function (root) {
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const isObject = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const isScalarId = value => typeof value === "string" && value !== "" || typeof value === "number" && Number.isFinite(value);

  // Checked in order. VpcId and SubnetId come last: most resources only reference them.
  const ID_KEYS = ["InstanceId", "Arn", "ARN", "arn", "id", "Id", "ResourceId", "ResourceArn", "GroupId", "VolumeId", "ImageId",
    "NetworkInterfaceId", "DBInstanceIdentifier", "FunctionName", "BucketName"];
  // Keys ending in Id/Arn that name some other resource, so never identify a record.
  const REFERENCE_KEYS = new Set(["OwnerId", "AccountId", "AvailabilityZoneId", "VpcId", "SubnetId", "ImageId", "KernelId", "RamdiskId",
    "KmsKeyId", "KeyId", "KmsKeyArn", "DhcpOptionsId", "RequesterId", "ReservationId", "SnapshotId", "RoleArn", "OutpostArn",
    "TenantId", "SubscriptionId", "ProjectId", "ParentId", "SourceArn", "TargetArn", "PolicyArn"]);

  function idInfo(record) {
    if (!isObject(record)) return null;
    for (const key of ID_KEYS) {
      if (!own(record, key)) continue;
      const value = record[key];
      if (key === "id" ? typeof value === "string" && value !== "" : isScalarId(value)) return {key, id: String(value)};
    }
    // S3 list-buckets entries: {Name, CreationDate}.
    if (typeof record.Name === "string" && own(record, "CreationDate") && Object.keys(record).length <= 4) return {key: "Name", id: record.Name};
    const meta = record.metadata;
    if (isObject(meta)) {
      if (isScalarId(meta.uid)) return {key: "metadata.uid", id: String(meta.uid)};
      if (isScalarId(meta.name)) return {key: "metadata.name", id: meta.namespace ? `${meta.namespace}/${meta.name}` : String(meta.name)};
    }
    if (typeof record.address === "string" && record.address) return {key: "address", id: record.address};
    if (isScalarId(record.name)) return {key: "name", id: String(record.name)};
    if (isScalarId(record.Name)) return {key: "Name", id: String(record.Name)};
    for (const key of Object.keys(record)) {
      if (/(?:Id|Arn)$/.test(key) && !REFERENCE_KEYS.has(key) && typeof record[key] === "string" && record[key]) return {key, id: record[key]};
    }
    for (const key of ["SubnetId", "VpcId"]) if (typeof record[key] === "string" && record[key]) return {key, id: record[key]};
    return null;
  }

  const idOf = record => idInfo(record)?.id ?? null;

  function nameOf(record, id) {
    if (isObject(record)) {
      const tags = record.Tags || record.TagList || record.tags;
      if (Array.isArray(tags)) {
        const tag = tags.find(item => isObject(item) && (item.Key === "Name" || item.key === "Name"));
        const value = tag && (tag.Value ?? tag.value);
        if (typeof value === "string" && value) return value;
      }
      for (const key of ["name", "Name"]) if (typeof record[key] === "string" && record[key]) return record[key];
      if (isObject(record.metadata) && typeof record.metadata.name === "string") return record.metadata.name;
    }
    return id;
  }

  // ---- Ignore patterns ----

  const escapeRegex = text => text.replace(/[.+?^${}()|[\]\\]/g, "\\$&");

  function compileIgnore(patterns) {
    const tests = (patterns || []).map(pattern => String(pattern).trim()).filter(Boolean).map(pattern => {
      if (!pattern.includes("*")) {
        // A bare name matches any path whose last segment is that name.
        if (!pattern.includes(".")) return path => path === pattern || lastName(path) === pattern;
        return path => path === pattern;
      }
      let source = pattern;
      let prefix = "";
      if (source.startsWith("**.")) { prefix = "(?:.*\\.)?"; source = source.slice(3); } else if (source.startsWith("*.")) { prefix = "(?:.*\\.)?"; source = source.slice(2); }
      const body = source.split("**").map(part => part.split("*").map(escapeRegex).join("[^.]*")).join(".*");
      const re = new RegExp(`^${prefix}${body}$`);
      return path => re.test(path);
    });
    if (!tests.length) return () => false;
    // A pattern that matches a field also covers everything under it (Tags covers Tags.Owner).
    return path => prefixes(path).some(prefix => tests.some(test => test(prefix)));
  }

  // "A.B[0].C" → ["A", "A.B[0]", "A.B[0].C"] (and "A.B" for the unindexed list itself).
  function prefixes(path) {
    const out = [];
    let depth = 0;
    for (let i = 0; i < path.length; i++) {
      const char = path[i];
      if (char === "[") {
        if (depth === 0 && i > 0) out.push(path.slice(0, i));
        depth++;
      } else if (char === "]") depth--;
      else if (char === "." && depth === 0) out.push(path.slice(0, i));
    }
    out.push(path);
    return [...new Set(out)];
  }

  function lastName(path) {
    let depth = 0, start = 0;
    for (let i = 0; i < path.length; i++) {
      if (path[i] === "[") depth++;
      else if (path[i] === "]") depth--;
      else if (path[i] === "." && depth === 0) start = i + 1;
    }
    return path.slice(start).replace(/\[.*$/, "");
  }

  // ---- Deep comparison ----

  function equal(a, b) {
    if (a === b) return true;
    if (Array.isArray(a)) return Array.isArray(b) && a.length === b.length && a.every((item, i) => equal(item, b[i]));
    if (isObject(a) && isObject(b)) {
      const keys = Object.keys(a);
      return keys.length === Object.keys(b).length && keys.every(key => own(b, key) && equal(a[key], b[key]));
    }
    return false;
  }

  const join = (path, key) => path ? `${path}.${key}` : String(key);
  const isTag = item => isObject(item) && typeof item.Key === "string" && Object.keys(item).every(key => ["Key", "Value", "PropagateAtLaunch", "ResourceId", "ResourceType"].includes(key));
  const isTagList = list => Array.isArray(list) && list.length > 0 && list.every(isTag);
  const isPrimitive = value => value === null || typeof value !== "object";
  const elementKey = value => typeof value === "string" ? "s:" + value : "j:" + JSON.stringify(value);

  function compareValues(before, after, path, ignored, out) {
    if (path && ignored(path)) return;
    if (equal(before, after)) return;
    if (isObject(before) && isObject(after)) {
      const keys = Object.keys(before);
      for (const key of Object.keys(after)) if (!own(before, key)) keys.push(key);
      for (const key of keys) compareValues(own(before, key) ? before[key] : undefined, own(after, key) ? after[key] : undefined, join(path, key), ignored, out);
      return;
    }
    if (Array.isArray(before) && Array.isArray(after)) {
      if ((isTagList(before) || !before.length) && (isTagList(after) || !after.length)) {
        const map = list => Object.fromEntries(list.map(tag => [tag.Key, tag.Value]));
        compareValues(map(before), map(after), path, ignored, out);
        return;
      }
      if (before.every(isPrimitive) && after.every(isPrimitive)) {
        const had = new Set(before.map(elementKey)), has = new Set(after.map(elementKey));
        const added = path + "[+]", removed = path + "[-]";
        for (const item of after) if (!had.has(elementKey(item)) && !ignored(added)) out.push({path: added, before: undefined, after: item});
        for (const item of before) if (!has.has(elementKey(item)) && !ignored(removed)) out.push({path: removed, before: item, after: undefined});
        return;
      }
      compareLists(before, after, path, ignored, out);
      return;
    }
    out.push({path: path || "", before, after});
  }

  // Lists of objects: match by element id when every element has a unique one, else by index.
  function compareLists(before, after, path, ignored, out) {
    const ids = list => {
      const values = list.map(item => idOf(item));
      return values.every(id => id !== null) && new Set(values).size === values.length ? values : null;
    };
    const beforeIds = ids(before), afterIds = ids(after);
    const describe = (item, id) => id ?? JSON.stringify(item);
    if (beforeIds && afterIds) {
      const index = new Map(beforeIds.map((id, i) => [id, i]));
      const seen = new Set();
      afterIds.forEach((id, i) => {
        if (index.has(id)) {
          seen.add(id);
          compareValues(before[index.get(id)], after[i], `${path}[${id}]`, ignored, out);
        } else if (!ignored(path + "[+]")) out.push({path: path + "[+]", before: undefined, after: describe(after[i], id)});
      });
      beforeIds.forEach((id, i) => {
        if (!seen.has(id) && !ignored(path + "[-]")) out.push({path: path + "[-]", before: describe(before[i], id), after: undefined});
      });
      return;
    }
    const length = Math.max(before.length, after.length);
    for (let i = 0; i < length; i++) {
      if (i >= before.length) {
        if (!ignored(path + "[+]")) out.push({path: path + "[+]", before: undefined, after: isPrimitive(after[i]) ? after[i] : describe(after[i], idOf(after[i]))});
      } else if (i >= after.length) {
        if (!ignored(path + "[-]")) out.push({path: path + "[-]", before: isPrimitive(before[i]) ? before[i] : describe(before[i], idOf(before[i])), after: undefined});
      } else compareValues(before[i], after[i], `${path}[${i}]`, ignored, out);
    }
  }

  // The fields shown for an added or removed record: top-level scalars, then scalars one level down.
  function summaryFields(record, side, ignored) {
    const fields = [];
    const add = (path, value) => {
      if (fields.length >= 12 || ignored(path)) return;
      fields.push(side === "after" ? {path, before: undefined, after: value} : {path, before: value, after: undefined});
    };
    if (!isObject(record)) {
      add("", record);
      return fields;
    }
    for (const key of Object.keys(record)) if (isPrimitive(record[key])) add(key, record[key]);
    for (const key of Object.keys(record)) {
      if (!isObject(record[key])) continue;
      for (const inner of Object.keys(record[key])) if (isPrimitive(record[key][inner])) add(`${key}.${inner}`, record[key][inner]);
    }
    return fields;
  }

  function keyed(list) {
    const map = new Map();
    const counts = new Map();
    list.forEach((record, index) => {
      const info = idInfo(record);
      let id = info ? info.id : `#${index}`;
      if (map.has(id)) {
        const n = (counts.get(id) || 1) + 1;
        counts.set(id, n);
        id = `${id}#${n}`;
      }
      map.set(id, {record, key: info?.key ?? null});
    });
    return map;
  }

  const KIND_ORDER = {changed: 0, added: 1, removed: 2};

  function diffRecords(before, after, options = {}) {
    const ignored = compileIgnore(options.ignore);
    const left = keyed(Array.isArray(before) ? before : before === undefined || before === null ? [] : [before]);
    const right = keyed(Array.isArray(after) ? after : after === undefined || after === null ? [] : [after]);
    const changes = [];
    const summary = {added: 0, removed: 0, changed: 0, unchanged: 0};
    const keyCounts = new Map();
    for (const {key} of [...left.values(), ...right.values()]) if (key) keyCounts.set(key, (keyCounts.get(key) || 0) + 1);
    const idKey = [...keyCounts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? null;

    for (const [id, {record}] of right) {
      if (left.has(id)) {
        const fields = [];
        compareValues(left.get(id).record, record, "", ignored, fields);
        if (fields.length) {
          summary.changed++;
          changes.push({kind: "changed", id, name: nameOf(record, id), before: left.get(id).record, after: record, fields});
        } else summary.unchanged++;
      } else {
        summary.added++;
        changes.push({kind: "added", id, name: nameOf(record, id), before: undefined, after: record, fields: summaryFields(record, "after", ignored)});
      }
    }
    for (const [id, {record}] of left) {
      if (right.has(id)) continue;
      summary.removed++;
      changes.push({kind: "removed", id, name: nameOf(record, id), before: record, after: undefined, fields: summaryFields(record, "before", ignored)});
    }
    changes.sort((a, b) => KIND_ORDER[a.kind] - KIND_ORDER[b.kind] || String(a.name).localeCompare(String(b.name), undefined, {numeric: true}));
    return {changes, summary, idKey};
  }

  function diffValues(before, after, options = {}) {
    const fields = [];
    compareValues(before, after, "", compileIgnore(options.ignore), fields);
    return {fields};
  }

  // ---- Markdown report ----

  function cell(value) {
    if (value === undefined) return "—";
    let text = JSON.stringify(value);
    if (text === undefined) text = String(value);
    if (text.length > 200) text = text.slice(0, 199) + "…";
    return "`" + text.replace(/\|/g, "\\|").replace(/`/g, "'").replace(/\r?\n/g, " ") + "`";
  }

  function fieldTable(fields, beforeLabel, afterLabel) {
    const lines = [`| Path | ${beforeLabel} | ${afterLabel} |`, "| --- | --- | --- |"];
    for (const field of fields) lines.push(`| \`${(field.path || "(root)").replace(/\|/g, "\\|")}\` | ${cell(field.before)} | ${cell(field.after)} |`);
    return lines.join("\n");
  }

  function toMarkdown(result, options = {}) {
    const beforeLabel = options.beforeLabel || "Before", afterLabel = options.afterLabel || "After";
    const lines = [`# Diff: ${beforeLabel} → ${afterLabel}`, ""];
    if (!result.changes) {
      if (!result.fields || !result.fields.length) return lines.concat("No differences.", "").join("\n");
      lines.push(`${result.fields.length} field${result.fields.length === 1 ? "" : "s"} changed.`, "", fieldTable(result.fields, beforeLabel, afterLabel), "");
      return lines.join("\n");
    }
    const {summary} = result;
    lines.push(`**Summary:** ${summary.changed} changed, ${summary.added} added, ${summary.removed} removed, ${summary.unchanged} unchanged`, "");
    if (!result.changes.length) lines.push("No differences.", "");
    const titles = {changed: "Changed", added: "Added", removed: "Removed"};
    for (const change of result.changes) {
      const name = change.name && change.name !== change.id ? `${change.name} (\`${change.id}\`)` : `\`${change.id}\``;
      lines.push(`## ${titles[change.kind]}: ${name}`, "");
      if (change.fields.length) lines.push(fieldTable(change.fields, beforeLabel, afterLabel), "");
    }
    return lines.join("\n");
  }

  const api = {idOf, diffRecords, diffValues, toMarkdown};
  if (typeof module !== "undefined") module.exports = api;
  else root.JVDiff = api;
})(typeof self !== "undefined" ? self : this);

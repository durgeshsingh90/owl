"use strict";
// Cloud-aware presentation: where the output came from (AWS, Azure, GCP, kubectl,
// Terraform), the resource list inside its envelope, display names, tags, status
// colours, key fields, default table columns and console links.
(function (root) {
  const isObject = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const objects = list => Array.isArray(list) && list.length > 0 && list.filter(isObject).length >= list.length / 2;
  // AWS paging and metadata keys that sit beside the resource list.
  const AWS_EXTRA = new Set(["NextToken", "nextToken", "Marker", "NextMarker", "IsTruncated", "ResponseMetadata", "MaxResults", "MaxItems", "ContinuationToken", "NextContinuationToken", "KeyCount", "OwnerId"]);

  function firstObject(value) {
    if (Array.isArray(value)) return value.find(isObject);
    return isObject(value) ? value : undefined;
  }

  function detectSource(value) {
    const top = isObject(value) ? value : undefined;
    const sample = firstObject(top?.value) || firstObject(top?.items) || firstObject(value) || top;
    if (!sample) return "unknown";
    if (top && ("terraform_version" in top || "format_version" in top && ("values" in top || "planned_values" in top || "resource_changes" in top))) return "terraform";
    if (top?.apiVersion && top?.kind || sample.apiVersion && sample.kind && sample.metadata) return "kubectl";
    if (typeof sample.id === "string" && sample.id.startsWith("/subscriptions/") || typeof top?.id === "string" && top.id.startsWith("/subscriptions/")) return "azure";
    if (typeof sample.selfLink === "string" && sample.selfLink.includes("googleapis.com") || /^compute#/.test(sample.kind || "")) return "gcp";
    const text = JSON.stringify(sample).slice(0, 20000);
    if (/"arn:aws[\w-]*:/.test(text)) return "aws";
    const keys = Object.keys(top || sample);
    if (keys.length && keys.filter(key => /^[A-Z]/.test(key)).length >= keys.length * 0.8) return "aws";
    return "unknown";
  }

  // The resource list inside a CLI envelope: {path, rows: [{value, path}], label}, or null.
  function unwrap(value, source = detectSource(value)) {
    const rowsOf = (list, base) => list.map((item, index) => ({value: item, path: [...base, index]}));
    if (isObject(value) && Array.isArray(value.Reservations) && value.Reservations.some(reservation => Array.isArray(reservation?.Instances))) {
      const rows = [];
      value.Reservations.forEach((reservation, r) => (reservation?.Instances || []).forEach((instance, i) => rows.push({value: instance, path: ["Reservations", r, "Instances", i]})));
      return {path: ["Reservations"], rows, label: "Instances", kind: "Reservations[].Instances[]"};
    }
    if (isObject(value) && Array.isArray(value.value) && (source === "azure" || objects(value.value))) {
      return {path: ["value"], rows: rowsOf(value.value, ["value"]), label: "value", kind: "value[]"};
    }
    if (isObject(value) && Array.isArray(value.items) && (source === "kubectl" || objects(value.items))) {
      return {path: ["items"], rows: rowsOf(value.items, ["items"]), label: "items", kind: "items[]"};
    }
    if (source === "terraform" && isObject(value)) {
      const module = value.values?.root_module || value.planned_values?.root_module;
      if (module) {
        const rows = [];
        const walk = (current, base) => {
          (current.resources || []).forEach((resource, index) => rows.push({value: resource, path: [...base, "resources", index]}));
          (current.child_modules || []).forEach((child, index) => walk(child, [...base, "child_modules", index]));
        };
        const base = value.values ? ["values", "root_module"] : ["planned_values", "root_module"];
        walk(module, base);
        if (rows.length) return {path: base, rows, label: "resources", kind: "terraform resources"};
      }
    }
    if (Array.isArray(value) && objects(value)) return {path: [], rows: rowsOf(value, []), label: "items", kind: "[]"};
    if (isObject(value)) {
      // {"Vpcs": [...], "NextToken": "..."}: the one list of objects beside paging keys.
      const lists = Object.keys(value).filter(key => objects(value[key]));
      const others = Object.keys(value).filter(key => !lists.includes(key) && !AWS_EXTRA.has(key));
      if (lists.length === 1 && others.length === 0) {
        return {path: [lists[0]], rows: rowsOf(value[lists[0]], [lists[0]]), label: lists[0], kind: `${lists[0]}[]`};
      }
    }
    return null;
  }

  // Tags as [{key, value}]: AWS [{Key, Value}], Azure {k: v}, kubectl metadata.labels.
  function tags(record) {
    if (!isObject(record)) return [];
    const list = record.Tags || record.TagSet || record.tags || record.metadata?.labels || record.labels;
    if (Array.isArray(list)) {
      return list.filter(tag => isObject(tag) && ("Key" in tag || "key" in tag))
        .map(tag => ({key: String(tag.Key ?? tag.key), value: tag.Value ?? tag.value ?? ""}));
    }
    if (isObject(list)) return Object.entries(list).filter(([, value]) => !isObject(value)).map(([key, value]) => ({key, value: value ?? ""}));
    return [];
  }

  // Is this array an AWS-style tag list ([{Key, Value}])?
  const isTagList = value => Array.isArray(value) && value.length > 0 && value.every(tag => isObject(tag) && "Key" in tag && "Value" in tag && Object.keys(tag).length <= 3);

  const ID_KEYS = ["InstanceId", "VpcId", "SubnetId", "GroupId", "VolumeId", "ImageId", "NetworkInterfaceId", "DBInstanceIdentifier", "FunctionName", "BucketName", "Arn", "ARN", "arn", "id", "Id", "uid"];
  // Name tag, then name, then the resource ID.
  function displayName(record) {
    if (!isObject(record)) return undefined;
    const name = tags(record).find(tag => tag.key === "Name" || tag.key === "name");
    if (name && name.value !== "") return String(name.value);
    for (const key of ["Name", "name", "displayName", "DisplayName"]) if (typeof record[key] === "string" && record[key]) return record[key];
    if (typeof record.metadata?.name === "string") return record.metadata.name;
    for (const key of ID_KEYS) if (record[key] !== undefined && !isObject(record[key])) return String(record[key]);
    const anyId = Object.keys(record).find(key => /(Id|Arn|Name)$/.test(key) && typeof record[key] === "string");
    return anyId ? record[anyId] : undefined;
  }

  const KEY_FIELD = /^(id|Id|uid|arn|Arn|ARN|name|Name|displayName|region|Region|location|Location|zone|AvailabilityZone|state|State|status|Status|phase|provisioningState|[A-Za-z]+Id|[A-Za-z]+Arn|[A-Za-z]+State|[A-Za-z]+Status|CreateTime|CreationTime|CreationDate|CreatedTime|CreatedDate|LaunchTime|createdAt|created|creationTimestamp|timeCreated|createdTime)$/;
  const isKeyField = key => typeof key === "string" && KEY_FIELD.test(key);
  const STATUS_KEY = /^(state|State|status|Status|phase|Phase|provisioningState|powerState|health|Health|[A-Za-z]*State|[A-Za-z]*Status|Name)$/;
  const GOOD = /^(running|succeeded|success|successful|available|active|ready|healthy|ok|in-use|in_use|enabled|completed|complete|bound|true|online|attached|associated|issued|create_complete|update_complete|deployed|passing|vm running)$/i;
  const WARN = /^(stopped|stopping|pending|creating|updating|deleting|starting|rebooting|modifying|deallocated|deallocating|provisioning|in_progress|in-progress|warning|degraded|impaired|inactive|shutting-down|unknown|detaching|attaching|vm deallocated|vm stopped)$/i;
  const BAD = /^(terminated|failed|failure|error|errored|deleted|unhealthy|disabled|critical|rejected|crashloopbackoff|create_failed|rollback_complete|lost|evicted|offline)$/i;
  // good, warn or bad for a status-like key's value; null when it is not a status.
  function statusTone(key, value) {
    if (typeof value !== "string" || value.length > 40) return null;
    if (key !== undefined && !STATUS_KEY.test(String(key))) return null;
    if (GOOD.test(value)) return "good";
    if (WARN.test(value)) return "warn";
    if (BAD.test(value)) return "bad";
    return null;
  }

  // Built-in column sets for common commands; other lists get their key fields first.
  const TEMPLATES = [
    {name: "EC2 instances", test: record => "InstanceId" in record && "InstanceType" in record,
      columns: [["@name", "Name"], "InstanceId", "State.Name", "InstanceType", "Placement.AvailabilityZone", "PrivateIpAddress", "PublicIpAddress", "LaunchTime"]},
    {name: "Azure VMs", test: record => record.hardwareProfile?.vmSize !== undefined || /virtualMachines$/i.test(record.type || ""),
      columns: ["name", "resourceGroup", "location", "hardwareProfile.vmSize", "provisioningState", "powerState"]},
    {name: "Security groups", test: record => "GroupId" in record && "IpPermissions" in record,
      columns: ["GroupName", "GroupId", "VpcId", "Description"]},
    {name: "VPCs", test: record => "VpcId" in record && "CidrBlock" in record && !("SubnetId" in record),
      columns: [["@name", "Name"], "VpcId", "CidrBlock", "State", "IsDefault", "OwnerId"]},
    {name: "Subnets", test: record => "SubnetId" in record,
      columns: [["@name", "Name"], "SubnetId", "VpcId", "CidrBlock", "AvailabilityZone", "AvailableIpAddressCount", "State"]},
    {name: "Volumes", test: record => "VolumeId" in record && "Size" in record,
      columns: [["@name", "Name"], "VolumeId", "State", "Size", "VolumeType", "AvailabilityZone", "CreateTime"]},
    {name: "Kubernetes objects", test: record => record.metadata && record.kind,
      columns: ["metadata.name", "metadata.namespace", "kind", "status.phase", "metadata.creationTimestamp"]},
    {name: "Azure resources", test: record => typeof record.id === "string" && record.id.startsWith("/subscriptions/"),
      columns: ["name", "type", "resourceGroup", "location", "provisioningState"]},
    {name: "Terraform resources", test: record => "address" in record && "type" in record && "values" in record,
      columns: ["address", "type", "name", "provider_name", "mode"]},
    {name: "CloudTrail events", test: record => "eventName" in record && "eventSource" in record,
      columns: ["eventTime", "eventName", "eventSource", "userIdentity.arn", "sourceIPAddress", "awsRegion", "errorCode"]},
    {name: "CloudTrail lookup", test: record => "EventName" in record && "EventId" in record,
      columns: ["EventTime", "EventName", "Username", "EventSource", "EventId"]},
    {name: "CloudWatch log events", test: record => "message" in record && ("timestamp" in record || "ingestionTime" in record),
      columns: ["timestamp", "logStreamName", "message"]},
    {name: "Lambda functions", test: record => "FunctionName" in record && "Runtime" in record,
      columns: ["FunctionName", "Runtime", "Handler", "MemorySize", "Timeout", "LastModified"]},
    {name: "gcloud instances", test: record => typeof record.selfLink === "string" && "machineType" in record,
      columns: ["name", "zone", "machineType", "status", "creationTimestamp"]},
  ];
  function template(records) {
    const sample = records.find(isObject);
    return sample ? TEMPLATES.find(item => item.test(sample)) || null : null;
  }

  // Console links for ARNs and Azure resource IDs.
  function consoleLink(value) {
    if (typeof value !== "string") return null;
    if (/^arn:aws[\w-]*:/.test(value)) return {label: "Open in AWS Console", url: `https://console.aws.amazon.com/go/view?arn=${encodeURIComponent(value)}`};
    if (/^\/subscriptions\/[0-9a-f-]{36}\//i.test(value)) return {label: "Open in Azure Portal", url: `https://portal.azure.com/#@/resource${value}`};
    return null;
  }

  const api = {detectSource, unwrap, tags, isTagList, displayName, isKeyField, statusTone, template, consoleLink, TEMPLATES};
  if (typeof module !== "undefined") module.exports = api;
  else root.JVCloud = api;
})(typeof self !== "undefined" ? self : this);

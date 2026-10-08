"use strict";
// Secret detection for CLI output: access keys, secret keys, tokens, connection strings,
// private keys and password-like fields. Used to warn, mask and redact before sharing.
(function (root) {
  const isContainer = value => value !== null && typeof value === "object";

  const LABELS = {
    "aws-access-key": "AWS access key ID",
    "aws-secret-key": "AWS secret access key",
    "session-token": "Session token",
    "azure-connection-string": "Azure connection string",
    "sas-token": "SAS token",
    "private-key": "Private key",
    "jwt": "JSON Web Token",
    "password": "Password or secret",
    "custom": "Custom rule"
  };

  const PRIVATE_KEY = /-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----/;
  const ACCESS_KEY = /(?:^|[^A-Z0-9])(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}(?![A-Z0-9])/;
  const JWT = /(?:^|[^A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*|^eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]+$/;
  const AZURE = /AccountKey=|SharedAccessKey=|DefaultEndpointsProtocol=/i;
  const SAS_SIG = /(?:^|[?&;\s])sig=[^&\s]+/i;
  const SAS_PARTS = /(?:^|[?&;\s])(?:sv|se)=/i;
  const SESSION_VALUE = /^(?:FwoGZXIvYXdzE|IQoJb3JpZ2lu)/;

  const SECRET_KEY_NAME = /secret.?access.?key|aws_secret/i;
  const SESSION_KEY_NAME = /session.?token|security.?token/i;
  const PASSWORD_KEY_NAME = /^(?:password|passwd|pwd|pass|secret|client_?secret|api_?key|apikey|access_?token|auth_?token|token|private_?key|connection_?string)$/i;
  const PASSWORD_SUFFIX = /(?:password|passwd|secret|token)$/i;
  // Opaque tokens that are not credentials: paging and idempotency tokens.
  const NOT_SECRET_TOKEN = /^(?:next|continuation|next_?continuation|pagination|page|starting|start|client|idempotency|client_?request|marker|sync|change|query_?execution)_?token$/i;
  // Field names that describe a secret rather than hold one (SecretArn, PasswordLastUsed, TokenEndpoint...).
  const METADATA_WORDS = new Set(["arn", "arns", "id", "ids", "name", "names", "date", "time", "timestamp", "used", "policy", "policies",
    "endpoint", "endpoints", "url", "urls", "uri", "length", "enabled", "required", "age", "type", "types", "status", "count", "version",
    "versions", "expiration", "expiry", "expires", "format", "hint", "ref", "reference", "path", "location", "mode", "size", "at", "on",
    "created", "updated", "changed", "rotated", "rotation", "provider", "field", "label", "description", "set", "list", "region", "algorithm", "kind"]);

  function words(key) {
    return key.replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2").split(/[\s_.\-:/]+/).filter(Boolean).map(word => word.toLowerCase());
  }

  function isMetadataKey(key) {
    const parts = words(key);
    return parts.length > 1 && METADATA_WORDS.has(parts[parts.length - 1]);
  }

  // Values that point at a secret or are already masked, rather than being one.
  function isReference(value) {
    const text = value.trim();
    if (!text) return true;
    if (/^arn:aws[\w-]*:/.test(text)) return true;
    if (/^[*•x#]{3,}$/i.test(text) || text.includes("****") || /^<[^<>]*>$/.test(text) || /^\[?redacted/i.test(text)) return true;
    if (/^\$\{[^}]+\}$|^\{\{[^}]+\}\}$|^\$\([^)]+\)$|^%[A-Z_]+%$/i.test(text)) return true;
    if (/^@Microsoft\.KeyVault\(/i.test(text) || /^https:\/\/[^/\s]+\.vault\.azure\.net\//i.test(text)) return true;
    if (/^(?:true|false|null|none|n\/a|undefined|changeme)$/i.test(text)) return true;
    if (/^(?:projects\/[^/]+\/secrets\/|secretsmanager:|ssm:|vault:)/i.test(text)) return true;
    return false;
  }

  const ruleCache = new WeakMap();
  function compileRule(rule) {
    if (!rule || typeof rule !== "object") return null;
    let compiled = ruleCache.get(rule);
    if (compiled !== undefined) return compiled;
    const make = pattern => {
      if (pattern === undefined || pattern === null || pattern === "") return null;
      try { return new RegExp(pattern, "i"); } catch { return false; }
    };
    const key = make(rule.keyPattern), value = make(rule.valuePattern);
    compiled = key === false || value === false || (!key && !value) ? null : {key, value, label: rule.label || LABELS.custom};
    ruleCache.set(rule, compiled);
    return compiled;
  }

  const result = (kind, label = LABELS[kind]) => ({kind, label});

  // What kind of secret `value` (found under `key`) is, or null.
  function detect(key, value, rules = []) {
    if (typeof value !== "string" || value === "") return null;
    const name = typeof key === "string" ? key : "";
    if (PRIVATE_KEY.test(value)) return result("private-key");
    if (ACCESS_KEY.test(value)) return result("aws-access-key");
    if (SESSION_VALUE.test(value)) return result("session-token");
    if (AZURE.test(value)) return result("azure-connection-string");
    if (SAS_SIG.test(value) && SAS_PARTS.test(value)) return result("sas-token");
    if (JWT.test(value)) return result("jwt");
    if (name && !isMetadataKey(name) && !isReference(value)) {
      if (SECRET_KEY_NAME.test(name) && value.length >= 16) return result("aws-secret-key");
      if (SESSION_KEY_NAME.test(name) && value.length >= 16) return result("session-token");
      if ((PASSWORD_KEY_NAME.test(name) || PASSWORD_SUFFIX.test(name)) && !NOT_SECRET_TOKEN.test(name)) return result("password");
    }
    for (const rule of rules || []) {
      const compiled = compileRule(rule);
      if (!compiled) continue;
      if (compiled.key && !compiled.key.test(name)) continue;
      if (compiled.value && !compiled.value.test(value)) continue;
      return result("custom", compiled.label);
    }
    return null;
  }

  // Every secret under `root` as {path, kind, label}; paths are key/index arrays.
  function scan(root, rules = [], limit = 10000) {
    const found = [];
    const stack = [[root, [], undefined]];
    while (stack.length && found.length < limit) {
      const [value, path, key] = stack.pop();
      if (typeof value === "string") {
        const hit = detect(key, value, rules);
        if (hit) found.push({path, kind: hit.kind, label: hit.label});
      } else if (Array.isArray(value)) {
        for (let i = value.length - 1; i >= 0; i--) stack.push([value[i], path.concat(i), key]);
      } else if (isContainer(value)) {
        const keys = Object.keys(value);
        for (let i = keys.length - 1; i >= 0; i--) stack.push([value[keys[i]], path.concat(keys[i]), keys[i]]);
      }
    }
    return found;
  }

  function mask(text) {
    const value = String(text ?? "");
    return value.length >= 12 ? "••••••" + value.slice(-4) : "••••••";
  }

  // A deep copy with every detected secret replaced by "[REDACTED <kind>]".
  function redact(root, rules = []) {
    const copy = (value, key) => {
      if (typeof value === "string") {
        const hit = detect(key, value, rules);
        return hit ? `[REDACTED ${hit.kind}]` : value;
      }
      // Array items inherit the key of the array (Passwords: ["..."]).
      if (Array.isArray(value)) return value.map(item => copy(item, key));
      if (isContainer(value)) {
        const out = {};
        for (const name of Object.keys(value)) Object.defineProperty(out, name, {value: copy(value[name], name), enumerable: true, writable: true, configurable: true});
        return out;
      }
      return value;
    };
    return copy(root, undefined);
  }

  const api = {detect, scan, mask, redact, labels: LABELS};
  if (typeof module !== "undefined") module.exports = api;
  else root.JVSecrets = api;
})(typeof self !== "undefined" ? self : this);

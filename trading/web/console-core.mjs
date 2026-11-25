// Pure request-building helpers. No network, credentials, or trading side effects.
const FORBIDDEN_HEADERS = new Set(["cookie", "authorization", "proxy-authorization", "x-api-key", "x-csrf-token", "x-trading-environment", "host", "origin", "referer", "content-length", "connection"]);
const SENSITIVE = /secret|password|token|api[-_]?key|app[-_]?key|authorization|cookie|csrf|x-live-confirmation/i;

export function sanitize(value) {
  if (Array.isArray(value)) return value.map(sanitize);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, SENSITIVE.test(key) ? "[REDACTED]" : sanitize(item)]));
  return value;
}

export function parseHeaders(text) {
  const value = JSON.parse(text || "{}");
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Headers must be a JSON object.");
  const result = Object.create(null), seen = new Set();
  for (const [name, raw] of Object.entries(value)) {
    const lower = name.toLowerCase();
    if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(name) || seen.has(lower)) throw new Error(`Invalid or duplicate header: ${name}`);
    if (FORBIDDEN_HEADERS.has(lower) || lower.startsWith("sec-") || lower.startsWith("proxy-")) throw new Error(`${name} is managed by the local session/request environment. Do not paste .env secrets here.`);
    if (!["string", "number", "boolean"].includes(typeof raw) || /[\r\n]/.test(String(raw))) throw new Error(`Invalid value for header ${name}`);
    result[name] = String(raw); seen.add(lower);
  }
  return result;
}

export function buildRequest({origin, method, path, pathParams = {}, query = [], headers = {}, body = "", environment, localAuth = true}) {
  method = method.toUpperCase();
  if (!["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].includes(method)) throw new Error("Unsupported request method.");
  if (!["sandbox", "production"].includes(environment)) throw new Error("Choose an explicit request environment.");
  path = path.trim();
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("\\")) throw new Error("Use a local API path such as /v1/system, not an external URL.");
  path = path.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (_, name) => {
    const value = String(pathParams[name] || "").trim();
    if (!value || /[{}]/.test(value)) throw new Error(`Fill path parameter ${name}.`);
    return encodeURIComponent(value);
  });
  if (/[{}]/.test(path)) throw new Error("Unresolved path parameter.");
  const url = new URL(path, origin);
  if (url.origin !== origin || url.hash || !(url.pathname.startsWith("/v1/") || ["/v1", "/health", "/openapi.json"].includes(url.pathname))) throw new Error("This console only executes this project's /v1 API, /health, and /openapi.json.");
  for (const item of query) if (item.enabled !== false && String(item.key || "").trim()) url.searchParams.append(String(item.key).trim(), String(item.value ?? ""));
  const custom = parseHeaders(JSON.stringify(headers));
  const hasBody = !["GET", "HEAD"].includes(method) && body.trim() !== "";
  const parsedBody = hasBody ? JSON.parse(body) : null;
  const actualHeaders = {"X-Trading-Environment": environment, ...custom};
  if (hasBody && !Object.keys(custom).some(key => key.toLowerCase() === "content-type")) actualHeaders["Content-Type"] = "application/json";
  return {method, url: url.toString(), path: url.pathname, environment, localAuth, headers: actualHeaders,
          body: hasBody ? body : null, parsedBody};
}

export function previewRequest(request) {
  return {method: request.method, url: safeUrl(request.url), environment: request.environment,
          authentication: request.localAuth ? "Automatic HttpOnly local session; .env keys stay on server" : "Disabled for this request",
          headers: {...sanitize(request.headers), ...(request.localAuth ? {"X-CSRF-Token": "[MANAGED LOCAL SESSION]", "Cookie": "[HTTPONLY; BROWSER MANAGED]"} : {})},
          body: sanitize(request.parsedBody)};
}

export function safeUrl(value) {
  const url = new URL(value);
  for (const key of [...url.searchParams.keys()]) if (SENSITIVE.test(key)) url.searchParams.set(key, "[REDACTED]");
  return url.toString();
}

export function safeBuilder(value) {
  const url = new URL(value.path, value.origin);
  for (const key of [...url.searchParams.keys()]) if (SENSITIVE.test(key)) url.searchParams.set(key, "[REDACTED]");
  const basePath = value.path.split("?")[0];
  return {...value, path: basePath + (url.search ? url.search : ""), headers: sanitize(value.headers),
    query: value.query.map(item => ({...item, value: SENSITIVE.test(item.key) ? "[REDACTED]" : item.value})),
    body: ["GET", "HEAD"].includes(value.method) || !value.body.trim() ? "" : JSON.stringify(sanitize(JSON.parse(value.body)), null, 2)};
}

export function classifyRequest(request, activeEnvironment) {
  const {method, path, parsedBody} = request;
  const broker = method === "POST" && (/^\/v1\/orders\/[^/]+\/(submit|cancel)$/.test(path) || /^\/v1\/strategies\/[^/]+\/tick$/.test(path)) || method === "PATCH" && /^\/v1\/orders\/[^/]+$/.test(path);
  const arm = method === "POST" && (path === "/v1/live/arm" || /^\/v1\/strategies\/[^/]+\/arm$/.test(path));
  const switchingLive = method === "PUT" && path === "/v1/environment" && parsedBody?.environment === "production";
  const globalHalt = method === "PUT" && path === "/v1/risk/kill-switch" && parsedBody?.enabled === true;
  const live = request.environment === "production" || activeEnvironment === "production";
  if (globalHalt) return {review: false, live: false, label: "Global halt — no automatic cancellation", phrase: ""};
  if (broker && live) return {review: true, live: true, label: "May send a REAL-MONEY broker mutation", phrase: "EXECUTE LIVE REQUEST"};
  if (arm || switchingLive) return {review: true, live: true, label: "Changes live permissions/account context; does not itself place an order", phrase: "EXECUTE LIVE REQUEST"};
  if (broker) return {review: true, live: false, label: "May submit/modify/cancel a sandbox broker order", phrase: ""};
  return {review: false, live, label: ["GET", "HEAD", "OPTIONS"].includes(method) ? "Read request" : "Local/control/read-only broker operation — inspect body before sending", phrase: ""};
}

export function exampleValue(schema, components = {}, depth = 0, key = "") {
  if (depth > 6) return null;
  if (schema.$ref) return exampleValue(components[schema.$ref.split("/").at(-1)] || {}, components, depth + 1, key);
  if (key === "confirmation") return ""; // Never prefill live arming/switch acknowledgements.
  if (key === "symbol") return "AAPL";
  if (key === "side") return "BUY";
  if (key === "quantity") return 1;
  if (key === "limit_price") return "REVIEW_PRICE";
  if (key === "reason") return "Manual API console action";
  if (key === "enabled") return true; // Safe halt, never auto-enable trading.
  if (schema.default !== undefined) return schema.default;
  if (schema.const !== undefined) return schema.const;
  if (schema.enum) return schema.enum[0];
  if (schema.anyOf) return exampleValue(schema.anyOf.find(item => item.type !== "null") || {}, components, depth + 1, key);
  if (schema.type === "object" || schema.properties) return Object.fromEntries(Object.entries(schema.properties || {}).filter(([name]) => (schema.required || []).includes(name)).map(([name, spec]) => [name, exampleValue(spec, components, depth + 1, name)]));
  if (schema.type === "array") return key === "symbols" ? ["AAPL", "MSFT"] : [];
  if (schema.type === "integer" || schema.type === "number") return Math.max(1, schema.minimum || 1);
  if (schema.type === "boolean") return false;
  return "";
}

export function catalogFromSchema(schema) {
  return Object.entries(schema.paths || {}).flatMap(([path, methods]) => Object.entries(methods).filter(([method]) => ["get", "post", "put", "patch", "delete", "head", "options"].includes(method)).map(([method, operation]) => ({path, method: method.toUpperCase(), summary: operation.summary || path, tag: operation.tags?.[0] || "Other", operation})));
}

export function curlSnippet(request) {
  const quote = value => `'${String(value).replaceAll("'", "'\\''")}'`;
  const headers = sanitize(request.headers);
  if (request.localAuth) headers["X-API-Key"] = "<YOUR_LOCAL_BACKEND_KEY>";
  const lines = [`curl --request ${request.method} ${quote(safeUrl(request.url))}`];
  for (const [key, value] of Object.entries(headers)) lines.push(`  --header ${quote(`${key}: ${value}`)}`);
  if (request.body) lines.push(`  --data-raw ${quote(JSON.stringify(sanitize(request.parsedBody)))}`);
  return lines.join(" \\\n");
}

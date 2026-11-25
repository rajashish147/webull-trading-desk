import test from "node:test";
import assert from "node:assert/strict";
import {buildRequest, catalogFromSchema, classifyRequest, curlSnippet, exampleValue, parseHeaders, previewRequest, safeBuilder, sanitize} from "../trading/web/console-core.mjs";

const base = {origin: "http://127.0.0.1:8000", method: "GET", path: "/v1/system", environment: "sandbox", headers: {}, query: [], pathParams: {}, body: "", localAuth: true};

test("query values are encoded and disabled rows omitted", () => {
  const request = buildRequest({...base, path: "/v1/market/quotes", query: [{key: "symbols", value: "AAPL,MSFT"}, {key: "ignored", value: "x", enabled: false}]});
  assert.equal(new URL(request.url).searchParams.get("symbols"), "AAPL,MSFT");
  assert.equal(new URL(request.url).searchParams.has("ignored"), false);
});
test("path parameters are required and encoded", () => {
  assert.throws(() => buildRequest({...base, path: "/v1/orders/{order_id}"}), /order_id/);
  const request = buildRequest({...base, path: "/v1/orders/{order_id}", pathParams: {order_id: "test id"}});
  assert.equal(request.path, "/v1/orders/test%20id");
});
test("GET never sends an editor body", () => {
  const request = buildRequest({...base, body: "not JSON"});
  assert.equal(request.body, null);
  assert.equal(request.parsedBody, null);
});
test("invalid POST JSON fails before execution", () => {
  assert.throws(() => buildRequest({...base, method: "POST", body: "invalid"}));
});
test("POST JSON is sent unchanged with content type", () => {
  const request = buildRequest({...base, method: "POST", path: "/v1/risk/check", body: '{"quantity":1}'});
  assert.equal(request.body, '{"quantity":1}');
  assert.equal(request.headers["Content-Type"], "application/json");
});
test("external/private/traversal paths are blocked", () => {
  for (const path of ["https://example.com", "//example.com/v1/system", "/.env", "/ui/session", "/static/.env", "/v1/../.env", "/v1/%2e%2e/.env", "/v1/system#fragment", "/v1\\system"]) assert.throws(() => buildRequest({...base, path}));
});
test("health and OpenAPI paths are allowed", () => {
  assert.equal(buildRequest({...base, path: "/health"}).path, "/health");
  assert.equal(buildRequest({...base, path: "/openapi.json"}).path, "/openapi.json");
});
test("request environment is always explicit", () => {
  assert.throws(() => buildRequest({...base, environment: "live"}));
  assert.equal(buildRequest({...base, environment: "production"}).headers["X-Trading-Environment"], "production");
});
test("managed credentials and browser headers cannot be overridden", () => {
  for (const key of ["X-API-Key", "Authorization", "Cookie", "X-CSRF-Token", "X-Trading-Environment", "Host", "Origin", "Sec-Fetch-Site"]) assert.throws(() => parseHeaders(JSON.stringify({[key]: "secret"})), /managed/);
});
test("live confirmation headers and idempotency are editable", () => {
  const headers = parseHeaders('{"X-Live-Confirmation":"short-lived","X-Live-Acknowledgement":"SUBMIT LIVE ORDER","Idempotency-Key":"test"}');
  assert.equal(headers["X-Live-Confirmation"], "short-lived");
});
test("duplicate and newline-injected headers are blocked", () => {
  assert.throws(() => parseHeaders('{"test":"a","TEST":"b"}'), /duplicate/);
  assert.throws(() => parseHeaders(JSON.stringify({test: "a\r\nb"})), /Invalid/);
});
test("headers cannot mutate prototypes", () => {
  const result = parseHeaders('{"__proto__":"safe-header"}');
  assert.equal(Object.getPrototypeOf(result), null);
  assert.equal(result.__proto__, "safe-header");
});
test("preview masks tokens but retains actionable parameters", () => {
  const request = buildRequest({...base, method: "POST", path: "/v1/orders/id/submit", headers: {"X-Live-Confirmation": "do-not-save", "X-Live-Acknowledgement": "SUBMIT LIVE ORDER"}});
  const preview = previewRequest(request);
  assert.equal(preview.headers["X-Live-Confirmation"], "[REDACTED]");
  assert.equal(preview.headers["X-Live-Acknowledgement"], "SUBMIT LIVE ORDER");
  assert.equal(JSON.stringify(preview).includes("do-not-save"), false);
});
test("recursive response redaction covers nested credentials", () => {
  const result = sanitize({token: "private", result: [{app_secret: "hidden", quantity: 1}], credentials_configured: true});
  assert.equal(result.token, "[REDACTED]");
  assert.equal(result.result[0].app_secret, "[REDACTED]");
  assert.equal(result.credentials_configured, true);
});
test("history builder redacts both inline and table query secrets", () => {
  const result = safeBuilder({...base, path: "/v1/system?token=private-inline", query: [{key: "api_key", value: "private-table"}], headers: {"X-Live-Confirmation": "private-header"}, method: "POST", body: '{"secret":"private-body"}'});
  const text = JSON.stringify(result);
  for (const secret of ["private-inline", "private-table", "private-header", "private-body"]) assert.equal(text.includes(secret), false);
});
test("history keeps unresolved path templates for manual reuse", () => {
  assert.equal(safeBuilder({...base, path: "/v1/orders/{order_id}"}).path, "/v1/orders/{order_id}");
});
test("live broker actions require extra execution review", () => {
  for (const [method, path] of [["POST", "/v1/orders/id/submit"], ["POST", "/v1/orders/id/cancel"], ["PATCH", "/v1/orders/id"]]) {
    const result = classifyRequest(buildRequest({...base, method, path, environment: "production"}), "production");
    assert.equal(result.review, true); assert.equal(result.phrase, "EXECUTE LIVE REQUEST");
  }
});
test("sandbox broker actions are reviewed but not labeled real money", () => {
  const result = classifyRequest(buildRequest({...base, method: "POST", path: "/v1/orders/id/submit"}), "sandbox");
  assert.equal(result.review, true); assert.equal(result.live, false); assert.equal(result.phrase, "");
});
test("active live context cannot be hidden by a sandbox request header", () => {
  assert.equal(classifyRequest(buildRequest({...base, method: "POST", path: "/v1/orders/id/submit"}), "production").live, true);
});
test("arming and switching to live require extra review", () => {
  assert.equal(classifyRequest(buildRequest({...base, method: "POST", path: "/v1/live/arm", body: '{"confirmation":"ENABLE LIVE TRADING"}'}), "sandbox").review, true);
  assert.equal(classifyRequest(buildRequest({...base, method: "PUT", path: "/v1/environment", body: '{"environment":"production"}'}), "sandbox").review, true);
});
test("emergency halt does not require an extra live dialog", () => {
  const result = classifyRequest(buildRequest({...base, method: "PUT", path: "/v1/risk/kill-switch", environment: "production", body: '{"enabled":true}'}), "production");
  assert.equal(result.review, false);
});
test("previews and drafts do not become automatic broker actions", () => {
  for (const path of ["/v1/orders", "/v1/risk/check", "/v1/orders/id/preview", "/v1/orders/id/live-approval", "/v1/broker/connect"]) assert.equal(classifyRequest(buildRequest({...base, method: "POST", path}), "sandbox").review, false);
});
test("safe templates never prefill live enablement acknowledgements", () => {
  assert.equal(exampleValue({const: "ENABLE LIVE TRADING"}, {}, 0, "confirmation"), "");
  assert.equal(exampleValue({type: "boolean"}, {}, 0, "enabled"), true);
  assert.equal(exampleValue({type: "string"}, {}, 0, "limit_price"), "REVIEW_PRICE");
});
test("OpenAPI catalog excludes non-method properties", () => {
  const result = catalogFromSchema({paths: {"/v1/system": {parameters: [], get: {summary: "System", tags: ["System"]}}}});
  assert.equal(result.length, 1); assert.equal(result[0].method, "GET");
});
test("cURL export uses placeholders, never session/live credentials", () => {
  const request = buildRequest({...base, method: "POST", path: "/v1/orders/id/submit?token=private-query", headers: {"X-Live-Confirmation": "private-header"}});
  const snippet = curlSnippet(request);
  assert.equal(snippet.includes("private-query"), false); assert.equal(snippet.includes("private-header"), false);
  assert.equal(snippet.includes("<YOUR_LOCAL_BACKEND_KEY>"), true);
});

test("live strategy arming and manual evaluation require console review", () => {
  for (const path of ["/v1/strategies/id/arm", "/v1/strategies/id/tick"]) {
    const result = classifyRequest(buildRequest({...base, method: "POST", path, environment: "production"}), "production");
    assert.equal(result.review, true);
    assert.equal(result.phrase, "EXECUTE LIVE REQUEST");
  }
});

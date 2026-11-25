import {buildRequest, catalogFromSchema, classifyRequest, curlSnippet, exampleValue, parseHeaders, previewRequest, safeBuilder, safeUrl, sanitize} from "./console-core.mjs";

const $ = id => document.getElementById(id);
const state = {csrf: null, system: null, schema: null, catalog: [], endpoint: null, busy: false, preparing: false,
  history: [], currentResponse: null, pending: null, variables: new Map()};
const element = (tag, text, className) => {
  const item = document.createElement(tag);
  if (text != null) item.textContent = String(text);
  if (className) item.className = className;
  return item;
};

function notice(message, error = false) {
  $("console-notice").hidden = false;
  $("console-notice").textContent = message;
  $("console-notice").className = "notification " + (error ? "error" : "info");
}

function controls() {
  $("send-request").disabled = state.busy || state.preparing || Boolean(state.pending) || !state.csrf || !state.system;
  $("reload-schema").disabled = state.busy;
  $("refresh-context").disabled = state.busy || !state.csrf;
  const response = state.currentResponse;
  $("save-response").disabled = !response || state.busy;
  $("use-response-token").disabled = state.busy || !response || response.history || !response.data?.token ||
    response.data?.intent?.environment !== "production" || !Number.isFinite(Date.parse(response.data.expires_at)) || Date.parse(response.data.expires_at) <= Date.now();
  const pending = state.pending;
  $("confirm-send").disabled = !pending || state.busy || !$("send-review-check").checked ||
    Boolean(pending.classification.phrase && $("send-phrase").value.trim() !== pending.classification.phrase);
}

async function internal(path) {
  const response = await fetch(path, {credentials: "same-origin", headers: {"X-CSRF-Token": state.csrf || ""}, redirect: "error"});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || data.detail || `Context request failed (${response.status})`);
  return data;
}

async function refreshContext(syncRequest = true) {
  state.system = await internal("/v1/system");
  const live = state.system.environment === "production";
  $("active-environment").textContent = live ? "ACTIVE: LIVE · REAL MONEY" : "ACTIVE: SANDBOX";
  $("active-environment").classList.toggle("production", live);
  $("system-context").textContent = `Active ${state.system.environment} · account …${state.system.configured_account_hint || "?"} (${state.system.selected_account?.account_label || "not validated"}) · ${state.system.connected ? "broker connected" : "broker disconnected"} · ${state.system.kill_switch.enabled ? "halted/disarmed" : "trading enabled"} · .env on server`;
  if (syncRequest && $("pin-environment").checked) $("request-environment").value = state.system.environment;
  updatePreview(); controls();
}

function requestTab(name) {
  document.querySelectorAll("[data-request-tab]").forEach(button => {
    const selected = button.dataset.requestTab === name;
    button.classList.toggle("active", selected); button.setAttribute("aria-selected", String(selected));
  });
  for (const tab of ["params", "headers", "body", "preview"]) $(`request-${tab}`).hidden = tab !== name;
}

function responseTab(name) {
  document.querySelectorAll("[data-response-tab]").forEach(button => {
    const selected = button.dataset.responseTab === name;
    button.classList.toggle("active", selected); button.setAttribute("aria-selected", String(selected));
  });
  for (const tab of ["pretty", "raw", "headers", "request"]) $(`response-${tab}`).hidden = tab !== name;
}

function addQuery(key = "", value = "", enabled = true) {
  const row = element("tr"), useCell = element("td"), keyCell = element("td"), valueCell = element("td"), actionCell = element("td");
  const use = element("input"); use.type = "checkbox"; use.checked = enabled; use.setAttribute("aria-label", "Include query parameter");
  const keyInput = element("input"); keyInput.value = key; keyInput.placeholder = "key"; keyInput.setAttribute("aria-label", "Query parameter key");
  const valueInput = element("input"); valueInput.value = value; valueInput.placeholder = "value"; valueInput.setAttribute("aria-label", "Query parameter value");
  const remove = element("button", "×", "icon-button"); remove.setAttribute("aria-label", "Remove query parameter");
  remove.addEventListener("click", () => {row.remove(); updatePreview();});
  for (const input of [use, keyInput, valueInput]) input.addEventListener("input", updatePreview);
  useCell.append(use); keyCell.append(keyInput); valueCell.append(valueInput); actionCell.append(remove);
  row.append(useCell, keyCell, valueCell, actionCell); $("query-parameters").append(row);
}

function pathFields(values = {}) {
  $("path-parameters").replaceChildren();
  const names = [...$("request-path").value.matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)].map(match => match[1]);
  for (const name of new Set(names)) {
    const label = element("label", `Path: ${name}`), input = element("input");
    input.dataset.pathName = name; input.value = values[name] || ""; input.placeholder = `Enter ${name}`;
    input.addEventListener("input", updatePreview); label.append(input); $("path-parameters").append(label);
  }
}

function builder() {
  const pathParams = Object.fromEntries([...$("path-parameters").querySelectorAll("input")].map(input => [input.dataset.pathName, input.value]));
  const query = [...$("query-parameters").querySelectorAll("tr")].map(row => {
    const inputs = row.querySelectorAll("input"); return {enabled: inputs[0].checked, key: inputs[1].value, value: inputs[2].value};
  });
  return {origin: location.origin, method: $("request-method").value, path: $("request-path").value, pathParams, query,
    headers: parseHeaders($("headers-editor").value), body: $("body-editor").value,
    environment: $("request-environment").value, localAuth: $("local-auth").checked};
}

function updatePreview() {
  const bodyAllowed = !["GET", "HEAD"].includes($("request-method").value);
  $("body-editor").disabled = !bodyAllowed;
  $("body-description").textContent = bodyAllowed ? "JSON request body" : "GET/HEAD request: no body is sent";
  const mismatch = state.system && $("request-environment").value !== state.system.environment;
  $("environment-warning").hidden = !mismatch;
  $("environment-warning").textContent = mismatch ? `Request is pinned to ${$("request-environment").value}, but the server is active in ${state.system.environment}. Normal scoped endpoints will reject it. Choosing this header does NOT switch the server.` : "";
  try { $("outgoing-preview").textContent = JSON.stringify(previewRequest(buildRequest(builder())), null, 2); }
  catch (error) { $("outgoing-preview").textContent = `Request not ready: ${error.message}`; }
  $("curl-preview").hidden = true;
}

function renderCatalog() {
  $("endpoint-list").replaceChildren();
  const search = $("endpoint-search").value.trim().toLowerCase();
  const filtered = state.catalog.filter(item => `${item.method} ${item.path} ${item.summary} ${item.tag}`.toLowerCase().includes(search));
  for (const tag of new Set(filtered.map(item => item.tag))) {
    const group = element("div", null, "endpoint-group"); group.append(element("h3", tag));
    for (const item of filtered.filter(entry => entry.tag === tag)) {
      const button = element("button", null, "endpoint-button");
      if (state.endpoint === item) button.classList.add("active");
      button.append(element("span", item.method, `method-label ${item.method}`), element("code", item.path));
      button.title = item.summary; button.addEventListener("click", () => loadEndpoint(item)); group.append(button);
    }
    $("endpoint-list").append(group);
  }
  if (!filtered.length) $("endpoint-list").append(element("p", "No matching endpoints.", "empty"));
}

function loadEndpoint(item) {
  if (state.busy || state.preparing || state.pending) return;
  state.endpoint = item;
  $("request-method").value = item.method; $("request-path").value = item.path;
  $("request-description").textContent = `${item.summary} · ${item.tag}`;
  let liveHeaders = {};
  try {liveHeaders = Object.fromEntries(Object.entries(parseHeaders($("headers-editor").value)).filter(([key]) => ["x-live-confirmation", "x-live-acknowledgement"].includes(key.toLowerCase())));} catch { /* Invalid old editor is replaced by the selected template. */ }
  $("headers-editor").value = JSON.stringify({...liveHeaders, ...(item.method === "POST" && item.path === "/v1/orders" ? {"Idempotency-Key": crypto.randomUUID()} : {})}, null, 2);
  const bodySchema = item.operation.requestBody?.content?.["application/json"]?.schema;
  $("body-editor").value = bodySchema ? JSON.stringify(exampleValue(bodySchema, state.schema.components?.schemas || {}), null, 2) : "";
  $("query-parameters").replaceChildren(); pathFields();
  for (const parameter of item.operation.parameters || []) if (parameter.in === "query") {
    const value = parameter.schema?.default ?? (parameter.name === "symbols" ? "AAPL,MSFT" : "");
    addQuery(parameter.name, value, Boolean(parameter.required || parameter.schema?.default !== undefined));
  }
  if (!$("query-parameters").children.length) addQuery();
  if ($("pin-environment").checked && state.system) $("request-environment").value = state.system.environment;
  renderCatalog(); updatePreview(); requestTab(bodySchema ? "body" : "params");
}

async function loadSchema() {
  const response = await fetch("/openapi.json", {redirect: "error"});
  if (!response.ok) throw new Error("Could not load the local OpenAPI schema.");
  state.schema = await response.json(); state.catalog = catalogFromSchema(state.schema); renderCatalog();
}

function reviewControls() {
  const pending = state.pending;
  const phraseValid = !pending?.classification.phrase || $("send-phrase").value.trim() === pending.classification.phrase;
  $("confirm-send").disabled = !pending || state.busy || !$("send-review-check").checked || !phraseValid;
}

async function prepareSend() {
  if (state.busy || state.preparing || state.pending || !state.csrf) return;
  state.preparing = true; controls();
  try {
    // Update the actual context, but never silently change the request being reviewed.
    const snapshot = builder(), request = buildRequest(snapshot);
    await refreshContext(false);
    const classification = classifyRequest(request, state.system.environment);
    if (classification.review) {
      state.pending = {request, snapshot, classification};
      $("send-review-title").textContent = classification.live ? "LIVE API REQUEST · REAL MONEY" : "Review broker request";
      $("send-review-warning").textContent = `${classification.label}.\nActive server: ${state.system.environment} · account …${state.system.configured_account_hint || "?"} (${state.system.selected_account?.account_label || "not validated"}) · request header: ${request.environment}.\nThis executes once; a timeout is not proof the broker rejected the operation.`;
      $("send-review-request").textContent = JSON.stringify(previewRequest(request), null, 2);
      $("send-phrase-label").hidden = !classification.phrase; $("send-phrase").value = ""; $("send-review-check").checked = false;
      $("confirm-send").classList.toggle("live-action", classification.live); $("send-review").showModal(); reviewControls();
    } else await execute(request, snapshot);
  } catch (error) { notice(error.message, true); }
  finally {state.preparing = false; controls();}
}

async function execute(request, snapshot) {
  if (state.busy) return;
  state.busy = true; state.pending = null; controls();
  const start = performance.now();
  $("response-summary").textContent = `${request.method} ${safeUrl(request.url)} · executing once…`;
  const headers = {...request.headers};
  if (request.localAuth) headers["X-CSRF-Token"] = state.csrf;
  try {
    const response = await fetch(request.url, {method: request.method, headers, body: request.body,
      credentials: request.localAuth ? "same-origin" : "omit", redirect: "manual"});
    if (response.type === "opaqueredirect") throw new Error("Redirect blocked to avoid replaying an action. Use the canonical endpoint path (usually without a trailing slash).");
    const raw = await response.text();
    let data;
    try { data = raw ? JSON.parse(raw) : null; } catch { data = raw; }
    const result = {request: previewRequest(request), status: response.status, statusText: response.statusText,
      duration: Math.round(performance.now() - start), bytes: new TextEncoder().encode(raw).length,
      headers: Object.fromEntries(response.headers.entries()), raw, data, history: false};
    state.currentResponse = result; renderResponse(result);
    state.history.unshift({time: new Date().toLocaleTimeString(), builder: safeBuilder(snapshot),
      result: {...result, data: sanitize(data), raw: typeof data === "object" ? JSON.stringify(sanitize(data), null, 2) : String(data ?? ""), headers: sanitize(result.headers), history: true}});
    state.history = state.history.slice(0, 20); renderHistory();
    const id = data?.id || data?.intent?.order_id;
    if (id) state.variables.set(data?.environment || data?.intent?.environment || request.environment, {order_id: id, client_order_id: data?.client_order_id || id});
    notice(response.ok ? `${response.status} response received. Inspect the body/state; HTTP success is not a guaranteed fill.` : `${response.status} response received. Error details are shown below. Do not blindly retry broker mutations.`, !response.ok);
  } catch (error) {
    const result = {request: previewRequest(request), status: 0, statusText: "Transport failure", duration: Math.round(performance.now() - start), bytes: 0, headers: {}, raw: error.message, data: {error: error.message}, history: false};
    state.currentResponse = result; renderResponse(result);
    notice(`${error.message}\nIf this was a broker mutation, inspect order/broker history before retrying; the request may have succeeded.`, true);
  } finally {
    try {
      const remaining = Object.fromEntries(Object.entries(parseHeaders($("headers-editor").value)).filter(([key]) => !["x-live-confirmation", "x-live-acknowledgement"].includes(key.toLowerCase())));
      if (Object.keys(request.headers).some(key => key.toLowerCase() === "x-live-confirmation")) $("headers-editor").value = JSON.stringify(remaining, null, 2);
    } catch { /* Do not overwrite an editor the user changed to invalid JSON. */ }
    try { await refreshContext(); } catch (error) { notice(`Response preserved; context refresh failed: ${error.message}`, true); }
    state.busy = false; controls();
  }
}

function renderResponse(result) {
  $("response-summary").textContent = `${result.request.method} ${result.request.url}${result.history ? " · loaded from redacted history; NOT executed" : ""}`;
  $("response-status").textContent = result.status ? `${result.status} ${result.statusText}` : "Transport failure";
  $("response-status").className = `badge ${result.status >= 200 && result.status < 300 ? "success" : "danger"}`;
  $("response-time").textContent = `${result.duration} ms`; $("response-size").textContent = `${result.bytes.toLocaleString()} bytes`;
  $("response-pretty").textContent = result.data == null ? "(empty response)" : typeof result.data === "object" ? JSON.stringify(result.data, null, 2) : String(result.data);
  $("response-raw").textContent = result.raw || "(empty response)";
  $("response-headers").textContent = JSON.stringify(sanitize(result.headers), null, 2);
  $("response-request").textContent = JSON.stringify(result.request, null, 2);
  responseTab("pretty"); controls();
}

function renderHistory() {
  $("history-list").replaceChildren();
  for (const entry of state.history) {
    const button = element("button", null, "history-button"), summary = element("div");
    button.append(element("span", entry.builder.method, `method-label ${entry.builder.method}`));
    summary.append(element("code", entry.builder.path), element("small", `${entry.time} · ${entry.builder.environment} · HTTP ${entry.result.status}`)); button.append(summary);
    button.addEventListener("click", () => {
      if (state.busy) return;
      const value = entry.builder;
      $("request-method").value = value.method; $("request-path").value = value.path;
      $("request-environment").value = value.environment; $("pin-environment").checked = false; $("local-auth").checked = value.localAuth;
      $("headers-editor").value = JSON.stringify(value.headers, null, 2); $("body-editor").value = value.body;
      pathFields(value.pathParams); $("query-parameters").replaceChildren(); value.query.forEach(item => addQuery(item.key, item.value, item.enabled));
      state.currentResponse = entry.result; renderResponse(entry.result); updatePreview();
      notice("Loaded redacted history, not executed. Supply fresh live approval headers if needed. The original request environment stays pinned.");
    });
    $("history-list").append(button);
  }
  if (!state.history.length) $("history-list").append(element("p", "No requests sent.", "empty"));
}

document.querySelectorAll("[data-request-tab]").forEach(button => button.addEventListener("click", () => requestTab(button.dataset.requestTab)));
document.querySelectorAll("[data-response-tab]").forEach(button => button.addEventListener("click", () => responseTab(button.dataset.responseTab)));
$("endpoint-search").addEventListener("input", renderCatalog);
$("request-path").addEventListener("input", () => {pathFields(); updatePreview();});
for (const id of ["request-method", "request-environment", "local-auth", "headers-editor", "body-editor"]) $(id).addEventListener("input", () => {
  if (id === "request-environment") $("pin-environment").checked = false; updatePreview();
});
$("pin-environment").addEventListener("change", () => {if ($("pin-environment").checked && state.system) $("request-environment").value = state.system.environment; updatePreview();});
$("add-query").addEventListener("click", () => {addQuery(); updatePreview();});
$("send-request").addEventListener("click", prepareSend);
$("refresh-context").addEventListener("click", () => refreshContext().catch(error => notice(error.message, true)));
$("reload-schema").addEventListener("click", () => loadSchema().catch(error => notice(error.message, true)));
for (const [button, editor] of [["format-body", "body-editor"], ["format-headers", "headers-editor"]]) $(button).addEventListener("click", () => {
  try {$(editor).value = JSON.stringify(JSON.parse($(editor).value || "{}"), null, 2); updatePreview();} catch (error) {notice(`Invalid JSON: ${error.message}`, true);}
});
$("generate-idempotency").addEventListener("click", () => {
  try {const value = parseHeaders($("headers-editor").value); value["Idempotency-Key"] = crypto.randomUUID(); $("headers-editor").value = JSON.stringify(value, null, 2); updatePreview();} catch (error) {notice(error.message, true);}
});
$("show-curl").addEventListener("click", () => {
  try {$("curl-preview").textContent = "# POSIX shell / Git Bash template; replace credential placeholders locally.\n" + curlSnippet(buildRequest(builder())); $("curl-preview").hidden = false;} catch (error) {notice(error.message, true);}
});
$("use-last-id").addEventListener("click", () => {
  const values = state.variables.get($("request-environment").value);
  if (!values) return notice("No response order IDs captured for this environment yet.", true);
  for (const input of $("path-parameters").querySelectorAll("input")) if (values[input.dataset.pathName]) input.value = values[input.dataset.pathName];
  updatePreview();
});
$("use-response-token").addEventListener("click", () => {
  try {
    const result = state.currentResponse;
    if (!result || result.history || !result.data?.token || !Number.isFinite(Date.parse(result.data.expires_at)) || Date.parse(result.data.expires_at) <= Date.now()) throw new Error("Use a fresh current live-approval response, not redacted history.");
    const headers = Object.fromEntries(Object.entries(parseHeaders($("headers-editor").value)).filter(([key]) => !["x-live-confirmation", "x-live-acknowledgement"].includes(key.toLowerCase())));
    headers["X-Live-Confirmation"] = result.data.token; headers["X-Live-Acknowledgement"] = result.data.acknowledgement;
    $("headers-editor").value = JSON.stringify(headers, null, 2); $("request-environment").value = "production"; $("pin-environment").checked = false;
    updatePreview(); requestTab("headers"); notice("Live approval headers inserted locally. Nothing executed. Choose the exact matching submit/replace/cancel request before the approval expires.");
  } catch (error) {notice(error.message, true);}
});
$("save-response").addEventListener("click", () => {
  const result = state.currentResponse; if (!result) return;
  const blob = new Blob([JSON.stringify({request: result.request, status: result.status, headers: sanitize(result.headers), body: sanitize(result.data)}, null, 2)], {type: "application/json"});
  const url = URL.createObjectURL(blob), link = element("a"); link.href = url; link.download = "api-response-redacted.json"; link.click(); URL.revokeObjectURL(url);
});
$("clear-history").addEventListener("click", () => {state.history = []; renderHistory();});
for (const id of ["close-send-review", "back-send-review"]) $(id).addEventListener("click", () => {state.pending = null; $("send-review").close(); controls();});
$("send-review").addEventListener("cancel", () => {state.pending = null; controls();});
$("send-review-check").addEventListener("change", reviewControls); $("send-phrase").addEventListener("input", reviewControls);
$("confirm-send").addEventListener("click", () => {
  reviewControls(); if ($("confirm-send").disabled) return;
  const pending = state.pending; $("send-review").close(); execute(pending.request, pending.snapshot);
});

async function boot() {
  controls();
  try {
    const response = await fetch("/ui/session", {headers: {"X-Local-UI": "1"}, credentials: "same-origin", redirect: "error"});
    if (!response.ok) throw new Error("Open this console on localhost on the same machine as the Python server.");
    state.csrf = (await response.json()).csrf_token;
    await refreshContext(); await loadSchema();
    loadEndpoint(state.catalog.find(item => item.path === "/v1/system" && item.method === "GET") || state.catalog[0]);
    notice("Ready. Only session/schema/system metadata loaded automatically. .env keys stay on the server; manual requests run only when you click Send.");
  } catch (error) {notice(error.message, true);}
  controls();
}
boot();
// Expiry indicators only; this timer never executes requests.
setInterval(controls, 1000);

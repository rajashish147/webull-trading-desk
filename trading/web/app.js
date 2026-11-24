"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  csrf: null, system: null, busy: 0, orders: [], orderOffset: 0, orderLimit: 25,
  selected: null, watchlist: ["AAPL", "MSFT"], quotes: [], auditAfter: 0,
  auditHasNext: false, historyKey: null, confirm: null,
};
const money = (value) => value == null || !Number.isFinite(Number(value)) ? "—" :
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Number(value));
const price = (value) => value == null || !Number.isFinite(Number(value)) ? "—" :
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 4 }).format(Number(value));
const date = (value) => value ? new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text != null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
};

function notify(message, kind = "info") {
  $("notification").textContent = message;
  $("notification").className = "notification" + (kind === "error" ? " error" : kind === "success" ? "" : " info");
  $("notification").hidden = false;
}

async function api(path, method = "GET", body, extraHeaders = {}) {
  const headers = { "X-CSRF-Token": state.csrf || "", ...extraHeaders };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const violations = data.error?.details?.violations;
    const detail = Array.isArray(data.detail) ? data.detail.map((item) => `${item.loc?.slice(1).join(".")}: ${item.msg}`).join("\n") : data.detail;
    const message = violations?.length ? violations.join("\n") : data.error?.message || detail || `Request failed (HTTP ${response.status}).`;
    throw new Error(response.status === 401 ? "Your local session expired. Reload this page to reconnect to the server." : message);
  }
  return data;
}

async function run(action, emergency = false) {
  if (state.busy && !emergency) return;
  state.busy += 1;
  updateControls();
  try { await action(); }
  catch (error) { notify(error.message || "Something went wrong. Check the server terminal.", "error"); }
  finally { state.busy -= 1; updateControls(); }
}

function updateControls() {
  const ready = Boolean(state.csrf), busy = state.busy > 0;
  const connected = Boolean(state.system?.connected);
  const sandbox = state.system?.environment === "sandbox";
  const halted = state.system?.kill_switch?.enabled !== false;
  document.querySelectorAll("button").forEach((button) => {
    if (button.dataset.tab || button.classList.contains("icon-button") || ["confirm-back", "modify-back"].includes(button.id)) return;
    button.disabled = !ready || busy || button.dataset.blocked === "true";
  });
  ["check-risk", "use-quote", "fetch-quotes", "fetch-bars", "load-history", "reconcile-all"].forEach((id) => {
    $(id).disabled = !ready || busy || !connected;
  });
  $("connect").disabled = !ready || busy || connected;
  $("connect").textContent = connected ? "Connected" : state.busy && !connected ? "Working…" : "Connect Webull";
  $("enable-trading").disabled = !ready || busy || !connected || !sandbox || !halted;
  $("halt-trading").disabled = !ready;
  $("orders-previous").disabled = !ready || busy || state.orderOffset === 0;
  $("orders-next").disabled = !ready || busy || state.orders.length < state.orderLimit;
  $("audit-next").disabled = !ready || busy || !state.auditHasNext;
  $("history-next").disabled = !ready || busy || !connected || !state.historyKey;
  $("confirm-action").disabled = !ready || busy || !$("confirm-check").checked || !state.confirm;
}

function setTab(tab) {
  document.querySelectorAll(".tab-panel").forEach((panel) => { panel.hidden = panel.id !== `panel-${tab}`; });
  document.querySelectorAll(".nav-item").forEach((item) => {
    const active = item.dataset.tab === tab;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
  });
  $("page-title").textContent = { overview: "Overview", orders: "Orders", market: "Market data", activity: "Activity", settings: "Settings" }[tab];
}

function renderSystem() {
  const system = state.system, connected = Boolean(system.connected), halted = system.kill_switch.enabled;
  $("environment").textContent = system.environment;
  $("environment").classList.toggle("production", system.environment !== "sandbox");
  $("connection-label").textContent = connected ? "Webull connected" : "Not connected";
  $("connection-dot").classList.toggle("muted", !connected);
  $("account-label").textContent = connected ? `${system.selected_account?.account_label || "Individual Cash"} · ${system.environment}` : "Credentials are loaded from .env. Connect to load your account.";
  $("trading-status").textContent = halted ? "Trading halted" : "Sandbox trading enabled";
  $("trading-status").className = "badge " + (halted ? "warning" : "success");
  $("halt-description").textContent = system.environment !== "sandbox" ? "Production orders are disabled. Read-only access." : halted ? "Drafts and previews are safe. Submissions and replacements are blocked." : "Order submissions still require your explicit confirmation. Halting does not cancel existing orders.";
  const account = system.selected_account?.account_id;
  renderList($("configuration"), {
    "Configuration source": system.configuration_source || ".env",
    "Webull credentials": system.credentials_configured ? "Configured · kept on server" : "Missing · edit .env and restart",
    "Environment": system.environment,
    "Broker endpoint": system.broker_endpoint,
    "Selected account": account ? `${account.slice(0, 4)}…${account.slice(-4)}` : "Connect to validate account",
    "Browser access": "Automatic local session · no key entry",
    "Production orders": "Disabled",
    "Order reconciliation": `${system.reconcile_interval_seconds}s · read-only`,
  });
  updateControls();
}

function renderList(target, values) {
  target.replaceChildren();
  for (const [key, value] of Object.entries(values)) {
    const row = node("div"); row.append(node("dt", key), node("dd", value)); target.append(row);
  }
}

function tableEmpty(target, columns, message) {
  const row = node("tr"), cell = node("td", message, "empty"); cell.colSpan = columns; row.append(cell); target.replaceChildren(row);
}

function badge(status) {
  const style = status === "FILLED" ? "success" : ["UNKNOWN", "REJECTED"].includes(status) ? "danger" :
    ["SUBMITTING", "MODIFYING", "CANCELLING", "ACKNOWLEDGED"].includes(status) ? "warning" : ["OPEN", "PARTIALLY_FILLED"].includes(status) ? "blue" : "neutral";
  return node("span", status.replaceAll("_", " "), `badge ${style}`);
}

async function loadPortfolio() {
  const result = await api("/v1/portfolio"), balance = result.balance;
  const asset = balance.account_currency_assets?.find((item) => item.currency === "USD") || {};
  $("cash-value").textContent = money(balance.total_cash_balance ?? asset.cash_balance);
  $("buying-power").textContent = money(asset.buying_power);
  $("portfolio-value").textContent = money(balance.total_net_liquidation_value ?? asset.net_liquidation_value);
  $("day-pnl").textContent = money(balance.total_day_profit_loss);
  $("day-pnl").className = Number(balance.total_day_profit_loss) < 0 ? "negative" : Number(balance.total_day_profit_loss) > 0 ? "positive" : "";
  $("position-count").textContent = result.positions.length;
  $("positions").replaceChildren();
  for (const position of result.positions) {
    const row = node("tr");
    row.append(node("td", position.symbol), node("td", position.quantity), node("td", price(position.last_price)), node("td", money(position.unrealized_profit_loss), Number(position.unrealized_profit_loss) < 0 ? "negative" : "positive"));
    $("positions").append(row);
  }
  if (!result.positions.length) tableEmpty($("positions"), 4, "No positions. Your account has no holdings yet.");
}

async function fetchQuotes() {
  const symbols = $("watchlist-symbols").value.split(",").map((symbol) => symbol.trim().toUpperCase()).filter(Boolean);
  if (!symbols.length || symbols.length > 100) throw new Error("Enter between 1 and 100 comma-separated symbols.");
  state.quotes = await api(`/v1/market/quotes?symbols=${encodeURIComponent(symbols.join(","))}`);
  renderQuotes();
}

function renderQuotes() {
  for (const id of ["overview-quotes", "market-quotes"]) {
    const target = $(id); target.replaceChildren();
    for (const quote of state.quotes) {
      const row = node("tr", null, "selectable"), symbol = node("td", null, "symbol-cell");
      const choose = node("button", quote.symbol, "text-button");
      choose.addEventListener("click", () => chooseQuote(quote)); symbol.append(choose, node("small", "US equity / ETF"));
      const change = Number(quote.change_ratio) * 100;
      row.append(symbol, node("td", price(quote.price)), node("td", Number.isFinite(change) ? `${change >= 0 ? "+" : ""}${change.toFixed(2)}%` : "—", change < 0 ? "negative" : "positive"), node("td", `${price(quote.bid)} / ${price(quote.ask)}`));
      target.append(row);
    }
    if (!state.quotes.length) tableEmpty(target, 4, "No quote data returned. Check your market-data permissions.");
  }
  if (!$("ticket-price").value) {
    const quote = state.quotes.find((item) => item.symbol === $("ticket-symbol").value.toUpperCase());
    if (quote) $("ticket-price").value = quote.ask || quote.price;
  }
  updateNotional();
}

function chooseQuote(quote) {
  $("ticket-symbol").value = quote.symbol;
  $("ticket-price").value = $("ticket-side").value === "SELL" ? quote.bid || quote.price : quote.ask || quote.price;
  $("bars-symbol").value = quote.symbol;
  $("risk-result").hidden = true;
  updateNotional(); setTab("overview"); $("ticket-price").focus();
}

function ticketSpec() {
  if (!$("ticket-form").reportValidity()) return null;
  const quantity = Number($("ticket-quantity").value);
  if (!Number.isInteger(quantity) || quantity < 1) throw new Error("Shares must be a positive whole number.");
  return { symbol: $("ticket-symbol").value.trim().toUpperCase(), side: $("ticket-side").value, quantity,
    limit_price: $("ticket-price").value, order_type: "LIMIT", time_in_force: $("ticket-tif").value, trading_session: "CORE" };
}

function updateNotional() {
  $("ticket-notional").textContent = $("ticket-price").value ? money(Number($("ticket-quantity").value) * Number($("ticket-price").value)) : "—";
}

async function loadOrders() {
  const data = await api(`/v1/orders?limit=${state.orderLimit}&offset=${state.orderOffset}`);
  state.orders = data.data;
  $("orders").replaceChildren(); $("recent-orders").replaceChildren();
  for (const order of state.orders) {
    const row = node("tr");
    row.append(node("td", date(order.created_at)), node("td", order.spec.symbol), node("td", order.spec.side, order.spec.side === "SELL" ? "negative" : "positive"), node("td", `${order.spec.quantity} / ${order.filled_quantity}`), node("td", price(order.spec.limit_price)));
    const status = node("td"); status.append(badge(order.state));
    const actions = node("td"), details = node("button", "Details", "button secondary small");
    details.addEventListener("click", () => showOrder(order)); actions.append(details); row.append(status, actions); $("orders").append(row);
  }
  for (const order of state.orders.slice(0, 4)) {
    const row = node("div", null, "recent-order"), summary = node("div"), actions = node("div");
    summary.append(node("strong", `${order.spec.side} ${order.spec.quantity} ${order.spec.symbol}`), node("small", `${price(order.spec.limit_price)} limit · ${date(order.created_at)}`));
    const details = node("button", "View", "text-button"); details.addEventListener("click", () => showOrder(order));
    actions.append(badge(order.state), details); row.append(summary, actions); $("recent-orders").append(row);
  }
  if (!state.orders.length) { tableEmpty($("orders"), 7, "No orders on this page. Create an offline draft to get started."); $("recent-orders").append(node("p", "No orders yet. Start with an offline draft.", "empty")); }
  $("orders-page").textContent = `Page ${Math.floor(state.orderOffset / state.orderLimit) + 1}`;
  if (state.selected && $("order-dialog").open) {
    const latest = state.orders.find((item) => item.id === state.selected.id);
    if (latest) showOrder(latest);
  }
  updateControls();
}

function actionButton(label, callback, blocked, kind = "secondary") {
  const button = node("button", label, `button ${kind}`);
  button.dataset.blocked = String(blocked);
  button.addEventListener("click", callback);
  $("order-actions").append(button);
}

function showOrder(order) {
  const changed = state.selected?.id !== order.id;
  state.selected = order;
  if (changed) $("order-preview").hidden = true;
  $("order-dialog-title").textContent = `${order.spec.side} ${order.spec.quantity} ${order.spec.symbol}`;
  $("order-detail").replaceChildren();
  for (const [label, value] of Object.entries({ "Status": order.state.replaceAll("_", " "), "Limit": price(order.spec.limit_price), "Filled": `${order.filled_quantity} / ${order.spec.quantity}`, "Duration": order.spec.time_in_force, "Session": "Regular hours", "Order ID": `${order.id.slice(0, 8)}…` })) {
    const cell = node("div"); cell.append(node("small", label), node("strong", value)); $("order-detail").append(cell);
  }
  $("order-json").textContent = JSON.stringify(order, null, 2);
  $("order-warning").hidden = !order.last_error && !order.pending_action;
  $("order-warning").className = "notification error";
  $("order-warning").textContent = order.last_error || `Pending ${order.pending_action}. Reconcile before taking another action.`;
  $("order-actions").replaceChildren();
  const connected = Boolean(state.system?.connected), halted = state.system?.kill_switch.enabled !== false;
  const writable = connected && state.system?.environment === "sandbox";
  if (order.state === "DRAFT") {
    actionButton("Preview & risk", () => run(previewSelected), !connected);
    actionButton("Submit sandbox order", () => confirmAction("Submit sandbox order", `Send BUY/SELL order: ${order.spec.side} ${order.spec.quantity} ${order.spec.symbol} at a ${price(order.spec.limit_price)} limit (${order.spec.time_in_force})? This sends an order to the Individual Cash sandbox account and may fill.`, "Submit sandbox order", submitSelected), !writable || halted, "primary");
  }
  if (["OPEN", "PARTIALLY_FILLED"].includes(order.state) && !order.pending_action) {
    actionButton("Modify", () => {
      $("modify-quantity").value = order.spec.quantity;
      $("modify-quantity").min = String(Math.floor(Number(order.filled_quantity)) + 1);
      $("modify-price").value = order.spec.limit_price;
      $("modify-dialog").showModal();
    }, !writable || halted);
  }
  if (!["DRAFT", "CANCELLED", "REJECTED", "EXPIRED"].includes(order.state)) actionButton("Reconcile", () => run(reconcileSelected), !connected);
  if (!["FILLED", "CANCELLED", "REJECTED", "EXPIRED"].includes(order.state)) {
    actionButton(order.state === "DRAFT" ? "Cancel draft" : "Cancel order", () => confirmAction("Cancel this order?", order.state === "DRAFT" ? "Cancel this offline draft. It has not been submitted to Webull." : `Request cancellation of ${order.spec.symbol} order ${order.id.slice(0, 8)}…. A cancellation request does not guarantee that the order has not filled.`, "Confirm cancellation", cancelSelected), !writable || Boolean(order.pending_action), "danger");
  }
  if (!$("order-dialog").open) $("order-dialog").showModal();
  updateControls();
}

async function previewSelected() {
  const result = await api(`/v1/orders/${state.selected.id}/preview`, "POST");
  $("order-preview").hidden = false;
  $("order-preview").className = "preview-output" + (result.risk.allowed ? "" : " error");
  $("order-preview").textContent = result.risk.allowed ? `Risk checks passed. Nothing submitted.\n\n${JSON.stringify(result.broker_preview, null, 2)}` : `Risk checks blocked this draft:\n${result.risk.violations.join("\n")}\n\nNothing submitted.`;
}

function confirmAction(title, message, label, callback) {
  state.confirm = callback;
  $("confirm-title").textContent = title; $("confirm-message").textContent = message;
  $("confirm-action").textContent = label; $("confirm-check").checked = false;
  $("confirm-check-label").textContent = "I reviewed the account and this action.";
  $("confirm-dialog").showModal(); updateControls();
}

async function updateAfterMutation(order, message) {
  showOrder(order);
  notify(order.last_error ? `${order.state}: ${order.last_error}` : `${message} · ${order.state.replaceAll("_", " ")}`, order.last_error || ["UNKNOWN", "REJECTED"].includes(order.state) ? "error" : "success");
  await loadOrders();
  if (state.system.connected) await loadPortfolio();
  $("order-report").textContent = JSON.stringify(await api("/v1/reports/orders"), null, 2);
}

async function submitSelected() {
  const order = await api(`/v1/orders/${state.selected.id}/submit`, "POST");
  await updateAfterMutation(order, "Submission processed — check status and fills");
}
async function cancelSelected() {
  const draft = state.selected.state === "DRAFT";
  const order = await api(`/v1/orders/${state.selected.id}/cancel`, "POST");
  await updateAfterMutation(order, draft ? "Offline draft cancelled" : "Cancellation processed — check final status");
}
async function reconcileSelected() {
  await updateAfterMutation(await api(`/v1/orders/${state.selected.id}/reconcile`, "POST"), "Broker state reconciled");
}

async function loadAudit(next = false) {
  const result = await api(`/v1/audit?after=${next ? state.auditAfter : 0}&limit=100`);
  $("audit").replaceChildren();
  for (const event of [...result.data].reverse()) {
    const row = node("tr");
    row.append(node("td", event.sequence), node("td", date(event.timestamp)), node("td", event.action.replaceAll("_", " ")), node("td", JSON.stringify(event.details), "audit-details"));
    $("audit").append(row);
  }
  if (!result.data.length) tableEmpty($("audit"), 4, "No audit events on this page.");
  state.auditAfter = result.data.at(-1)?.sequence || state.auditAfter;
  state.auditHasNext = result.data.length === 100; updateControls();
}

async function loadHistory(next = false) {
  const response = await api(`/v1/broker/order-history${next && state.historyKey ? `?pagination_key=${encodeURIComponent(state.historyKey)}` : ""}`);
  $("broker-history").textContent = JSON.stringify(response, null, 2);
  state.historyKey = response.pagination_key || null; updateControls();
}

function drawChart(bars) {
  const points = [...bars].filter((bar) => Number.isFinite(Number(bar.close))).sort((a, b) => new Date(a.time) - new Date(b.time));
  const svg = $("price-chart"); svg.replaceChildren();
  const svgNode = (tag, attributes, text) => {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    if (text) element.textContent = text;
    svg.append(element); return element;
  };
  if (!points.length) { svgNode("text", { x: 400, y: 120, "text-anchor": "middle", class: "chart-label" }, "No closing-price data returned"); return; }
  const values = points.map((bar) => Number(bar.close)), low = Math.min(...values), high = Math.max(...values), range = high - low || 1;
  for (let line = 0; line < 4; line++) {
    const y = 20 + line * 55;
    svgNode("line", { x1: 75, x2: 780, y1: y, y2: y, class: "chart-grid" });
    svgNode("text", { x: 65, y: y + 4, "text-anchor": "end", class: "chart-label" }, price(high - line * (high - low) / 3));
  }
  svgNode("polyline", { class: "chart-line", points: points.map((bar, index) => `${75 + index / Math.max(1, points.length - 1) * 705},${20 + (high - Number(bar.close)) / range * 165}`).join(" ") });
  svgNode("text", { x: 75, y: 220, class: "chart-label" }, date(points[0].time));
  svgNode("text", { x: 780, y: 220, "text-anchor": "end", class: "chart-label" }, date(points.at(-1).time));
}

async function fetchBars() {
  const symbol = $("bars-symbol").value.trim().toUpperCase();
  const result = await api(`/v1/market/bars/${encodeURIComponent(symbol)}?timespan=${encodeURIComponent($("bars-interval").value)}&count=${encodeURIComponent($("bars-count").value)}`);
  const group = result.result?.find((item) => item.symbol === symbol) || result.result?.[0];
  const bars = group?.result || [];
  drawChart(bars);
  $("chart-summary").textContent = `${symbol} · ${bars.length} bars returned`;
  $("bars").replaceChildren();
  for (const bar of [...bars].sort((a, b) => new Date(b.time) - new Date(a.time)).slice(0, 100)) {
    const row = node("tr"); row.append(node("td", date(bar.time)), node("td", price(bar.open)), node("td", price(bar.high)), node("td", price(bar.low)), node("td", price(bar.close)), node("td", Number(bar.volume).toLocaleString())); $("bars").append(row);
  }
  if (!bars.length) tableEmpty($("bars"), 6, "No bars returned. Check the selected interval and your data permissions.");
}

async function refreshAll() {
  state.system = await api("/v1/system"); renderSystem();
  const reads = [loadOrders(), loadAudit(), api("/v1/reports/orders").then((result) => { $("order-report").textContent = JSON.stringify(result, null, 2); })];
  if (state.system.connected) reads.push(loadPortfolio(), fetchQuotes());
  const outcomes = await Promise.allSettled(reads);
  $("last-refresh").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  const failures = outcomes.filter((result) => result.status === "rejected");
  if (failures.length) notify(failures.map((result) => result.reason.message).join("\n"), "error");
}

document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => setTab(button.dataset.tab)));
$("refresh-all").addEventListener("click", () => run(refreshAll));
$("connect").addEventListener("click", () => run(async () => {
  notify("Connecting to Webull. If requested, approve verification in the Webull App.");
  await api("/v1/broker/connect", "POST");
  notify("Webull connected. Trading remains halted until you explicitly enable it.", "success");
  await refreshAll();
}));
$("halt-trading").addEventListener("click", () => run(async () => {
  await api("/v1/risk/kill-switch", "PUT", { enabled: true, reason: "Personal dashboard emergency halt" });
  state.system = await api("/v1/system"); renderSystem();
  if (state.selected && $("order-dialog").open) showOrder(state.selected);
  notify("Trading halted. Existing broker orders are NOT automatically cancelled; inspect and cancel them explicitly.", "success");
}, true));
$("enable-trading").addEventListener("click", () => confirmAction("Enable sandbox trading?", "Allow explicit order submissions and replacements in the sandbox. No order is placed by enabling trading. Review your account and risk limits first.", "Enable sandbox trading", async () => {
  await api("/v1/risk/kill-switch", "PUT", { enabled: false, reason: "Personal dashboard reviewed sandbox session" });
  state.system = await api("/v1/system"); renderSystem();
  if (state.selected && $("order-dialog").open) showOrder(state.selected);
  notify("Sandbox trading enabled. Every order still needs a separate submission confirmation.", "success");
}));
$("ticket-form").addEventListener("input", () => { updateNotional(); $("risk-result").hidden = true; });
$("ticket-form").addEventListener("submit", (event) => {
  event.preventDefault(); run(async () => {
    const spec = ticketSpec(); if (!spec) return;
    const serialized = JSON.stringify(spec);
    let intent;
    try { intent = JSON.parse(sessionStorage.getItem("webull_draft_intent") || "null"); } catch { intent = null; }
    if (!intent || intent.spec !== serialized) intent = { key: crypto.randomUUID(), spec: serialized };
    sessionStorage.setItem("webull_draft_intent", JSON.stringify(intent));
    const order = await api("/v1/orders", "POST", spec, { "Idempotency-Key": intent.key });
    sessionStorage.removeItem("webull_draft_intent");
    await loadOrders(); showOrder(order); notify("Offline draft created. Nothing has been sent to Webull.", "success");
  });
});
$("check-risk").addEventListener("click", () => run(async () => {
  const spec = ticketSpec(); if (!spec) return;
  const result = await api("/v1/risk/check", "POST", spec), target = $("risk-result");
  target.hidden = false; target.className = "risk-result" + (result.allowed ? "" : " error"); target.replaceChildren(node("strong", result.allowed ? "Risk checks passed · nothing submitted" : "Risk checks blocked this order"));
  if (!result.allowed) { const list = node("ul"); result.violations.forEach((message) => list.append(node("li", message))); target.append(list); }
  else target.append(node("p", `Estimated cost with buffer: ${money(result.metrics.estimated_cost_with_buffer)}`));
}));
$("use-quote").addEventListener("click", () => run(async () => {
  const symbol = $("ticket-symbol").value.trim().toUpperCase();
  const quotes = await api(`/v1/market/quotes?symbols=${encodeURIComponent(symbol)}`);
  if (!quotes.length) throw new Error("No quote returned for this symbol.");
  const quote = quotes[0]; $("ticket-price").value = $("ticket-side").value === "SELL" ? quote.bid || quote.price : quote.ask || quote.price;
  updateNotional(); $("risk-result").hidden = true;
}));
$("watchlist-form").addEventListener("submit", (event) => {
  event.preventDefault(); run(async () => {
    const symbols = $("watchlist-symbols").value.split(",").map((symbol) => symbol.trim().toUpperCase()).filter(Boolean);
    const result = await api("/v1/watchlist", "PUT", { symbols }); state.watchlist = result.symbols;
    $("watchlist-symbols").value = state.watchlist.join(", "); notify("Local watchlist saved.", "success");
  });
});
$("fetch-quotes").addEventListener("click", () => run(fetchQuotes));
$("bars-form").addEventListener("submit", (event) => { event.preventDefault(); run(fetchBars); });
$("refresh-orders").addEventListener("click", () => run(loadOrders));
$("orders-previous").addEventListener("click", () => run(async () => { state.orderOffset = Math.max(0, state.orderOffset - state.orderLimit); await loadOrders(); }));
$("orders-next").addEventListener("click", () => run(async () => { state.orderOffset += state.orderLimit; await loadOrders(); }));
$("reconcile-all").addEventListener("click", () => run(async () => { await api("/v1/reconcile", "POST"); await loadOrders(); notify("Active order reconciliation requested. Inspect order states for any unresolved outcomes.", "success"); }));
$("refresh-audit").addEventListener("click", () => run(() => loadAudit(false)));
$("audit-next").addEventListener("click", () => run(() => loadAudit(true)));
$("load-history").addEventListener("click", () => run(() => loadHistory(false)));
$("history-next").addEventListener("click", () => run(() => loadHistory(true)));
$("close-order").addEventListener("click", () => $("order-dialog").close());
for (const id of ["close-confirm", "confirm-back"]) $(id).addEventListener("click", () => { $("confirm-dialog").close(); state.confirm = null; });
$("confirm-dialog").addEventListener("cancel", () => { state.confirm = null; });
$("confirm-check").addEventListener("change", updateControls);
$("confirm-action").addEventListener("click", () => {
  const callback = state.confirm; if (!callback || !$("confirm-check").checked) return;
  state.confirm = null; $("confirm-dialog").close(); run(callback);
});
for (const id of ["close-modify", "modify-back"]) $(id).addEventListener("click", () => $("modify-dialog").close());
$("modify-form").addEventListener("submit", (event) => {
  event.preventDefault(); run(async () => {
    const quantity = Number($("modify-quantity").value);
    if (!Number.isInteger(quantity)) throw new Error("Quantity must be a whole number.");
    const order = await api(`/v1/orders/${state.selected.id}`, "PATCH", { quantity, limit_price: $("modify-price").value });
    $("modify-dialog").close(); await updateAfterMutation(order, "Replacement processed — check broker status");
  });
});

async function boot() {
  updateControls();
  try {
    const response = await fetch("/ui/session", { headers: { "X-Local-UI": "1" }, credentials: "same-origin" });
    if (!response.ok) throw new Error("Open this dashboard at http://127.0.0.1:8000 on the same machine as the Python server.");
    state.csrf = (await response.json()).csrf_token;
    await run(async () => {
      const watchlist = await api("/v1/watchlist");
      state.watchlist = watchlist.symbols.length ? watchlist.symbols : ["AAPL", "MSFT"];
      $("watchlist-symbols").value = state.watchlist.join(", ");
      const risk = await api("/v1/risk");
      renderList($("risk-limits"), Object.fromEntries(Object.entries(risk.limits).map(([key, value]) => [key.replaceAll("_", " "), value])));
      await refreshAll();
    });
  } catch (error) { notify(error.message, "error"); }
  updateControls();
}
boot();

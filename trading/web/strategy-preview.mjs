// Display-only arithmetic. Never authorizes or executes a trading action.
const SYMBOL = /^[A-Z][A-Z0-9.\-]{0,19}$/;
const value = input => input == null || String(input).trim() === "" || !Number.isFinite(Number(input)) ? null : Number(input);
const rounded = input => Math.round((input + Number.EPSILON) * 1e6) / 1e6;

export function normalizedSymbols(a, b) {
  const pair = [a, b].map(input => String(input || "").trim().toUpperCase());
  return {pair, symbols: [...new Set(pair.filter(symbol => SYMBOL.test(symbol)))],
    complete: pair.every(symbol => SYMBOL.test(symbol)) && pair[0] !== pair[1]};
}

export function calculateStrategyPreview(input) {
  const {pair, complete} = normalizedSymbols(input.symbolA, input.symbolB);
  const warnings = [], rows = [];
  for (const symbol of pair) {
    const matches = (input.quotes || []).filter(quote => quote.symbol === symbol);
    const quote = matches.length === 1 ? matches[0] : null;
    const ask = value(quote?.ask), bid = value(quote?.bid), last = value(quote?.price);
    const stamp = value(quote?.quote_time), age = stamp == null ? null : ((input.now ?? Date.now()) - stamp) / 1000;
    const valid = ask != null && bid != null && ask > 0 && bid > 0 && ask >= bid;
    const fresh = valid && age != null && age >= -5 && age <= 15 && quote?.trade_status === "RTH";
    rows.push({symbol, ask, bid, last, age, valid, fresh, market: quote?.trade_status || "unknown", stamp});
  }
  if (!complete) warnings.push("Enter two distinct valid stock symbols.");
  for (const row of rows) if (row.symbol && !row.fresh) warnings.push(`${row.symbol}: missing, stale, crossed, or non-regular-hours quote. Display is not a tradable signal.`);
  const coherent = rows.every(row => row.stamp != null) && Math.abs(rows[0].stamp - rows[1].stamp) <= 2000;
  if (complete && rows.every(row => row.valid) && !coherent) warnings.push("Quote timestamps differ by more than two seconds.");
  const combined = complete && rows.every(row => row.valid);
  const askSum = combined ? rounded(rows[0].ask + rows[1].ask) : null;
  const bidSum = combined ? rounded(rows[0].bid + rows[1].bid) : null;
  const qty = value(input.quantity), validQty = qty != null && Number.isInteger(qty) && qty > 0;
  if (!validQty) warnings.push("Shares each must be a positive whole number.");
  const z = value(input.z), a = value(input.profit), b = value(input.downside);
  const target = z > 0 && a > 0 ? rounded(z + a) : null;
  const stop = z > 0 && b > 0 && b < z ? rounded(z - b) : null;
  if (b != null && z != null && b >= z) warnings.push("Downside delta b must be smaller than entry sum Z.");
  const fee = value(input.feeBuffer) ?? 0.01;
  const cost = askSum != null && validQty ? rounded(askSum * qty) : null;
  const bufferedCost = cost != null ? rounded(cost * (1 + fee)) : null;
  const budget = value(input.budget), maxQty = value(input.limits?.max_order_quantity), maxLeg = value(input.limits?.max_order_notional);
  if (bufferedCost != null && budget > 0 && bufferedCost > budget) warnings.push("Current pair entry estimate plus fee buffer exceeds the pair budget.");
  if (validQty && maxQty > 0 && qty > maxQty) warnings.push("Shares each exceed the selected profile's per-order quantity limit.");
  if (validQty && maxLeg > 0 && rows.some(row => row.ask != null && row.ask * qty > maxLeg)) warnings.push("A current leg notional exceeds the selected profile's per-order limit.");
  const cycles = value(input.cycles), daily = value(input.limits?.max_daily_orders);
  if (cycles > 0 && daily > 0 && cycles * 4 > daily) warnings.push(`${cycles} full cycles normally need ${cycles * 4} submissions; the profile's global daily cap is ${daily}.`);
  return {rows, complete, fresh: combined && coherent && rows.every(row => row.fresh), quantity: validQty ? qty : null,
    askSum, bidSum, cost, bufferedCost, liquidationValue: bidSum != null && validQty ? rounded(bidSum * qty) : null,
    spreadCost: combined && validQty ? rounded((askSum - bidSum) * qty) : null,
    target, stop, entryGap: askSum != null && z > 0 ? rounded(askSum - z) : null,
    targetGap: bidSum != null && target != null ? rounded(target - bidSum) : null,
    downsideGap: bidSum != null && stop != null ? rounded(bidSum - stop) : null,
    grossTargetDifference: validQty && a > 0 ? rounded(qty * a) : null,
    grossDownsideDifference: validQty && b > 0 ? rounded(qty * b) : null, warnings};
}

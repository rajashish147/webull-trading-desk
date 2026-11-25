import test from "node:test";
import assert from "node:assert/strict";
import {calculateStrategyPreview, normalizedSymbols} from "../trading/web/strategy-preview.mjs";

const now = 200000;
const rows = [
  {symbol: "AAPL", price: "100", ask: "101", bid: "100", quote_time: now, trade_status: "RTH"},
  {symbol: "MSFT", price: "49", ask: "50", bid: "49", quote_time: now, trade_status: "RTH"},
];
const input = {symbolA: "AAPL", symbolB: "MSFT", quantity: 2, z: "150", profit: "5", downside: "10", budget: "500", quotes: rows, now, feeBuffer: "0.01", cycles: 1};

test("symbols normalize and same/invalid pair is not combined", () => {
  assert.deepEqual(normalizedSymbols(" aapl ", "msft").pair, ["AAPL", "MSFT"]);
  assert.equal(normalizedSymbols("AAPL", "AAPL").complete, false);
  assert.equal(calculateStrategyPreview({...input, symbolB: "AAPL"}).askSum, null);
});
test("combined ask/bid sums are per share, costs include quantity on both legs", () => {
  const result = calculateStrategyPreview(input);
  assert.equal(result.askSum, 151); assert.equal(result.bidSum, 149);
  assert.equal(result.cost, 302); assert.equal(result.bufferedCost, 305.02);
  assert.equal(result.liquidationValue, 298); assert.equal(result.spreadCost, 4);
});
test("changing shares recalculates without changing quote sums", () => {
  const result = calculateStrategyPreview({...input, quantity: 3});
  assert.equal(result.askSum, 151); assert.equal(result.cost, 453);
  assert.equal(result.bufferedCost, 457.53);
});
test("targets and gaps follow Z+a and Z-b", () => {
  const result = calculateStrategyPreview(input);
  assert.equal(result.target, 155); assert.equal(result.stop, 140);
  assert.equal(result.entryGap, 1); assert.equal(result.targetGap, 6); assert.equal(result.downsideGap, 9);
  assert.equal(result.grossTargetDifference, 10); assert.equal(result.grossDownsideDifference, 20);
});
test("only one quoted stock is displayed but no combined estimate is invented", () => {
  const result = calculateStrategyPreview({...input, quotes: rows.slice(0, 1)});
  assert.equal(result.rows[0].ask, 101); assert.equal(result.askSum, null); assert.equal(result.cost, null);
});
test("duplicate response symbols do not create a valid quote pair", () => {
  assert.equal(calculateStrategyPreview({...input, quotes: [...rows, rows[0]]}).fresh, false);
});
test("freshness and timestamp coherence are checked", () => {
  assert.equal(calculateStrategyPreview(input).fresh, true);
  assert.equal(calculateStrategyPreview({...input, now: now + 16000}).fresh, false);
  assert.equal(calculateStrategyPreview({...input, quotes: [rows[0], {...rows[1], quote_time: now - 3000}]}).fresh, false);
});
test("closed markets and crossed/invalid prices are not tradable previews", () => {
  assert.equal(calculateStrategyPreview({...input, quotes: [rows[0], {...rows[1], trade_status: "CLOSED"}]}).fresh, false);
  assert.equal(calculateStrategyPreview({...input, quotes: [rows[0], {...rows[1], ask: "48"}]}).askSum, null);
  assert.equal(calculateStrategyPreview({...input, quotes: [rows[0], {...rows[1], ask: "NaN"}]}).cost, null);
});
test("zero/fractional/blank shares never produce a position estimate", () => {
  for (const quantity of [0, -1, "", 1.5, "NaN"]) assert.equal(calculateStrategyPreview({...input, quantity}).cost, null);
});
test("invalid downside and missing targets warn without fabricating levels", () => {
  assert.equal(calculateStrategyPreview({...input, downside: "150"}).stop, null);
  assert.equal(calculateStrategyPreview({...input, z: ""}).target, null);
});
test("budget and per-order caps warn without acting", () => {
  const result = calculateStrategyPreview({...input, budget: "100", limits: {max_order_quantity: "1", max_order_notional: "150", max_daily_orders: "3"}, cycles: 2});
  assert.ok(result.warnings.some(message => message.includes("pair budget")));
  assert.ok(result.warnings.some(message => message.includes("quantity limit")));
  assert.ok(result.warnings.some(message => message.includes("per-order limit")));
  assert.ok(result.warnings.some(message => message.includes("8 submissions")));
});
test("small decimal sums avoid obvious binary float display errors", () => {
  const quotes = [{...rows[0], ask: "0.1", bid: "0.1"}, {...rows[1], ask: "0.2", bid: "0.2"}];
  assert.equal(calculateStrategyPreview({...input, quotes, quantity: 3}).askSum, 0.3);
  assert.equal(calculateStrategyPreview({...input, quotes, quantity: 3}).cost, 0.9);
});

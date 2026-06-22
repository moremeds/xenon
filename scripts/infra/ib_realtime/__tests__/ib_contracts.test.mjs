import { test } from "node:test";
import assert from "node:assert/strict";
import { SecType } from "@stoqey/ib";
import { forexContract, stockContract } from "../ib_contracts.js";

test("forexContract builds an IDEALPRO CASH contract", () => {
  const c = forexContract("USD", "JPY");
  assert.equal(c.symbol, "USD");
  assert.equal(c.currency, "JPY");
  assert.equal(c.exchange, "IDEALPRO");
  assert.equal(c.secType, SecType.CASH);
});

test("stockContract carries non-default exchange + currency", () => {
  // 5016 JX Advanced Metals on TSEJ in JPY (real listing).
  const c = stockContract("5016", "TSEJ", "JPY");
  assert.equal(c.exchange, "TSEJ");
  assert.equal(c.currency, "JPY");
  assert.equal(c.secType, SecType.STK);
});

export type ModifyComboLeg = {
  action: "BUY" | "SELL";
  expiry: string;
  strike: number;
  right: "C" | "P";
  ratio: number;
  limitPrice?: number;
};

export type ReplaceComboOrder = {
  type: "combo";
  symbol: string;
  action: "BUY" | "SELL";
  quantity: number;
  limitPrice: number;
  tif?: "DAY" | "GTC";
  legs: ModifyComboLeg[];
  /** Idempotency key for the replacement place. OrderActionsContext sets it:
   * one key per logical replacement of one target order. */
  client_attempt_id?: string;
};

export type ModifyCancelTarget = {
  orderId?: number;
  permId?: number;
};

export type ModifyOrderRequest = {
  newPrice?: number;
  newQuantity?: number;
  outsideRth?: boolean;
  cancelOrders?: ModifyCancelTarget[];
  replaceOrder?: ReplaceComboOrder;
  modifySequence?: number;
};

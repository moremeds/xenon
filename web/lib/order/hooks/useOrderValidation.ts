/**
 * useOrderValidation — Client-side order validation
 *
 * Validates quantity and price inputs before submission.
 */

import { useMemo } from "react";
import type { OrderValidation } from "../types";

interface UseOrderValidationOptions {
  quantity: string;
  limitPrice: string;
  minQuantity?: number;
  minPrice?: number;
}

/**
 * Validate order inputs client-side.
 * Returns parsed values and any validation errors.
 */
export function useOrderValidation({
  quantity,
  limitPrice,
  minQuantity = 1,
  minPrice = 0.01,
}: UseOrderValidationOptions): OrderValidation {
  return useMemo(() => {
    const errors: OrderValidation["errors"] = {};

    const parsedQuantity = parseInt(quantity, 10);
    const parsedPrice = parseFloat(limitPrice);

    // Quantity validation
    if (quantity === "" || quantity === undefined) {
      errors.quantity = "Quantity is required";
    } else if (isNaN(parsedQuantity)) {
      errors.quantity = "Quantity must be a number";
    } else if (!Number.isFinite(parsedQuantity)) {
      errors.quantity = "Quantity must be finite";
    } else if (parsedQuantity < minQuantity) {
      errors.quantity = `Quantity must be at least ${minQuantity}`;
    } else if (!Number.isInteger(parsedQuantity)) {
      errors.quantity = "Quantity must be a whole number";
    }

    // Price validation
    if (limitPrice === "" || limitPrice === undefined) {
      errors.price = "Price is required";
    } else if (isNaN(parsedPrice)) {
      errors.price = "Price must be a number";
    } else if (!Number.isFinite(parsedPrice)) {
      errors.price = "Price must be finite";
    } else if (parsedPrice < minPrice) {
      errors.price = `Price must be at least $${minPrice.toFixed(2)}`;
    }

    const isValid = Object.keys(errors).length === 0;

    return {
      isValid,
      errors,
      parsedQuantity: isNaN(parsedQuantity) ? 0 : parsedQuantity,
      parsedPrice: isNaN(parsedPrice) ? 0 : parsedPrice,
    };
  }, [quantity, limitPrice, minQuantity, minPrice]);
}

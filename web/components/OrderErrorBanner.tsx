"use client";

import { formatOrderError } from "@/lib/orderError";

type OrderErrorBannerProps = {
  error: string | null;
};

export default function OrderErrorBanner({ error }: OrderErrorBannerProps) {
  if (!error) return null;

  const formatted = formatOrderError(error);

  return (
    <div className="order-error" role="alert">
      <div className="order-error-summary">{formatted.summary}</div>
      {formatted.details.map((detail) => (
        <div key={detail} className="order-error-detail">
          {detail}
        </div>
      ))}
    </div>
  );
}

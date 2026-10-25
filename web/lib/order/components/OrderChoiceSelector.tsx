"use client";

/**
 * OrderChoiceSelector — one-of button group for a server-supplied option list
 * (Futu order type / TIF / session). Same look as OrderTifSelector.
 */

interface OrderChoiceSelectorProps<T extends string> {
  options: readonly T[];
  value: T | null;
  onChange: (value: T) => void;
  ariaLabel: string;
  /** Visible button text per option; defaults to the option itself. */
  labels?: Partial<Record<T, string>>;
  disabled?: boolean;
  className?: string;
}

export function OrderChoiceSelector<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
  labels,
  disabled = false,
  className = "",
}: OrderChoiceSelectorProps<T>) {
  return (
    <div
      className={`order-tif-selector order-choice-selector ${className}`.trim()}
      role="group"
      aria-label={ariaLabel}
    >
      {options.map((option) => {
        const active = option === value;
        return (
          <button
            key={option}
            type="button"
            className={`order-action-btn order-tif-btn ${active ? "order-action-active order-tif-active" : ""}`}
            aria-pressed={active}
            disabled={disabled}
            onClick={() => onChange(option)}
          >
            {labels?.[option] ?? option}
          </button>
        );
      })}
    </div>
  );
}

// GET /admin/settings row (FastAPI src/xenon/runtime_settings.py::describe).
// The registry lives only in Python; the UI renders from this metadata.

export type SettingCategory = "toggle" | "tuning" | "safety";
export type SettingType = "bool" | "int" | "number" | "enum" | "set" | "object";

export interface SettingChange {
  changed_at: string | null;
  changed_by: string | null;
  old_value: unknown;
  new_value: unknown;
  note: string | null;
}

export interface RuntimeSetting {
  key: string;
  env_name: string | null;
  owner: "api" | "relay" | "futu-quotes";
  category: SettingCategory;
  type: SettingType;
  apply: "live" | "restart";
  rule: "free" | "subset" | "min" | "and";
  narrow_only: boolean;
  confirm_rule: string | null;
  description: string;
  consumers: string[];
  choices: string[];
  bounds: [number, number] | null;
  default: unknown;
  env_known: boolean;
  env_value: unknown;
  db_value: unknown;
  db_value_invalid: boolean;
  effective: unknown;
  updated_at: string | null;
  updated_by: string | null;
  note: string | null;
  last_change: SettingChange | null;
}

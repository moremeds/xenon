import { NextResponse } from "next/server";
import { readFile } from "fs/promises";
import { join } from "path";

export const runtime = "nodejs";

const TRADE_LOG_PATH = join(process.cwd(), "..", "data", "trade_log.json");

export async function GET(): Promise<Response> {
  try {
    const raw = await readFile(TRADE_LOG_PATH, "utf-8");
    const data = JSON.parse(raw);
    return NextResponse.json(data);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Failed to read trade log";
    return NextResponse.json({ error: message, trades: [] }, { status: 500 });
  }
}

/**
 * Centralized Clerk auth mock for Vitest.
 *
 * Usage: import { setupClerkMock } from "./helpers";
 * Then call vi.mock("@clerk/nextjs/server", () => setupClerkMock());
 */
import { vi } from "vitest";

export function setupClerkMock(token = "test-token") {
  return {
    auth: vi.fn(async () => ({
      getToken: async () => token,
      userId: "user_test123",
    })),
    currentUser: vi.fn(async () => ({ id: "user_test123" })),
  };
}

export function setupClerkClientMock() {
  return {
    useUser: vi.fn(() => ({ user: { id: "user_test123" }, isLoaded: true })),
    useAuth: vi.fn(() => ({
      getToken: async () => "test-token",
      isLoaded: true,
    })),
    ClerkProvider: ({ children }: { children: React.ReactNode }) => children,
  };
}

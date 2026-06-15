/** @vitest-environment jsdom */
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { IbPoolRoles } from "@/components/operator/IbPoolRoles";

afterEach(() => cleanup());

describe("IbPoolRoles", () => {
  it("renders each role with a status dot", () => {
    const { container } = render(
      <IbPoolRoles
        pool={{
          sync: { connected: true, client_id: 11 },
          orders: { connected: false },
        }}
      />,
    );
    expect(screen.getByText("sync")).toBeTruthy();
    expect(screen.getByText("orders")).toBeTruthy();
    expect(container.querySelectorAll(".operator-pool__dot").length).toBe(2);
  });
});

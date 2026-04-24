// @vitest-environment jsdom
import { describe, expect, it, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

import WizardModal from "@/components/ticker-detail/WizardModal";
import WizardSessionStrip from "@/components/ticker-detail/WizardSessionStrip";
import type { UseWizardSessionResult } from "@/lib/useWizardSession";

afterEach(() => {
  cleanup();
});

const sessionActive: UseWizardSessionResult = {
  session: {
    session_id: "wiz-1",
    state: "WORKING",
    structure_name: "Bull Call Spread",
  },
  loading: false,
  error: null,
};

const sessionEmpty: UseWizardSessionResult = {
  session: null,
  loading: false,
  error: null,
};

describe("WizardModal", () => {
  it("renders as a modal dialog when open", () => {
    render(
      <WizardModal
        open={true}
        sessionId="wiz-1"
        ticker="AAPL"
        session={sessionActive}
        onClose={() => {}}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: /combo wizard/i });
    expect(dialog).toBeTruthy();
    expect(dialog.getAttribute("aria-modal")).toBe("true");
  });

  it("does not render a drawer landmark", () => {
    render(
      <WizardModal
        open={true}
        sessionId="wiz-1"
        ticker="AAPL"
        session={sessionActive}
        onClose={() => {}}
      />,
    );
    expect(screen.queryByRole("complementary", { name: /drawer/i })).toBeNull();
  });

  it("does not render when closed", () => {
    render(
      <WizardModal
        open={false}
        sessionId="wiz-1"
        ticker="AAPL"
        session={sessionActive}
        onClose={() => {}}
      />,
    );
    expect(screen.queryByRole("dialog", { name: /combo wizard/i })).toBeNull();
  });
});

describe("WizardSessionStrip", () => {
  it("renders strip text and resume button when a session is active", () => {
    render(
      <WizardSessionStrip
        sessionId="wiz-1"
        session={sessionActive}
        onResume={() => {}}
      />,
    );
    expect(screen.getByText(/wizard session/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /resume wizard/i })).toBeTruthy();
  });

  it("renders nothing when no session is active", () => {
    const { container } = render(
      <WizardSessionStrip
        sessionId={null}
        session={sessionEmpty}
        onResume={() => {}}
      />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByText(/wizard session/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /resume wizard/i })).toBeNull();
  });
});

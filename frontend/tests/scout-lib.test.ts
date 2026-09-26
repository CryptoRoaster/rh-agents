import { describe, expect, it } from "vitest";
import {
  VERY_YOUNG_SECONDS,
  backlogLabel,
  filterQuery,
  formatAge,
  formatRate,
  formatUsd,
} from "@/lib/scout";
import { cockpitTarget } from "@/lib/cockpit-proxy";

describe("presentation helpers", () => {
  it("shows young ages compactly", () => {
    expect(formatAge(180)).toBe("3m");
    expect(formatAge(42 * 60)).toBe("42m");
    expect(formatAge(2 * 3600 + 15 * 60)).toBe("2h 15m");
    expect(formatAge(26 * 3600)).toBe("1d 2h");
    expect(formatAge(null)).toBe("—");
  });

  it("never turns a missing rate into zero", () => {
    expect(formatRate(null)).toBe("N/A");
    expect(formatRate(0.5)).toBe("50%");
    expect(formatRate(1)).toBe("100%");
  });

  it("keeps unavailable measurements visible as unavailable", () => {
    expect(formatUsd(null, "UNKNOWN")).toBe("unknown");
    expect(formatUsd("0", "AVAILABLE")).toBe("$0");
    expect(formatUsd("1234.5")).toBe("$1.2k");
  });

  it("states the ORBIT backlog plainly", () => {
    expect(backlogLabel(0, null)).toBe("ORBIT queue caught up");
    expect(backlogLabel(3, 5400)).toBe(
      "3 ORBIT reviews pending · oldest due 1h 30m",
    );
  });

  it("defines very young deterministically as under six hours", () => {
    expect(VERY_YOUNG_SECONDS).toBe(21600);
    expect(filterQuery("VERY_YOUNG")).toEqual({ max_age_seconds: "21600" });
    expect(filterQuery("PROMOTABLE")).toEqual({ status: "PROMOTABLE" });
    expect(filterQuery("ALL")).toEqual({});
  });
});

describe("cockpit proxy allowlist", () => {
  const base = "http://127.0.0.1:8000";
  const id = "0f6b1e2a-3c4d-4e5f-8a9b-0c1d2e3f4a5b";

  it("forwards only read paths the cockpit uses", () => {
    expect(
      cockpitTarget(["scout", "watches"], new URLSearchParams("limit=5"), base)
        ?.href,
    ).toBe(`${base}/api/scout/watches?limit=5`);
    expect(
      cockpitTarget(
        ["scout", "watches", id, "assessments"],
        new URLSearchParams(),
        base,
      ),
    ).not.toBeNull();
    expect(
      cockpitTarget(["paper", "portfolio"], new URLSearchParams(), base),
    ).not.toBeNull();
    expect(
      cockpitTarget(["trade-cases", id], new URLSearchParams(), base),
    ).not.toBeNull();
  });

  it("refuses anything else", () => {
    for (const path of [
      ["system"],
      ["trade-cases"],
      ["scout", "watches", "not-a-uuid"],
      ["scout", "..", "system"],
      ["markets"],
      ["runtime"],
    ]) {
      expect(cockpitTarget(path, new URLSearchParams(), base)).toBeNull();
    }
  });

  it("drops unknown query keys", () => {
    const url = cockpitTarget(
      ["scout", "watches"],
      new URLSearchParams("status=WATCHING&token=abc&debug=1"),
      base,
    );
    expect(url?.search).toBe("?status=WATCHING");
  });

  it("refuses a base URL carrying credentials", () => {
    expect(
      cockpitTarget(
        ["scout", "runs"],
        new URLSearchParams(),
        "http://u:p@host",
      ),
    ).toBeNull();
  });
});

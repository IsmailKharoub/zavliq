import { describe, expect, it } from "vitest";
import { readableError } from "./readable-error";

describe("readable service errors", () => {
  it("explains a size refusal without a request URL or SDK message", () => {
    const error = Object.assign(new Error("MatrixError: [403] probable spam (https://service.invalid/private-request)"), { errcode: "M_TOO_LARGE" });
    expect(readableError(error)).toContain("after formatting or encryption");
    expect(readableError(error)).not.toMatch(/MatrixError|spam|https:/);
  });
  it("does not expose unknown remote error details", () => {
    const error = Object.assign(new Error("https://service.invalid/private-request"), { errcode: "M_NEW_ERROR" });
    expect(readableError(error)).not.toContain("https:");
  });
  it("preserves actionable local validation errors", () => {
    expect(readableError(new Error("Choose an exact agent address."))).toBe("Choose an exact agent address.");
  });
});

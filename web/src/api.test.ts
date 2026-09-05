import { beforeEach, describe, expect, it } from "vitest";
import { authHeaders, feedbackAttachmentUrl, reviewPageUrl, setSessionToken } from "./api";

beforeEach(() => {
  localStorage.clear();
});

describe("reviewPageUrl", () => {
  it("url-encodes the doc id and appends the page number", () => {
    const url = reviewPageUrl("doc a/b", 3);
    expect(url).toContain(`/review/page/${encodeURIComponent("doc a/b")}/3`);
  });
});

describe("feedbackAttachmentUrl", () => {
  it("url-encodes the attachment name", () => {
    const url = feedbackAttachmentUrl("shot #1 (final).png");
    expect(url).toContain(`/feedback/admin/attachment/${encodeURIComponent("shot #1 (final).png")}`);
  });
});

describe("authHeaders", () => {
  it("omits X-User-Token when no session is stored", () => {
    setSessionToken(null);
    expect(authHeaders()["X-User-Token"]).toBeUndefined();
  });

  it("attaches the stored session token as X-User-Token", () => {
    setSessionToken("tok-123");
    expect(authHeaders()["X-User-Token"]).toBe("tok-123");
  });
});

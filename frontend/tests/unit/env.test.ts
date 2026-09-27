import { describe, expect, it } from "vitest";

import { parseServerEnv } from "../../lib/env";

describe("parseServerEnv", () => {
  it("accepts an http backend URL and strips the trailing slash", () => {
    expect(parseServerEnv({ BACKEND_INTERNAL_URL: "http://backend:8000/" })).toEqual({
      BACKEND_INTERNAL_URL: "http://backend:8000",
    });
  });

  it("fails fast when BACKEND_INTERNAL_URL is missing", () => {
    expect(() => parseServerEnv({})).toThrow(/BACKEND_INTERNAL_URL/);
  });

  it("rejects non-http schemes", () => {
    expect(() => parseServerEnv({ BACKEND_INTERNAL_URL: "ftp://backend" })).toThrow(
      /BACKEND_INTERNAL_URL/,
    );
  });
});

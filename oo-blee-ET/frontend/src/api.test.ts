import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, explainError } from "./api";

const reply = (status: number, body: unknown) => vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status })));
afterEach(() => vi.unstubAllGlobals());

describe("api client", () => {
  it("returns parsed JSON and posts bodies as JSON", async () => {
    const f = vi.fn(async () => new Response(JSON.stringify({ ok: 1 }), { status: 200 }));
    vi.stubGlobal("fetch", f);
    await api.preview("acme", "user_ravi", { principal_id: "user_ravi", role: "data_subject" });
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/erasure/preview");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ tenant: "acme", user_id: "user_ravi", acting_as: { principal_id: "user_ravi", role: "data_subject" } });
  });

  it("turns the API's structured errors into ApiError with details", async () => {
    reply(409, { error: { code: "PREVIEW_STALE", message: "changed", details: { new_preview: { preview_version: "abc" } } } });
    const e = await api.graph("acme").catch((x) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect(e).toMatchObject({ status: 409, code: "PREVIEW_STALE", details: { new_preview: { preview_version: "abc" } } });
  });

  it("handles FastAPI validation errors and unreachable servers", async () => {
    reply(422, { detail: [{ msg: "bad" }] });
    expect(await api.config().catch((x) => x)).toMatchObject({ code: "REQUEST_INVALID", status: 422 });
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("failed"); }));
    expect(await api.config().catch((x) => x)).toMatchObject({ code: "NETWORK", status: 0 });
  });

  it("explains the errors a person can act on in plain words", () => {
    expect(explainError(new ApiError(403, "FORBIDDEN_ROLE", "x"))).toMatch(/preview an erasure but not perform/);
    expect(explainError(new ApiError(403, "FORBIDDEN_SUBJECT", "x"))).toMatch(/own data/);
    expect(explainError(new ApiError(409, "PREVIEW_STALE", "x"))).toMatch(/new preview/);
    expect(explainError(new ApiError(400, "OTHER", "raw message"))).toBe("raw message");
    expect(explainError(new Error("boom"))).toBe("Something went wrong.");
  });
});

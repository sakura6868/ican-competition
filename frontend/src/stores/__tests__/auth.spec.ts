import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

import { useAuthStore } from "@/stores/auth";
import { ApiError } from "@/api/client";

function mockFetchOnce(status: number, body: unknown) {
  globalThis.fetch = vi.fn().mockResolvedValue(
    new Response(status === 204 ? null : JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

describe("auth store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("login sets the user", async () => {
    mockFetchOnce(200, { id: "u1", username: "inst1", display_name: "王老师", role: "instructor" });
    const auth = useAuthStore();
    const user = await auth.login("inst1", "correct-horse-42");
    expect(user.username).toBe("inst1");
    expect(auth.isAuthenticated).toBe(true);
    expect(auth.isWriter).toBe(true);
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/v1/auth/login",
      expect.objectContaining({ method: "POST", credentials: "same-origin" }),
    );
  });

  it("login failure throws ApiError and stays unauthenticated", async () => {
    mockFetchOnce(401, { detail: "用户名或密码错误" });
    const auth = useAuthStore();
    await expect(auth.login("inst1", "bad")).rejects.toBeInstanceOf(ApiError);
    expect(auth.isAuthenticated).toBe(false);
  });

  it("fetchMe restores session from cookie", async () => {
    mockFetchOnce(200, { id: "u2", username: "stu1", display_name: "", role: "learner" });
    const auth = useAuthStore();
    expect(await auth.fetchMe()).toBe(true);
    expect(auth.isAuthenticated).toBe(true);
    expect(auth.isWriter).toBe(false);
  });

  it("fetchMe returns false and clears user on 401", async () => {
    mockFetchOnce(401, { detail: "未登录" });
    const auth = useAuthStore();
    expect(await auth.fetchMe()).toBe(false);
    expect(auth.isAuthenticated).toBe(false);
  });

  it("logout clears user", async () => {
    mockFetchOnce(204, null);
    const auth = useAuthStore();
    await auth.logout();
    expect(auth.isAuthenticated).toBe(false);
  });
});

// API 客户端：同源 /api 前缀（Vite 代理到后端 8011），携带会话 Cookie。

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  isForm = false,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined && !isForm) {
    headers["Content-Type"] = "application/json";
  }
  // CSRF 双提交令牌：读不到时先向 /auth/csrf 刷新（旧会话/刷新页后自动恢复）
  if (method !== "GET") {
    const csrf = await ensureCsrfToken();
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  const resp = await fetch(`/api/v1${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: isForm ? (body as FormData) : body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 204) {
    return undefined as T;
  }
  let data: unknown = null;
  try {
    data = await resp.json();
  } catch {
    data = null;
  }
  if (!resp.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : resp.statusText;
    throw new ApiError(resp.status, detail);
  }
  // 离线可用性：SOP 列表读取成功后自动缓存（断网时本地考试可复用）
  if (method === "GET" && path === "/sops") {
    try {
      localStorage.setItem("gx_sop_cache_v1", JSON.stringify(data));
    } catch {
      // 存储失败不影响在线流程
    }
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  postForm: <T>(path: string, form: FormData) => request<T>("POST", path, form, true),
};

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

let csrfRefreshing: Promise<string | null> | null = null;

async function ensureCsrfToken(): Promise<string | null> {
  const existing = readCookie("gx_csrf");
  if (existing) return existing;
  // 旧会话/刷新后可能没有 CSRF cookie：并发写请求共享一次刷新
  if (!csrfRefreshing) {
    csrfRefreshing = fetch("/api/v1/auth/csrf", {
      method: "GET",
      credentials: "same-origin",
    })
      .then(() => readCookie("gx_csrf"))
      .catch(() => null)
      .finally(() => {
        csrfRefreshing = null;
      });
  }
  return csrfRefreshing;
}

// static/js/api.js — điểm duy nhất gọi API (ES module, cùng origin với API)
// Hằng số khớp code Phase 1: app/core/config.py (csrf_cookie_name, csrf_header_name)
// và SAFE_METHODS trong app/modules/auth/dependencies.py
const CSRF_COOKIE_NAME = "avh_csrf";   // giữ nguyên tên cả khi COOKIE_SECURE=true (không có tiền tố __Host-)
const CSRF_HEADER_NAME = "X-CSRF-Token";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const FALLBACK_ERROR_MESSAGE = "Đã có lỗi xảy ra, vui lòng thử lại sau";
const API_PATH_PREFIX = "/api/v1/";
// Base giả (TLD .invalid không bao giờ tồn tại): nếu input tự chỉ định authority ("//host", "/\host",
// tab/xuống dòng chèn giữa…) thì host sau khi parse sẽ khác host này
const PATH_ONLY_BASE = "http://relative-path.invalid";
const PATH_ONLY_HOST = new URL(PATH_ONLY_BASE).host;

export class ApiError extends Error {
  constructor(message, { status, code, details = [], retryAfterSeconds = null, cause } = {}) {
    super(message, { cause });
    this.name = "ApiError";
    this.status = status;                       // 0 = không kết nối được máy chủ
    this.code = code;                           // error.code của envelope, vd "csrf_failed", "rate_limit_exceeded"
    this.details = details;                     // [{ field, message, code }] khi 422
    this.retryAfterSeconds = retryAfterSeconds; // từ header Retry-After (429), null nếu không có
  }
}

// Đọc cookie ở MỖI request, không cache, không lưu localStorage: đăng nhập lại sẽ cấp token mới
function readCsrfToken() {
  const prefix = `${CSRF_COOKIE_NAME}=`;
  const entry = document.cookie.split("; ").find((part) => part.startsWith(prefix));
  return entry === undefined ? null : entry.slice(prefix.length);
}

function buildHeaders(method, body) {
  const headers = { Accept: "application/json" };
  if (body !== undefined && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json"; // FormData: để trình duyệt tự đặt boundary
  }
  if (!SAFE_METHODS.has(method)) {
    const csrfToken = readCsrfToken();
    if (csrfToken !== null) headers[CSRF_HEADER_NAME] = csrfToken; // chỉ qua header, không bao giờ qua URL
  }
  return headers;
}

async function readJsonBody(response) {
  const contentType = response.headers.get("Content-Type") ?? "";
  if (response.status === 204 || !contentType.includes("application/json")) return null;
  return response.json().catch(() => null);
}

function parseRetryAfterSeconds(response) {
  const seconds = Number.parseInt(response.headers.get("Retry-After") ?? "", 10);
  return Number.isFinite(seconds) ? seconds : null;
}

// Trả URL tuyệt đối đã kiểm tra của một endpoint API cùng origin; ném TypeError cho mọi thứ khác.
// Kiểm tra trên kết quả của URL parser (chuẩn WHATWG, giống trình duyệt) chứ không trên chuỗi thô,
// vì parser coi "\" như "/", bỏ tab/xuống dòng, và rút gọn ".." / "%2e%2e" trước khi gửi request.
function resolveApiUrl(path) {
  if (typeof path !== "string" || !path.startsWith("/")) {
    throw new TypeError("apiRequest chỉ nhận đường dẫn /api/v1/..., không nhận URL tuyệt đối");
  }
  let pathOnly;
  let url;
  try {
    pathOnly = new URL(path, PATH_ONLY_BASE);
    url = new URL(path, window.location.origin);
  } catch (error) {
    throw new TypeError("Đường dẫn API không hợp lệ", { cause: error });
  }
  const isPathOnly = pathOnly.host === PATH_ONLY_HOST;            // chặn "//host", "/\host" kể cả host trùng origin
  const isSameOrigin = url.origin === window.location.origin;
  const isApiPath = url.pathname.startsWith(API_PATH_PREFIX);     // pathname đã chuẩn hóa, chặn "/api/v1/../static"
  if (!isPathOnly || !isSameOrigin || !isApiPath) {
    throw new TypeError("apiRequest chỉ gọi endpoint cùng origin dưới /api/v1/");
  }
  return url;
}

export async function apiRequest(path, { method = "GET", body, signal } = {}) {
  // Kiểm tra TRƯỚC khi đọc cookie: CSRF token không bao giờ rời khỏi /api/v1/ cùng origin
  const url = resolveApiUrl(path);
  const httpMethod = method.toUpperCase();
  let response;
  try {
    // Gửi đúng URL tuyệt đối đã kiểm tra, không để fetch tự resolve lại theo <base href>
    response = await fetch(url.href, {
      method: httpMethod,
      signal,
      credentials: "same-origin",
      headers: buildHeaders(httpMethod, body),
      body: body === undefined || body instanceof FormData ? body : JSON.stringify(body),
    });
  } catch (error) {
    if (error.name === "AbortError") throw error; // người gọi chủ động hủy, không phải lỗi mạng
    throw new ApiError("Không kết nối được máy chủ, vui lòng kiểm tra mạng", {
      status: 0, code: "network_error", cause: error,
    });
  }
  const payload = await readJsonBody(response);
  if (!response.ok) {
    const envelope = payload?.error; // {"error": {"code", "message", "details"?}} — app/core/errors.py
    throw new ApiError(envelope?.message ?? FALLBACK_ERROR_MESSAGE, {
      status: response.status,
      code: envelope?.code ?? "unexpected_response", // vd 502 HTML từ proxy, không có envelope
      details: envelope?.details ?? [],
      retryAfterSeconds: parseRetryAfterSeconds(response),
    });
  }
  return payload;
}

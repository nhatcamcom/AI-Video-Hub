// static/js/session.js — trạng thái phiên trên header (GET /auth/me) và đăng xuất (POST /auth/logout)
import { ApiError, apiRequest } from "./api.js";

const CURRENT_USER_PATH = "/api/v1/auth/me";
const LOGOUT_PATH = "/api/v1/auth/logout";
const LOGGED_OUT_MESSAGE = "Bạn đã đăng xuất.";
const LOGOUT_FALLBACK_MESSAGE = "Chưa đăng xuất được. Vui lòng tải lại trang rồi thử lại.";
// Map (không dùng object thường) để mã lỗi lạ như "__proto__" không trỏ vào prototype
const LOGOUT_ERROR_MESSAGES = new Map([
  ["csrf_failed", "Phiên đăng nhập đã thay đổi (có thể ở tab khác). Vui lòng tải lại trang rồi thử lại."],
  ["network_error", "Không kết nối được máy chủ nên chưa đăng xuất được. Vui lòng thử lại."],
]);

/** Người dùng hiện tại, hoặc null khi chưa đăng nhập (401 là trạng thái bình thường, không phải lỗi). */
export async function fetchCurrentUser() {
  try {
    return await apiRequest(CURRENT_USER_PATH);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

function findHeaderElements() {
  const elements = {
    nav: document.getElementById("session-nav"),
    userName: document.getElementById("session-user-name"),
    logoutButton: document.getElementById("logout-button"),
    loginLink: document.getElementById("session-login-link"),
    message: document.getElementById("session-message"),
  };
  return Object.values(elements).every((element) => element !== null) ? elements : null;
}

/**
 * Gắn header phiên của trang. `onUserChange(user | null)` được gọi mỗi khi trạng thái phiên đổi.
 * Không tự gửi lại request khi gặp 401 / 403 csrf_failed: chỉ báo cho người dùng.
 */
export function createSessionHeader({ onUserChange = () => {} } = {}) {
  const header = findHeaderElements();
  if (header === null) throw new Error("Trang thiếu vùng trạng thái phiên trên header");
  let currentUser = null;
  let isLoggingOut = false;

  function showMessage(text) {
    // Vùng role="status" luôn có mặt (không ẩn) để trình đọc màn hình đọc được thông báo mới
    header.message.textContent = text;
  }

  function render(state) {
    const isFocusOnLogout = document.activeElement === header.logoutButton;
    header.nav.dataset.state = state;
    for (const view of header.nav.querySelectorAll("[data-session-view]")) {
      view.hidden = view.dataset.sessionView !== state;
    }
    // Luôn xóa tên khi không còn phiên: không giữ thông tin người dùng sau khi phiên bị thu hồi
    header.userName.textContent = state === "authenticated" ? currentUser.full_name : "";
    // Nút đăng xuất vừa bị ẩn khi đang giữ focus → chuyển focus sang link đăng nhập
    if (isFocusOnLogout && state === "anonymous") header.loginLink.focus();
  }

  function setUser(user) {
    currentUser = user;
    render(user === null ? "anonymous" : "authenticated");
    onUserChange(user);
  }

  async function refresh() {
    render("loading");
    try {
      setUser(await fetchCurrentUser());
    } catch {
      currentUser = null;
      render("error");
      onUserChange(null);
    }
  }

  async function logout() {
    if (isLoggingOut) return; // chặn bấm liên tục
    isLoggingOut = true;
    header.logoutButton.setAttribute("aria-disabled", "true");
    showMessage("");
    try {
      await apiRequest(LOGOUT_PATH, { method: "POST" });
      setUser(null);
      showMessage(LOGGED_OUT_MESSAGE);
    } catch (error) {
      const code = error instanceof ApiError ? error.code : null;
      showMessage(LOGOUT_ERROR_MESSAGES.get(code) ?? LOGOUT_FALLBACK_MESSAGE);
    } finally {
      isLoggingOut = false;
      header.logoutButton.removeAttribute("aria-disabled");
    }
  }

  header.logoutButton.addEventListener("click", logout);
  return { refresh, setUser, showMessage, getUser: () => currentUser };
}

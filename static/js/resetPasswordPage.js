// static/js/resetPasswordPage.js — trang /reset-password#token=...: POST /auth/reset-password
import { ApiError, apiRequest } from "./api.js";
import { createFormController } from "./authForm.js";
import { createSessionHeader } from "./session.js";

const RESET_PASSWORD_PATH = "/api/v1/auth/reset-password";
// Token do server sinh bằng secrets.token_urlsafe; độ dài khớp ResetPasswordRequest (20–128)
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{20,128}$/;
const BUSY_LABEL = "Đang đặt lại…";
const MISMATCH_MESSAGE = "Mật khẩu nhập lại không khớp.";
const CHECK_FIELDS_MESSAGE = "Vui lòng kiểm tra lại các ô được đánh dấu.";
const SUCCESS_MESSAGE =
  "Mật khẩu đã được đặt lại. Vì lý do an toàn, mọi phiên đăng nhập của tài khoản đã bị đăng xuất. Vui lòng đăng nhập lại bằng mật khẩu mới.";

/**
 * Lấy token từ fragment (#token=...) và XÓA fragment khỏi URL + lịch sử ngay lập tức.
 * Fragment không bao giờ được gửi lên server; token chỉ sống trong biến của module này.
 */
function takeTokenFromUrl() {
  const fragment = window.location.hash.slice(1);
  if (fragment === "") return null;
  window.history.replaceState(window.history.state, "", `${window.location.pathname}${window.location.search}`);
  const token = new URLSearchParams(fragment).get("token");
  return token !== null && TOKEN_PATTERN.test(token) ? token : null;
}

// Chạy ngay khi module được nạp, trước mọi request khác của trang
let resetToken = takeTokenFromUrl();

function initResetPasswordPage() {
  const form = document.getElementById("reset-password-form");
  const formSection = document.getElementById("reset-section");
  const invalidLinkNotice = document.getElementById("reset-invalid-link");
  const controller = createFormController(form);
  const session = createSessionHeader();

  function closeForm() {
    resetToken = null; // token đã dùng hoặc bị từ chối: không giữ lại
    form.reset();
    formSection.hidden = true;
  }

  // Không có token hợp lệ: không hiện form và KHÔNG gọi API đặt lại mật khẩu
  function showTokenState() {
    const hasToken = resetToken !== null;
    formSection.hidden = !hasToken;
    invalidLinkNotice.hidden = hasToken;
  }

  showTokenState();

  // Mở link trong email khi đang ở chính trang này: chỉ fragment đổi, trang KHÔNG tải lại, nên phải
  // tự đọc và xóa token mới. replaceState không phát "hashchange" → không lặp vô hạn.
  window.addEventListener("hashchange", () => {
    if (window.location.hash === "") return;
    resetToken = takeTokenFromUrl();
    form.reset();
    controller.hideStatus();
    showTokenState();
  });

  controller.onSubmit(async (formData) => {
    if (resetToken === null) return;
    const newPassword = String(formData.get("new_password") ?? "");
    if (newPassword !== String(formData.get("confirm_password") ?? "")) {
      controller.showStatus("error", CHECK_FIELDS_MESSAGE);
      controller.showFieldError("confirm_password", MISMATCH_MESSAGE)?.focus();
      return;
    }
    try {
      await apiRequest(RESET_PASSWORD_PATH, {
        method: "POST",
        body: { token: resetToken, new_password: newPassword },
      });
      closeForm();
      session.setUser(null); // server đã thu hồi mọi phiên, kể cả phiên của tab này
      controller.showStatus("success", SUCCESS_MESSAGE, { action: "login" });
    } catch (error) {
      if (error instanceof ApiError && error.code === "invalid_reset_token") {
        closeForm();
        controller.showApiError(error, { action: "request-new" });
        return;
      }
      controller.showApiError(error);
    }
  }, { busyLabel: BUSY_LABEL });

  session.refresh();
}

initResetPasswordPage();

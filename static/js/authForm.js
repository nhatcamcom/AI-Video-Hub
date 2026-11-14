// static/js/authForm.js — phần dùng chung của form xác thực: đang gửi, lỗi theo ô, thông báo
import { ApiError } from "./api.js";

const SECONDS_PER_MINUTE = 60;
const MAX_SECONDS_SHOWN_AS_SECONDS = 90;
const FALLBACK_ERROR_MESSAGE = "Đã có lỗi xảy ra. Vui lòng thử lại sau.";
const FALLBACK_FIELD_MESSAGE = "Giá trị không hợp lệ.";
const PASSWORD_FIELDS = new Set(["password", "new_password"]);

// Map (không dùng object thường) để mã lỗi lạ như "__proto__" không trỏ vào prototype
const ERROR_MESSAGES = new Map([
  ["invalid_credentials", "Email hoặc mật khẩu không đúng."],
  ["email_already_registered", "Email này đã được đăng ký. Hãy đăng nhập hoặc dùng chức năng quên mật khẩu."],
  ["invalid_reset_token", "Liên kết đặt lại mật khẩu không hợp lệ, đã hết hạn hoặc đã được dùng. Hãy yêu cầu liên kết mới."],
  ["csrf_failed", "Phiên làm việc đã thay đổi (có thể ở tab khác). Vui lòng tải lại trang rồi thử lại."],
  ["validation_error", "Vui lòng kiểm tra lại các ô được đánh dấu."],
  ["network_error", "Không kết nối được máy chủ. Kiểm tra mạng rồi thử lại."],
]);

function describeWait(seconds) {
  if (seconds === null || seconds <= 0) return "một lát";
  if (seconds <= MAX_SECONDS_SHOWN_AS_SECONDS) return `${seconds} giây`;
  return `khoảng ${Math.ceil(seconds / SECONDS_PER_MINUTE)} phút`;
}

/** Câu thông báo tiếng Việt cho lỗi từ api.js (theo error.code, không dùng message của server). */
export function describeApiError(error) {
  if (!(error instanceof ApiError)) return FALLBACK_ERROR_MESSAGE;
  if (error.code === "rate_limit_exceeded") {
    return `Bạn đã thử quá nhiều lần. Vui lòng thử lại sau ${describeWait(error.retryAfterSeconds)}.`;
  }
  return ERROR_MESSAGES.get(error.code) ?? FALLBACK_ERROR_MESSAGE;
}

function describeFieldError(fieldName, detailCode) {
  if (detailCode === "missing") return "Vui lòng nhập trường này.";
  if (detailCode === "string_too_long") return "Giá trị quá dài.";
  if (detailCode === "string_too_short") {
    if (PASSWORD_FIELDS.has(fieldName)) return "Mật khẩu cần ít nhất 10 ký tự.";
    if (fieldName === "full_name") return "Vui lòng nhập họ tên.";
    return "Giá trị quá ngắn.";
  }
  if (detailCode === "value_error") {
    if (fieldName === "email") return "Email không hợp lệ.";
    if (PASSWORD_FIELDS.has(fieldName)) return "Mật khẩu không được chỉ gồm khoảng trắng.";
  }
  return FALLBACK_FIELD_MESSAGE;
}

/** "body.email" → "email" (vị trí field trong envelope 422 của app/core/errors.py). */
function fieldNameOf(detail) {
  return String(detail?.field ?? "").split(".").at(-1);
}

/**
 * Điều khiển một <form data-status="id-vùng-trạng-thái">: trạng thái gửi, lỗi theo ô, thông báo.
 * Nút gửi dùng aria-disabled (không dùng disabled) để focus không rơi mất khi đang gửi.
 * Vùng trạng thái (role="status") luôn có mặt; chỉ hộp `.form-status` bên trong được ẩn/hiện,
 * để trình đọc màn hình vẫn đọc được thông báo mới.
 */
export function createFormController(form) {
  const submitButton = form.querySelector('button[type="submit"]');
  const statusRegion = document.getElementById(form.dataset.status);
  const status = statusRegion.querySelector(".form-status");
  const statusText = statusRegion.querySelector("[data-status-text]");
  const idleLabel = submitButton.textContent;
  let isBusy = false;

  function inputFor(fieldName) {
    return form.elements.namedItem(fieldName);
  }

  function errorElementFor(input) {
    return document.getElementById(`${input.id}-error`);
  }

  function clearFieldErrors() {
    for (const input of form.querySelectorAll("[aria-invalid]")) {
      input.removeAttribute("aria-invalid");
      const errorElement = errorElementFor(input);
      if (errorElement !== null) {
        errorElement.textContent = "";
        errorElement.hidden = true;
      }
    }
  }

  function showFieldError(fieldName, message) {
    const input = inputFor(fieldName);
    if (!(input instanceof HTMLInputElement)) return null;
    input.setAttribute("aria-invalid", "true");
    const errorElement = errorElementFor(input);
    if (errorElement !== null) {
      errorElement.textContent = message;
      errorElement.hidden = false;
    }
    return input;
  }

  function showStatus(state, message, { action = null } = {}) {
    status.dataset.state = state;
    statusText.textContent = message;
    for (const actionElement of status.querySelectorAll("[data-status-action]")) {
      actionElement.hidden = actionElement.dataset.statusAction !== action;
    }
    status.hidden = false;
  }

  function hideStatus() {
    status.hidden = true;
    statusText.textContent = "";
  }

  /** Lỗi 422 → lỗi dưới từng ô và focus ô sai đầu tiên; lỗi khác → vùng trạng thái. */
  function showApiError(error, { action = null } = {}) {
    showStatus("error", describeApiError(error), { action });
    if (!(error instanceof ApiError) || error.details.length === 0) return;
    const invalidInputs = error.details
      .map((detail) => showFieldError(fieldNameOf(detail), describeFieldError(fieldNameOf(detail), detail.code)))
      .filter((input) => input !== null);
    if (invalidInputs.length > 0) invalidInputs[0].focus();
  }

  function setBusy(busy, busyLabel) {
    isBusy = busy;
    form.setAttribute("aria-busy", String(busy));
    if (busy) submitButton.setAttribute("aria-disabled", "true");
    else submitButton.removeAttribute("aria-disabled");
    submitButton.textContent = busy ? busyLabel : idleLabel;
  }

  /** Gắn xử lý submit: chặn gửi lại khi request trước chưa xong, luôn trả nút về trạng thái thường. */
  function onSubmit(handler, { busyLabel }) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); // không bao giờ để trình duyệt tự gửi form (mật khẩu)
      if (isBusy) return;
      clearFieldErrors();
      hideStatus();
      setBusy(true, busyLabel);
      try {
        await handler(new FormData(form));
      } finally {
        setBusy(false, busyLabel);
      }
    });
  }

  return { onSubmit, showStatus, hideStatus, showApiError, showFieldError, inputFor };
}

/** Ẩn form và hiện thông báo "đang đăng nhập" khi đã có phiên (trang đăng nhập / đăng ký). */
export function toggleSignedInNotice(user, { formSection, notice, nameElement }) {
  const isSignedIn = user !== null;
  nameElement.textContent = isSignedIn ? user.full_name : "";
  notice.hidden = !isSignedIn;
  formSection.hidden = isSignedIn;
}

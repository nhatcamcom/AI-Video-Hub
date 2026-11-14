// static/js/registerPage.js — trang /register: POST /auth/register, rồi tự POST /auth/login
import { ApiError, apiRequest } from "./api.js";
import { createFormController, toggleSignedInNotice } from "./authForm.js";
import { createSessionHeader } from "./session.js";

const REGISTER_PATH = "/api/v1/auth/register";
const LOGIN_PATH = "/api/v1/auth/login";
const HOME_PATH = "/";
const BUSY_LABEL = "Đang tạo tài khoản…";
const SIGNING_IN_MESSAGE = "Đã tạo tài khoản. Đang đăng nhập…";
const SUCCESS_MESSAGE = "Đăng ký thành công. Đang chuyển về trang chủ…";
const MANUAL_LOGIN_MESSAGE =
  "Tài khoản đã được tạo nhưng chưa tự đăng nhập được. Vui lòng đăng nhập thủ công bằng email và mật khẩu vừa đăng ký.";
const DUPLICATE_EMAIL_FIELD_MESSAGE = "Email này đã được đăng ký.";

function readRegistration(formData) {
  return {
    full_name: String(formData.get("full_name") ?? ""),
    email: String(formData.get("email") ?? ""),
    password: String(formData.get("password") ?? ""),
  };
}

function initRegisterPage() {
  const form = document.getElementById("register-form");
  const signedInView = {
    formSection: document.getElementById("register-section"),
    notice: document.getElementById("signed-in-notice"),
    nameElement: document.getElementById("signed-in-name"),
  };
  const controller = createFormController(form);
  const session = createSessionHeader({
    onUserChange: (user) => toggleSignedInNotice(user, signedInView),
  });

  async function register(registration) {
    try {
      await apiRequest(REGISTER_PATH, { method: "POST", body: registration });
      return true;
    } catch (error) {
      const isDuplicate = error instanceof ApiError && error.code === "email_already_registered";
      controller.showApiError(error, { action: isDuplicate ? "existing-account" : null });
      if (isDuplicate) controller.showFieldError("email", DUPLICATE_EMAIL_FIELD_MESSAGE)?.focus();
      return false;
    }
  }

  async function signInAfterRegister({ email, password }) {
    controller.showStatus("info", SIGNING_IN_MESSAGE);
    try {
      const user = await apiRequest(LOGIN_PATH, { method: "POST", body: { email, password } });
      controller.showStatus("success", SUCCESS_MESSAGE);
      session.setUser(user);
      window.location.assign(HOME_PATH);
    } catch {
      // Đăng ký đã thành công: không báo lỗi đăng ký, chỉ hướng dẫn đăng nhập thủ công
      form.reset(); // không giữ mật khẩu trong ô nhập
      controller.showStatus("info", MANUAL_LOGIN_MESSAGE, { action: "manual-login" });
    }
  }

  controller.onSubmit(async (formData) => {
    const registration = readRegistration(formData);
    if (await register(registration)) await signInAfterRegister(registration);
  }, { busyLabel: BUSY_LABEL });

  session.refresh();
}

initRegisterPage();

// static/js/loginPage.js — trang /login: POST /auth/login rồi về trang chủ
import { ApiError, apiRequest } from "./api.js";
import { createFormController, toggleSignedInNotice } from "./authForm.js";
import { createSessionHeader } from "./session.js";

const LOGIN_PATH = "/api/v1/auth/login";
const HOME_PATH = "/";
const BUSY_LABEL = "Đang đăng nhập…";
const SUCCESS_MESSAGE = "Đăng nhập thành công. Đang chuyển về trang chủ…";

function readCredentials(formData) {
  return {
    email: String(formData.get("email") ?? ""),
    password: String(formData.get("password") ?? ""),
  };
}

function initLoginPage() {
  const form = document.getElementById("login-form");
  const signedInView = {
    formSection: document.getElementById("login-section"),
    notice: document.getElementById("signed-in-notice"),
    nameElement: document.getElementById("signed-in-name"),
  };
  const controller = createFormController(form);
  const session = createSessionHeader({
    onUserChange: (user) => toggleSignedInNotice(user, signedInView),
  });

  controller.onSubmit(async (formData) => {
    try {
      const user = await apiRequest(LOGIN_PATH, { method: "POST", body: readCredentials(formData) });
      controller.showStatus("success", SUCCESS_MESSAGE);
      session.setUser(user);
      window.location.assign(HOME_PATH);
    } catch (error) {
      // Không giữ mật khẩu sai trong ô nhập
      if (error instanceof ApiError && error.code === "invalid_credentials") {
        controller.inputFor("password").value = "";
      }
      controller.showApiError(error);
    }
  }, { busyLabel: BUSY_LABEL });

  session.refresh();
}

initLoginPage();

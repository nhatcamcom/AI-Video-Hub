// static/js/forgotPasswordPage.js — trang /forgot-password: POST /auth/forgot-password
import { apiRequest } from "./api.js";
import { createFormController } from "./authForm.js";
import { createSessionHeader } from "./session.js";

const FORGOT_PASSWORD_PATH = "/api/v1/auth/forgot-password";
const BUSY_LABEL = "Đang gửi…";
// Luôn cùng một câu dù email có tồn tại hay không (giống phản hồi của API, chống dò tài khoản)
const SENT_MESSAGE =
  "Nếu email có trong hệ thống, chúng tôi đã gửi hướng dẫn đặt lại mật khẩu. Vui lòng kiểm tra hộp thư (cả thư mục spam). Liên kết chỉ dùng được một lần và có hạn.";

function initForgotPasswordPage() {
  const form = document.getElementById("forgot-password-form");
  const controller = createFormController(form);
  const session = createSessionHeader();

  controller.onSubmit(async (formData) => {
    try {
      await apiRequest(FORGOT_PASSWORD_PATH, {
        method: "POST",
        body: { email: String(formData.get("email") ?? "") },
      });
      controller.showStatus("success", SENT_MESSAGE);
    } catch (error) {
      controller.showApiError(error);
    }
  }, { busyLabel: BUSY_LABEL });

  session.refresh();
}

initForgotPasswordPage();

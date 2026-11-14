// static/js/home.js — trang khung: kiểm tra kết nối máy chủ qua api.js (loading / success / error)
import { ApiError, apiRequest } from "./api.js";
import { createSessionHeader } from "./session.js";

const HEALTH_PATH = "/api/v1/health";
const LOADING_MESSAGE = "Đang kiểm tra kết nối máy chủ…";
const SUCCESS_MESSAGE = "Máy chủ hoạt động bình thường.";
const FALLBACK_ERROR_MESSAGE = "Không kiểm tra được trạng thái máy chủ. Vui lòng thử lại.";
// Map (không dùng object thường) để mã lỗi lạ như "__proto__" không trỏ vào prototype
const ERROR_MESSAGES = new Map([
  ["network_error", "Không kết nối được máy chủ. Kiểm tra mạng rồi bấm “Kiểm tra lại”."],
  ["database_unavailable", "Máy chủ đang chạy nhưng cơ sở dữ liệu tạm thời không khả dụng. Vui lòng thử lại sau ít phút."],
]);

function describeError(error) {
  if (error instanceof ApiError) return ERROR_MESSAGES.get(error.code) ?? FALLBACK_ERROR_MESSAGE;
  return FALLBACK_ERROR_MESSAGE;
}

function renderStatus(view, state, message) {
  view.container.dataset.state = state;
  view.text.textContent = message; // textContent: không bao giờ diễn giải chuỗi thành HTML
  const isRetryVisible = state === "error";
  // Nút đang giữ focus sắp bị ẩn → chuyển focus sang dòng trạng thái để không rơi về <body>
  if (!isRetryVisible && document.activeElement === view.retryButton) view.text.focus();
  view.retryButton.hidden = !isRetryVisible;
}

async function checkServerStatus(view) {
  renderStatus(view, "loading", LOADING_MESSAGE);
  try {
    await apiRequest(HEALTH_PATH);
    renderStatus(view, "success", SUCCESS_MESSAGE);
  } catch (error) {
    renderStatus(view, "error", describeError(error));
  }
}

function findStatusView() {
  const view = {
    container: document.getElementById("server-status"),
    text: document.getElementById("server-status-text"),
    retryButton: document.getElementById("server-status-retry"),
  };
  return Object.values(view).every((element) => element !== null) ? view : null;
}

function initHomePage() {
  const view = findStatusView();
  if (view === null) return;
  let isChecking = false;
  const runCheck = async () => {
    if (isChecking) return; // chặn bấm liên tục khi request trước chưa xong
    isChecking = true;
    try {
      await checkServerStatus(view);
    } finally {
      isChecking = false;
    }
  };
  view.retryButton.addEventListener("click", runCheck);
  runCheck();
}

initHomePage();
createSessionHeader().refresh();

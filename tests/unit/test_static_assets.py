"""File giao diện tĩnh tuân thủ CSP chặt (không inline) và các quy tắc a11y / XSS / form cơ bản."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
STATIC_URL_PREFIX = "/static/"
JS_FILES = sorted(STATIC_DIR.rglob("*.js"))
CSS_FILES = sorted(STATIC_DIR.rglob("*.css"))

AUTH_PAGES = ["login.html", "register.html", "forgot-password.html", "reset-password.html"]
ALL_PAGES = ["index.html", *AUTH_PAGES]
# Ô nhập thật sự nhận dữ liệu người dùng (cần <label>)
NON_LABELLED_INPUT_TYPES = {"hidden", "submit", "button", "reset"}
PASSWORD_AUTOCOMPLETE = {
    "login.html": {"current-password"},
    "register.html": {"new-password"},
    "reset-password.html": {"new-password"},
}
EMAIL_AUTOCOMPLETE = {"email", "username"}
API_MODULE = "api.js"

# Mẫu bị cấm trong JS: chèn HTML thô (XSS), eval, lưu token phía client, log debug
FORBIDDEN_JS_PATTERNS = {
    "innerHTML": re.compile(r"\binnerHTML\b"),
    "outerHTML": re.compile(r"\bouterHTML\b"),
    "insertAdjacentHTML": re.compile(r"\binsertAdjacentHTML\b"),
    "document.write": re.compile(r"\bdocument\.write"),
    "eval": re.compile(r"\beval\s*\("),
    "new Function": re.compile(r"\bnew\s+Function\b"),
    "localStorage": re.compile(r"\blocalStorage\b"),
    "sessionStorage": re.compile(r"\bsessionStorage\b"),
    "document.cookie ghi": re.compile(r"\bdocument\.cookie\s*="),
    "console.log": re.compile(r"\bconsole\.log\b"),
    "setAttribute style": re.compile(r"setAttribute\(\s*[\"']style"),
}
FORBIDDEN_CSS_PATTERNS = {
    "transition: all": re.compile(r"transition\s*:\s*all\b"),
    "will-change: all": re.compile(r"will-change\s*:\s*all\b"),
    "@import url ngoài": re.compile(r"@import\s+url\(\s*[\"']?https?:"),
}
FETCH_CALL = re.compile(r"\bfetch\s*\(")


class PageInspector(HTMLParser):
    """Gom các thẻ / thuộc tính cần kiểm tra trong một trang HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.inline_script_bodies: list[str] = []
        self._inside_script_without_src = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if tag == "script" and not attributes.get("src"):
            self._inside_script_without_src = True
            self.inline_script_bodies.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inside_script_without_src = False

    def handle_data(self, data: str) -> None:
        if self._inside_script_without_src:
            self.inline_script_bodies[-1] += data

    def find_all(self, tag: str) -> list[dict[str, str | None]]:
        return [attributes for name, attributes in self.tags if name == tag]

    def ids(self) -> set[str]:
        return {attrs["id"] for _, attrs in self.tags if attrs.get("id")}  # type: ignore[misc]


def inspect_page(page_name: str) -> PageInspector:
    inspector = PageInspector()
    inspector.feed((STATIC_DIR / page_name).read_text(encoding="utf-8"))
    return inspector


def strip_js_comments(source: str) -> str:
    return re.sub(r"//.*", "", source)  # comment được phép nhắc tên mẫu bị cấm


# --------------------------------------------------------------------------- mọi trang
@pytest.mark.parametrize("page_name", ALL_PAGES)
def test_page_has_no_inline_script(page_name: str) -> None:
    page = inspect_page(page_name)
    scripts = page.find_all("script")

    assert scripts, "trang phải nạp ít nhất một script"
    assert page.inline_script_bodies == []
    assert all(script.get("type") == "module" for script in scripts)


@pytest.mark.parametrize("page_name", ALL_PAGES)
def test_page_has_no_inline_style_or_event_handlers(page_name: str) -> None:
    page = inspect_page(page_name)
    handlers = [name for _, attributes in page.tags for name in attributes if name.startswith("on")]

    assert page.find_all("style") == []
    assert all("style" not in attributes for _, attributes in page.tags)
    assert handlers == []


@pytest.mark.parametrize("page_name", ALL_PAGES)
def test_page_has_landmarks_single_heading_and_live_region(page_name: str) -> None:
    page = inspect_page(page_name)

    assert len(page.find_all("h1")) == 1
    assert len(page.find_all("header")) == 1
    assert len(page.find_all("main")) == 1
    assert any(attrs.get("aria-live") == "polite" for _, attrs in page.tags)


@pytest.mark.parametrize("page_name", ALL_PAGES)
def test_page_declares_language_charset_and_viewport(page_name: str) -> None:
    page = inspect_page(page_name)
    metas = page.find_all("meta")

    assert page.find_all("html")[0].get("lang") == "vi"
    assert any((meta.get("charset") or "").lower() == "utf-8" for meta in metas)
    assert any(meta.get("name") == "viewport" for meta in metas)
    assert page.find_all("title")


@pytest.mark.parametrize("page_name", ALL_PAGES)
def test_page_only_references_existing_local_assets(page_name: str) -> None:
    page = inspect_page(page_name)
    references = [
        attributes.get("src") or attributes.get("href")
        for tag, attributes in page.tags
        if tag in {"script", "link"}
    ]
    local = [ref for ref in references if ref and ref.startswith(STATIC_URL_PREFIX)]
    external = [ref for ref in references if ref and ref.startswith(("http:", "https:", "//"))]

    assert local, "trang phải dùng CSS/JS cùng origin"
    assert external == []
    for reference in local:
        assert (STATIC_DIR / reference.removeprefix(STATIC_URL_PREFIX)).is_file(), reference


@pytest.mark.parametrize("page_name", ALL_PAGES)
def test_page_header_has_session_status_area(page_name: str) -> None:
    page = inspect_page(page_name)

    assert {"session-nav", "session-user-name", "logout-button"} <= page.ids()


@pytest.mark.parametrize("page_name", ALL_PAGES)
def test_buttons_have_explicit_type(page_name: str) -> None:
    buttons = inspect_page(page_name).find_all("button")

    assert all(button.get("type") in {"button", "submit"} for button in buttons)


# --------------------------------------------------------------------------- form xác thực
@pytest.mark.parametrize("page_name", AUTH_PAGES)
def test_auth_forms_post_to_same_page_never_get(page_name: str) -> None:
    # Nếu JS không chạy, method="post" giữ mật khẩu trong body thay vì lộ lên URL
    forms = inspect_page(page_name).find_all("form")

    assert len(forms) == 1
    assert forms[0].get("method") == "post"
    assert "action" not in forms[0]


@pytest.mark.parametrize("page_name", AUTH_PAGES)
def test_every_input_has_a_label(page_name: str) -> None:
    page = inspect_page(page_name)
    label_targets = {label.get("for") for label in page.find_all("label")}
    inputs = [
        attrs
        for attrs in page.find_all("input")
        if (attrs.get("type") or "text") not in NON_LABELLED_INPUT_TYPES
    ]

    assert inputs
    for attrs in inputs:
        assert attrs.get("id") in label_targets, attrs
        assert attrs.get("name"), attrs


@pytest.mark.parametrize("page_name", sorted(PASSWORD_AUTOCOMPLETE))
def test_password_inputs_use_correct_autocomplete(page_name: str) -> None:
    passwords = [
        a for a in inspect_page(page_name).find_all("input") if a.get("type") == "password"
    ]

    assert passwords
    for attrs in passwords:
        assert attrs.get("autocomplete") in PASSWORD_AUTOCOMPLETE[page_name]


@pytest.mark.parametrize("page_name", ["login.html", "register.html", "forgot-password.html"])
def test_email_inputs_use_email_type_and_autocomplete(page_name: str) -> None:
    emails = [a for a in inspect_page(page_name).find_all("input") if a.get("name") == "email"]

    assert len(emails) == 1
    assert emails[0].get("type") == "email"
    assert emails[0].get("autocomplete") in EMAIL_AUTOCOMPLETE


def test_reset_page_asks_to_confirm_new_password() -> None:
    names = {attrs.get("name") for attrs in inspect_page("reset-password.html").find_all("input")}

    assert {"new_password", "confirm_password"} <= names
    assert "token" not in names  # token chỉ nằm trong bộ nhớ JS, không bao giờ nằm trong form


# --------------------------------------------------------------------------- JS / CSS
@pytest.mark.parametrize("js_file", JS_FILES, ids=lambda path: path.name)
def test_javascript_avoids_forbidden_patterns(js_file: Path) -> None:
    code_only = strip_js_comments(js_file.read_text(encoding="utf-8"))

    found = [name for name, pattern in FORBIDDEN_JS_PATTERNS.items() if pattern.search(code_only)]

    assert found == []


@pytest.mark.parametrize(
    "js_file", [f for f in JS_FILES if f.name != API_MODULE], ids=lambda path: path.name
)
def test_only_api_module_calls_fetch(js_file: Path) -> None:
    code_only = strip_js_comments(js_file.read_text(encoding="utf-8"))

    assert not FETCH_CALL.search(code_only)


def test_reset_page_script_removes_token_from_url() -> None:
    code_only = strip_js_comments((STATIC_DIR / "js" / "resetPasswordPage.js").read_text("utf-8"))

    assert "history.replaceState" in code_only
    assert "location.hash" in code_only


@pytest.mark.parametrize("css_file", CSS_FILES, ids=lambda path: path.name)
def test_css_avoids_forbidden_patterns(css_file: Path) -> None:
    source = css_file.read_text(encoding="utf-8")

    found = [name for name, pattern in FORBIDDEN_CSS_PATTERNS.items() if pattern.search(source)]

    assert found == []


def test_expected_static_files_exist() -> None:
    expected = [
        *ALL_PAGES,
        "css/tokens.css",
        "css/base.css",
        "css/forms.css",
        "js/api.js",
        "js/home.js",
        "js/session.js",
        "js/authForm.js",
        "js/loginPage.js",
        "js/registerPage.js",
        "js/forgotPasswordPage.js",
        "js/resetPasswordPage.js",
    ]

    missing = [name for name in expected if not (STATIC_DIR / name).is_file()]

    assert missing == []

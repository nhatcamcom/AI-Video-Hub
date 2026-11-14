"""P2-1: file giao diện tĩnh tuân thủ CSP chặt (không inline) và các quy tắc a11y / XSS cơ bản."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
INDEX_PAGE = STATIC_DIR / "index.html"
STATIC_URL_PREFIX = "/static/"
JS_FILES = sorted(STATIC_DIR.rglob("*.js"))
CSS_FILES = sorted(STATIC_DIR.rglob("*.css"))

# Mẫu bị cấm trong JS: chèn HTML thô (XSS), eval, lưu token phía client, log debug
FORBIDDEN_JS_PATTERNS = {
    "innerHTML": re.compile(r"\binnerHTML\b"),
    "outerHTML": re.compile(r"\bouterHTML\b"),
    "insertAdjacentHTML": re.compile(r"\binsertAdjacentHTML\b"),
    "document.write": re.compile(r"\bdocument\.write"),
    "eval": re.compile(r"\beval\s*\("),
    "new Function": re.compile(r"\bnew\s+Function\b"),
    "localStorage": re.compile(r"\blocalStorage\s*[.\[]"),
    "sessionStorage": re.compile(r"\bsessionStorage\s*[.\[]"),
    "console.log": re.compile(r"\bconsole\.log\b"),
    "setAttribute style": re.compile(r"setAttribute\(\s*[\"']style"),
}
FORBIDDEN_CSS_PATTERNS = {
    "transition: all": re.compile(r"transition\s*:\s*all\b"),
    "will-change: all": re.compile(r"will-change\s*:\s*all\b"),
    "@import url ngoài": re.compile(r"@import\s+url\(\s*[\"']?https?:"),
}


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


@pytest.fixture(scope="module")
def index_page() -> PageInspector:
    inspector = PageInspector()
    inspector.feed(INDEX_PAGE.read_text(encoding="utf-8"))
    return inspector


def test_index_page_has_no_inline_script(index_page: PageInspector) -> None:
    scripts = index_page.find_all("script")

    assert scripts, "trang khung phải nạp ít nhất một script"
    assert index_page.inline_script_bodies == []
    assert all(script.get("type") == "module" for script in scripts)


def test_index_page_has_no_inline_style(index_page: PageInspector) -> None:
    assert index_page.find_all("style") == []
    assert all("style" not in attributes for _, attributes in index_page.tags)


def test_index_page_has_no_inline_event_handlers(index_page: PageInspector) -> None:
    handlers = [
        (tag, name)
        for tag, attributes in index_page.tags
        for name in attributes
        if name.startswith("on")
    ]

    assert handlers == []


def test_index_page_has_landmarks_and_single_heading(index_page: PageInspector) -> None:
    assert len(index_page.find_all("h1")) == 1
    assert len(index_page.find_all("header")) == 1
    assert len(index_page.find_all("main")) == 1
    assert any(attrs.get("aria-live") == "polite" for _, attrs in index_page.tags)


def test_index_page_declares_language_charset_and_viewport(index_page: PageInspector) -> None:
    html_tag = index_page.find_all("html")[0]
    metas = index_page.find_all("meta")

    assert html_tag.get("lang") == "vi"
    assert any(meta.get("charset", "").lower() == "utf-8" for meta in metas)  # type: ignore[union-attr]
    assert any(meta.get("name") == "viewport" for meta in metas)


def test_index_page_only_references_existing_local_assets(index_page: PageInspector) -> None:
    references = [
        attributes.get("src") or attributes.get("href")
        for tag, attributes in index_page.tags
        if tag in {"script", "link"}
    ]
    local = [ref for ref in references if ref and ref.startswith(STATIC_URL_PREFIX)]
    external = [ref for ref in references if ref and ref.startswith(("http:", "https:", "//"))]

    assert local, "trang phải dùng CSS/JS cùng origin"
    assert external == []
    for reference in local:
        assert (STATIC_DIR / reference.removeprefix(STATIC_URL_PREFIX)).is_file(), reference


def test_buttons_have_explicit_type(index_page: PageInspector) -> None:
    assert all(
        button.get("type") in {"button", "submit"} for button in index_page.find_all("button")
    )


@pytest.mark.parametrize("js_file", JS_FILES, ids=lambda path: path.name)
def test_javascript_avoids_forbidden_patterns(js_file: Path) -> None:
    source = js_file.read_text(encoding="utf-8")
    code_only = re.sub(r"//.*", "", source)  # comment được phép nhắc tên mẫu bị cấm

    found = [name for name, pattern in FORBIDDEN_JS_PATTERNS.items() if pattern.search(code_only)]

    assert found == []


@pytest.mark.parametrize("css_file", CSS_FILES, ids=lambda path: path.name)
def test_css_avoids_forbidden_patterns(css_file: Path) -> None:
    source = css_file.read_text(encoding="utf-8")

    found = [name for name, pattern in FORBIDDEN_CSS_PATTERNS.items() if pattern.search(source)]

    assert found == []


def test_expected_static_files_exist() -> None:
    expected = ["index.html", "css/tokens.css", "css/base.css", "js/api.js", "js/home.js"]

    missing = [name for name in expected if not (STATIC_DIR / name).is_file()]

    assert missing == []

"""Cấu hình lưu trữ (P2-3): STORAGE_ROOT an toàn và hạn mức theo user."""

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import config as config_module
from tests.conftest import build_settings

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ONE_GIBIBYTE = 1_073_741_824


def test_storage_defaults_match_approved_decisions() -> None:
    settings = build_settings()

    assert settings.storage_root == "storage"
    assert settings.storage_root_path == (PROJECT_ROOT / "storage").resolve()
    assert settings.storage_quota_bytes_per_user == ONE_GIBIBYTE


@pytest.mark.parametrize(
    "raw_root",
    ["storage", "./storage", "storage/../storage", str(PROJECT_ROOT / "storage")],
    ids=["relative", "dot-relative", "dot-dot-normalised", "absolute"],
)
def test_relative_and_absolute_storage_roots_resolve_to_the_same_path(raw_root: str) -> None:
    assert (
        build_settings(storage_root=raw_root).storage_root_path
        == (PROJECT_ROOT / "storage").resolve()
    )


def test_relative_storage_root_does_not_depend_on_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert (
        build_settings(storage_root="storage").storage_root_path
        == (PROJECT_ROOT / "storage").resolve()
    )


def test_absolute_storage_root_outside_project_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "media-root"

    assert build_settings(storage_root=str(root)).storage_root_path == root.resolve()


@pytest.mark.parametrize(
    "raw_root",
    [
        "static",
        "static/uploads",
        "app",
        "app/web",
        "storage/../static",
        "storage/../app/core",
        ".",
        str(PROJECT_ROOT),
        str(PROJECT_ROOT.parent),
        str(PROJECT_ROOT / "static" / "css"),
    ],
    ids=[
        "static",
        "inside-static",
        "app",
        "inside-app",
        "dot-dot-into-static",
        "dot-dot-into-app",
        "project-root-relative",
        "project-root-absolute",
        "parent-of-project",
        "absolute-inside-static",
    ],
)
def test_storage_root_inside_or_containing_static_or_app_is_rejected(raw_root: str) -> None:
    with pytest.raises(ValidationError, match="storage_root"):
        build_settings(storage_root=raw_root)


@pytest.mark.skipif(
    sys.platform != "win32", reason="hệ thống file Windows không phân biệt hoa/thường"
)
@pytest.mark.parametrize("raw_root", ["STATIC", "App/Web"], ids=["upper-static", "mixed-app"])
def test_storage_root_check_is_case_insensitive_on_windows(raw_root: str) -> None:
    with pytest.raises(ValidationError, match="storage_root"):
        build_settings(storage_root=raw_root)


def make_directory_link(link: Path, target: Path) -> None:
    """Symlink thư mục; Windows thiếu quyền tạo symlink thì dùng junction; không được thì skip."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        pass
    if sys.platform == "win32":
        import _winapi  # chỉ có trên Windows

        _winapi.CreateJunction(str(target), str(link))
        return
    pytest.skip("không tạo được liên kết thư mục trên máy này")


def remove_directory_link(link: Path) -> None:
    # rmdir/unlink chỉ gỡ chính liên kết, không đụng tới nội dung thư mục đích
    if link.is_junction():
        os.rmdir(link)
    elif link.is_symlink():
        link.unlink()


@pytest.fixture
def fake_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Cây dự án giả: test liên kết KHÔNG BAO GIỜ trỏ vào static/ hay app/ thật của repo
    (một liên kết sót lại tới thư mục thật có thể khiến công cụ dọn dẹp xóa nhầm mã nguồn)."""
    project = tmp_path / "fake-project"
    forbidden = (project / "static", project / "app")
    for directory in forbidden:
        directory.mkdir(parents=True)
    monkeypatch.setattr(config_module, "STORAGE_FORBIDDEN_DIRS", forbidden)
    yield project


@pytest.mark.parametrize("target_name", ["static", "app"])
def test_storage_root_linked_into_static_or_app_is_rejected(
    tmp_path: Path, fake_project: Path, target_name: str
) -> None:
    link = tmp_path / "looks-safe"
    make_directory_link(link, fake_project / target_name)
    try:
        with pytest.raises(ValidationError, match="storage_root"):
            build_settings(storage_root=str(link))
    finally:
        remove_directory_link(link)


def test_storage_root_link_to_unrelated_directory_is_accepted(
    tmp_path: Path, fake_project: Path
) -> None:
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    link = tmp_path / "media-link"
    make_directory_link(link, unrelated)
    try:
        assert build_settings(storage_root=str(link)).storage_root_path == unrelated.resolve()
    finally:
        remove_directory_link(link)


def test_creating_settings_never_creates_storage_directories(tmp_path: Path) -> None:
    root = tmp_path / "not-yet" / "media"

    settings = build_settings(storage_root=str(root))

    assert settings.storage_root_path == root.resolve()
    assert not (tmp_path / "not-yet").exists()


@pytest.mark.parametrize("quota", [0, -1], ids=["zero", "negative"])
def test_storage_quota_must_be_positive(quota: int) -> None:
    with pytest.raises(ValidationError, match="storage_quota_bytes_per_user"):
        build_settings(storage_quota_bytes_per_user=quota)

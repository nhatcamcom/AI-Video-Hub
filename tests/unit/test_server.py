from unittest.mock import patch

import pytest

from app.server import DEFAULT_GRACEFUL_SHUTDOWN_SECONDS, build_server_options, main
from tests.conftest import build_settings

pytestmark = pytest.mark.unit


def test_server_never_trusts_uvicorn_proxy_headers() -> None:
    assert build_server_options([])["proxy_headers"] is False
    assert build_server_options(["--reload", "--port", "9000"])["proxy_headers"] is False


def test_server_runs_the_app_factory_with_given_options() -> None:
    options = build_server_options(["--host", "0.0.0.0", "--port", "9000", "--reload"])  # noqa: S104

    assert options["app"] == "app.main:create_app"
    assert options["factory"] is True
    assert (options["host"], options["port"], options["reload"]) == ("0.0.0.0", 9000, True)  # noqa: S104


def test_server_defaults_to_localhost_without_reload() -> None:
    options = build_server_options([])

    assert (options["host"], options["port"], options["reload"]) == ("127.0.0.1", 8000, False)


def test_main_starts_uvicorn_with_safe_options() -> None:
    with (
        patch("app.server.uvicorn.run", autospec=True) as run,
        patch("app.server.get_settings", return_value=build_settings()),
    ):
        main(["--port", "8123"])

    run.assert_called_once()
    assert run.call_args.kwargs["proxy_headers"] is False
    assert run.call_args.kwargs["port"] == 8123


def test_uvicorn_cancels_hanging_requests_after_graceful_timeout() -> None:
    assert (
        build_server_options([])["timeout_graceful_shutdown"] == DEFAULT_GRACEFUL_SHUTDOWN_SECONDS
    )
    assert build_server_options([], graceful_shutdown_seconds=7)["timeout_graceful_shutdown"] == 7


def test_main_uses_the_configured_graceful_timeout() -> None:
    with (
        patch("app.server.uvicorn.run", autospec=True) as run,
        patch(
            "app.server.get_settings",
            return_value=build_settings(server_graceful_shutdown_seconds=33),
        ),
    ):
        main([])

    assert run.call_args.kwargs["timeout_graceful_shutdown"] == 33

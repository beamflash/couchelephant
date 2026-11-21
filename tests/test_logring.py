import logging
import time

import pytest

from app import logring

log = logging.getLogger("couchelephant")


@pytest.fixture(autouse=True)
def ring():
    logring.install()
    logring.clear()
    yield
    logring.clear()


def test_recent_returns_logged_lines_in_order():
    log.info("first")
    log.warning("second")
    lines = logring.recent()
    assert len(lines) == 2
    assert lines[0].endswith(" INFO couchelephant: first")
    assert lines[1].endswith(" WARNING couchelephant: second")
    assert lines[0][2] == ":" and lines[0][5] == ":" and lines[0][8] == " "


def test_old_lines_age_out():
    log.info("old")
    assert logring.recent(now=time.time() + 20 * 60) == []


def test_line_cap():
    for i in range(3000):
        log.info("n%d", i)
    lines = logring.recent()
    assert len(lines) == 2000
    assert lines[-1].endswith("n2999")
    assert lines[0].endswith("n1000")


def test_install_twice_logs_once():
    logring.install()
    logring.install()
    log.info("once")
    assert len(logring.recent()) == 1


def test_exception_text_included():
    try:
        raise ValueError("kaboom")
    except ValueError:
        log.exception("it broke")
    text = "\n".join(logring.recent())
    assert "it broke" in text
    assert "Traceback" in text and "ValueError: kaboom" in text


def test_warning_still_reaches_stderr_and_info_does_not(capfd, monkeypatch):
    # clean state: no root handlers, as in the real app
    monkeypatch.setattr(logging.root, "handlers", [])
    log.info("quiet info")
    log.warning("loud warning")
    logging.getLogger("couchelephant.recorder").error("child error")
    err = capfd.readouterr().err
    assert "loud warning" in err
    assert "child error" in err
    assert "quiet info" not in err
    assert len(logring.recent()) == 3


def test_no_double_output_when_another_handler_exists(capfd, monkeypatch):
    seen = logging.Handler()
    seen.emit = lambda r: None
    monkeypatch.setattr(logging.root, "handlers", [seen])
    log.warning("handled elsewhere")
    assert capfd.readouterr().err == ""

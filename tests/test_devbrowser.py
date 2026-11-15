import asyncio

from couchd.devbrowser import DevBrowser, browser_args, find_browser


def test_browser_override(monkeypatch):
    monkeypatch.setenv("COUCH_BROWSER", "/opt/my/chromium")
    assert find_browser() == "/opt/my/chromium"


def test_no_browser_found(monkeypatch):
    monkeypatch.delenv("COUCH_BROWSER", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    assert find_browser() is None


def test_args_open_a_tv_sized_app_window(tmp_path):
    args = browser_args("http://127.0.0.1:8765", tmp_path)
    assert "--app=http://127.0.0.1:8765" in args
    assert "--window-size=1920,1080" in args
    assert f"--user-data-dir={tmp_path}" in args


async def test_opens_and_closes_the_browser(tmp_path, monkeypatch):
    log = tmp_path / "args"
    fake = tmp_path / "chromium"
    fake.write_text(f'#!/bin/sh\necho "$@" > {log}\nexec sleep 30\n')
    fake.chmod(0o755)
    monkeypatch.setenv("COUCH_BROWSER", str(fake))

    browser = DevBrowser(tmp_path / "profile")
    await browser.open("http://127.0.0.1:1234")
    process = browser._process
    for _ in range(100):
        if log.exists():
            break
        await asyncio.sleep(0.02)
    assert "--app=http://127.0.0.1:1234" in log.read_text()
    assert (tmp_path / "profile").is_dir()

    await browser.close()
    assert process.returncode is not None


async def test_missing_browser_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("COUCH_BROWSER", str(tmp_path / "nope"))
    browser = DevBrowser(tmp_path / "profile")
    await browser.open("http://127.0.0.1:1234")
    await browser.close()

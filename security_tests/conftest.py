import os
import socket
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    from free_claude_code.config.loader import clear_settings_cache
    from free_claude_code.harnesses.environment import client_environment

    clean = client_environment(dict(os.environ), proxy_root_url="http://127.0.0.1")
    for key in tuple(os.environ):
        if key not in clean:
            monkeypatch.delenv(key)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    clear_settings_cache()

    # Tests must fail instead of accidentally calling any live provider or broker.
    def deny_network(*args, **kwargs):
        raise AssertionError("Outbound networking is forbidden in security tests")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    clear_settings_cache()
    yield tmp_path
    clear_settings_cache()

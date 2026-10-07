from __future__ import annotations

import base64
import json
import stat
import sys
import time

import pytest

from ucode import _entry, token_cache


def _jwt(expires_at: float, sub: str = "user") -> str:
    claims = json.dumps({"exp": expires_at, "sub": sub}).encode()
    return f"h.{base64.urlsafe_b64encode(claims).decode().rstrip('=')}.s"


@pytest.fixture(autouse=True)
def _no_bearer_env(monkeypatch):
    monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
    monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_FILE", raising=False)


WS = "https://ws.cloud.databricks.com"


class TestReadWrite:
    def test_round_trip(self):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, "prod", token)
        assert token_cache.read(WS, "prod") == token

    def test_key_normalizes_host_but_separates_profiles(self):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS + "/", "prod", token)
        assert token_cache.read(WS.upper(), "prod") == token
        assert token_cache.read(WS, None) is None
        assert token_cache.read(WS, "other") is None

    def test_config_file_is_part_of_the_key(self, monkeypatch):
        token_cache.write(WS, "prod", _jwt(time.time() + 3600))
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", "/elsewhere/.databrickscfg")
        assert token_cache.read(WS, "prod") is None

    def test_near_expiry_is_a_miss(self):
        token_cache.write(WS, None, _jwt(time.time() + token_cache.MIN_REMAINING_SECONDS - 1))
        assert token_cache.read(WS, None) is None

    def test_max_age_is_a_miss(self):
        token_cache.write(WS, None, _jwt(time.time() + 86400))
        later = time.time() + token_cache.MAX_AGE_SECONDS + 1
        assert token_cache.read(WS, None, now=later) is None

    def test_non_jwt_is_never_cached(self):
        token_cache.write(WS, None, "dapi-opaque-pat")
        assert not token_cache.CACHE_DIR.exists()

    @pytest.mark.parametrize("env", ["DATABRICKS_BEARER", "DATABRICKS_BEARER_COMMAND"])
    def test_env_overrides_disable_cache(self, monkeypatch, env):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        monkeypatch.setenv(env, "x")
        assert token_cache.read(WS, None) is None
        token_cache.write(WS, "other", token)
        monkeypatch.delenv(env)
        assert token_cache.read(WS, "other") is None

    def test_corrupt_entry_is_a_miss(self):
        token_cache.write(WS, None, _jwt(time.time() + 3600))
        (entry,) = token_cache.CACHE_DIR.iterdir()
        entry.write_text("{not json")
        assert token_cache.read(WS, None) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_files_are_private(self):
        token_cache.write(WS, None, _jwt(time.time() + 3600))
        assert stat.S_IMODE(token_cache.CACHE_DIR.stat().st_mode) == 0o700
        (entry,) = token_cache.CACHE_DIR.iterdir()
        assert stat.S_IMODE(entry.stat().st_mode) == 0o600

    def test_clear(self):
        token_cache.write(WS, None, _jwt(time.time() + 3600))
        token_cache.clear()
        assert token_cache.read(WS, None) is None

    def test_run_databricks_login_clears_cache(self, monkeypatch):
        import ucode.databricks as db_mod

        token_cache.write(WS, "prod", _jwt(time.time() + 3600))
        monkeypatch.setattr(db_mod, "databricks_cli_path", lambda: "databricks")
        monkeypatch.setattr(db_mod, "run", lambda *a, **k: None)
        db_mod.run_databricks_login(WS, "prod")
        assert token_cache.read(WS, "prod") is None


class TestFastPath:
    @pytest.fixture
    def cached(self):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        token_cache.write(WS, "prod", token)
        return token

    def test_host_only(self, cached):
        assert token_cache.fast_path_token(["auth-token", "--host", WS]) == cached

    def test_host_and_profile_in_either_order(self, cached):
        argv = ["auth-token", "--profile", "prod", "--host", WS]
        assert token_cache.fast_path_token(argv) == cached

    @pytest.mark.parametrize(
        "argv",
        [
            ["auth-token"],
            ["auth-token", "--profile", "prod"],
            ["auth-token", "--host", WS, "--use-pat"],
            ["auth-token", "--host", WS, "--force-refresh"],
            ["auth-token", "--host", WS, "--client-id", "c"],
            ["auth-token", "--host", WS, "--host", WS],
            ["auth-token", f"--host={WS}"],
            ["configure", "--host", WS],
        ],
    )
    def test_other_shapes_defer_to_full_cli(self, cached, argv):
        assert token_cache.fast_path_token(argv) is None


class TestEntry:
    def test_cache_hit_skips_full_cli(self, monkeypatch, capsys):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        monkeypatch.setattr(sys, "argv", ["ug", "auth-token", "--host", WS])
        monkeypatch.setattr("ucode.cli.main", lambda: pytest.fail("full CLI invoked"))
        _entry.main()
        assert capsys.readouterr().out == token + "\n"

    def test_cache_miss_delegates_to_full_cli(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sys, "argv", ["ug", "auth-token", "--host", WS])
        monkeypatch.setattr("ucode.cli.main", lambda: calls.append(True))
        _entry.main()
        assert calls == [True]

import json
import logging
import os
import pathlib
import time
from contextlib import contextmanager
from typing import Dict, Optional

import requests


TOKEN_REFRESH_BUFFER = 60
CACHE_DIR = pathlib.Path.home() / ".ruby_api_tokens"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def file_lock(lock_path: pathlib.Path, timeout_seconds: int = 10):
    """Small cross-platform lock for the shared token cache."""
    started_at = time.time()
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.time() - started_at > timeout_seconds:
                raise TimeoutError(f"Could not acquire token-cache lock: {lock_path}")
            time.sleep(0.1)

    try:
        yield
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def load_env_config(env_name: str) -> Dict[str, str]:
    """Read the conventional per-environment authentication variables."""
    prefix = env_name.upper() + "_"

    def env(key: str) -> str:
        value = os.getenv(prefix + key)
        if not value:
            raise RuntimeError(f"Missing required environment variable: {prefix}{key}")
        return value

    return {
        "ENV_NAME": env_name,
        "TOKEN_URL": env("TOKEN_URL"),
        "API_VALIDATE_URL": env("API_VALIDATE_URL"),
        "USERNAME": env("USERNAME"),
        "PASSWORD": env("PASSWORD"),
        "CLIENT_ID": env("CLIENT_ID"),
        "CLIENT_SECRET": env("CLIENT_SECRET"),
        "TENANT_NAME": env("TENANT_NAME"),
    }


class TokenManager:
    def __init__(self, config: Dict[str, str]):
        self.config = config
        self.env_name = config["ENV_NAME"]
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_timestamp: Optional[int] = None
        self.expires_in: Optional[int] = None
        self._cache_path = CACHE_DIR / f"token_cache_{self.env_name.lower()}.json"
        self._lock_path = CACHE_DIR / f"token_cache_{self.env_name.lower()}.lock"
        self._load_cache()

    def _load_cache(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            with file_lock(self._lock_path):
                with self._cache_path.open("r", encoding="utf-8") as cache_file:
                    data = json.load(cache_file)
            if data.get("env_name") != self.env_name:
                return
            self.access_token = data.get("access_token")
            self.refresh_token = data.get("refresh_token")
            self.token_timestamp = data.get("token_timestamp")
            self.expires_in = data.get("expires_in")
        except Exception as exc:
            logging.warning("Could not load token cache: %s", exc)

    def _save_cache(self) -> None:
        payload = {
            "env_name": self.env_name,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_timestamp": self.token_timestamp,
            "expires_in": self.expires_in,
        }
        try:
            with file_lock(self._lock_path):
                temp_path = self._cache_path.with_suffix(".json.tmp")
                with temp_path.open("w", encoding="utf-8") as cache_file:
                    json.dump(payload, cache_file, indent=2)
                temp_path.replace(self._cache_path)
        except Exception as exc:
            logging.warning("Could not save token cache: %s", exc)

    def _is_token_expired(self) -> bool:
        if not self.token_timestamp or not self.expires_in:
            return True
        return self.expires_in - (int(time.time()) - self.token_timestamp) <= TOKEN_REFRESH_BUFFER

    def _request_new_access_token(self) -> None:
        data = {
            "grant_type": "password",
            "username": f"{self.config['USERNAME']}@{self.config['TENANT_NAME']}",
            "password": self.config["PASSWORD"],
            "client_id": self.config["CLIENT_ID"],
            "client_secret": self.config["CLIENT_SECRET"],
        }
        response = requests.post(self.config["TOKEN_URL"], data=data, timeout=20)
        if response.status_code != 200:
            raise RuntimeError(f"Access token request failed with HTTP {response.status_code}.")
        payload = response.json()
        self.access_token = payload.get("access_token")
        self.refresh_token = payload.get("refresh_token")
        self.expires_in = payload.get("expires_in")
        self.token_timestamp = int(time.time())
        if not self.access_token or not self.expires_in:
            raise RuntimeError("Token response did not contain access_token and expires_in.")
        self._save_cache()

    def _refresh_access_token(self) -> None:
        if not self.refresh_token:
            self._request_new_access_token()
            return
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.config["CLIENT_ID"],
            "client_secret": self.config["CLIENT_SECRET"],
        }
        response = requests.post(self.config["TOKEN_URL"], data=data, timeout=20)
        if response.status_code != 200:
            self._request_new_access_token()
            return
        payload = response.json()
        self.access_token = payload.get("access_token")
        self.refresh_token = payload.get("refresh_token", self.refresh_token)
        self.expires_in = payload.get("expires_in")
        self.token_timestamp = int(time.time())
        if not self.access_token or not self.expires_in:
            self._request_new_access_token()
            return
        self._save_cache()

    def get_access_token(self) -> str:
        if not self.access_token:
            self._request_new_access_token()
        elif self._is_token_expired():
            self._refresh_access_token()
        return self.access_token or ""

    def refresh_access_token(self) -> str:
        """Refresh after an API response rejects an otherwise-current token."""
        self._refresh_access_token()
        return self.access_token or ""

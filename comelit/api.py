"""The Comelit cloud 'ccapi' JSON envelope used by every /servicerest/ call.

Request shape:
  {"ccapi":{"version":"1.1.0",
            "login":{"bearer":"<access_token>"},
            "endpoint":{"requuid":"<rand>","service":"jfs/get","version":"1.1.0"},
            "body":{...}}}
The response mirrors it; real payload is under ccapi.body, status under ccapi.error.
"""
from __future__ import annotations
import random
import requests
from .auth import Auth

BASE = "https://api.comelitgroup.com/servicerest"
UA = "ktor-client"


class CcApiError(RuntimeError):
    def __init__(self, error: dict):
        self.error = error
        super().__init__(f"ccapi error code={error.get('code')} action={error.get('action')} "
                         f"msg={error.get('message')!r}")


class CcApi:
    def __init__(self, auth: Auth, session: requests.Session | None = None):
        self.auth = auth
        self.s = session or requests.Session()

    def call(self, service: str, body: dict, version: str = "1.1.0") -> dict:
        env = {"ccapi": {
            "version": version,
            "login": {"bearer": self.auth.bearer},
            "endpoint": {"requuid": str(random.randint(1, 2**31)),
                         "service": service, "version": version},
            "body": body,
        }}
        r = self.s.post(f"{BASE}/{service}", json=env,
                        headers={"user-agent": UA, "accept": "application/json"}, timeout=20)
        r.raise_for_status()
        cc = r.json()["ccapi"]
        err = cc.get("error") or {}
        if err.get("code", 0) != 0:
            raise CcApiError(err)
        return cc["body"]

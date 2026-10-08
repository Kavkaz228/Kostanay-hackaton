"""Destructive QA for a NEW, explicitly marked local release-check installation.

Uses only Python's standard library. Never run against an operating installation.
The marker, fixed loopback port, temporary admin password and empty-database
preconditions are intentional: this script changes a password and adds QA data.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
from pathlib import Path
import secrets
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener


ROBOT = "RELEASE-CHECK-ONLY"
MARKER = "ALLUR_RELEASE_CHECK_ONLY"


class CheckFailure(RuntimeError):
    pass


def require(condition, label):
    if not condition:
        raise CheckFailure(label)


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ""

    def call(self, method, path, body=None, *, headers=None, expected=200, timeout=30):
        request_headers = {"Content-Type": "application/json"}
        if self.csrf:
            request_headers["X-CSRF-Token"] = self.csrf
        request_headers.update(headers or {})
        request = Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                          method=method, headers=request_headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                status, raw = response.status, response.read()
        except HTTPError as error:
            status, raw = error.code, error.read()
        # Do not echo response bodies: auth responses contain temporary secrets.
        require(status == expected, f"{method} {path}: HTTP {status}, expected {expected}")
        return json.loads(raw) if raw else None

    def login(self, password):
        result = self.call("POST", "/api/auth/login", {"username": "admin", "password": password})
        self.csrf = result["csrf"]
        return result


def await_alert(client, status):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = client.call("GET", "/api/monitoring")
        matches = [a for a in result["alerts"] if a["robot_id"] == ROBOT
                   and a["type"].startswith("limit:joint_temperature_c") and a["status"] == status]
        if matches:
            return matches[0]
        time.sleep(1)
    raise CheckFailure(f"Background monitoring did not produce {status} alert")


def verify(client):
    stats = client.call("GET", "/api/admin/system")
    require(stats["robot_measurements"] == 2, "Expected exactly two isolated QA measurements")
    config = client.call("GET", "/api/robots/config?" + urlencode({"robot_id": ROBOT}))
    require(config["limits"]["joint_temperature_c"]["high_critical"] == 80, "Robot config not persisted")
    monitor = client.call("GET", "/api/monitoring")
    alerts = [a for a in monitor["alerts"] if a["robot_id"] == ROBOT and a["type"].startswith("limit:")]
    require(any(a["status"] == "resolved" and a["acknowledged"] for a in alerts),
            "Acknowledged recovery history not persisted")
    require(not client.call("GET", "/api/automation")["commands"], "Unexpected physical commands")
    return {"measurements": 2, "persisted_config": True, "persisted_recovery": True, "physical_commands": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Only http://127.0.0.1:8094 or http://localhost:8094")
    parser.add_argument("--installation-root", type=Path, required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--verify-existing", action="store_true", help="After restart/restore: read-only data checks")
    parser.add_argument("--require-ai", action="store_true")
    parser.add_argument("--inference", action="store_true", help="Also run a real local-model request, up to 180 seconds")
    args = parser.parse_args()
    location = urlsplit(args.url)
    require(location.scheme == "http" and location.hostname in ("localhost", "127.0.0.1")
            and location.port == 8094 and location.path in ("", "/")
            and not location.username and not location.password and not location.query and not location.fragment,
            "Refusing target: this QA script is restricted to local port 8094")
    root = args.installation_root.resolve(strict=True)
    marker = root / ".release-check-only"
    require(marker.is_file() and marker.read_text(encoding="utf-8-sig").strip() == MARKER,
            "Missing explicit .release-check-only marker")
    password_path = args.password_file.resolve(strict=True)
    require(password_path.is_relative_to(root), "Password file must belong to this isolated installation")
    client = Client(args.url)
    require(client.call("GET", "/api/health")["status"] == "ok", "Health check failed")
    client.call("GET", "/api/state", expected=401)
    identity = client.login(password_path.read_text(encoding="utf-8-sig").strip())
    if args.verify_existing:
        require(not identity["must_change"], "Existing QA account still requires a password change")
        result = {"status": "passed", "mode": "verify-existing", **verify(client)}
        print(json.dumps(result, ensure_ascii=False))
        return
    require(identity["must_change"] is True and identity["role"] == "admin",
            "Refusing mutation: expected a fresh bootstrap administrator")
    password = secrets.token_urlsafe(36)
    new_password_path = root / ".release-check-password"
    require(not new_password_path.exists(), "QA password already exists; use --verify-existing after a successful run")
    # Save before changing so an interrupted request never loses the new secret.
    with new_password_path.open("x", encoding="utf-8") as stream:
        stream.write(password)
    new_password_path.chmod(0o600)
    client.call("POST", "/api/auth/password", {
        "current_password": password_path.read_text(encoding="utf-8-sig").strip(), "new_password": password})
    client.csrf = ""
    require(not client.login(password)["must_change"], "Password change did not persist")
    require(client.call("GET", "/api/admin/system")["robot_measurements"] == 0,
            "Refusing QA data: installation already contains robot measurements")
    automation = client.call("GET", "/api/automation")
    require(not automation["robots"] and not automation["commands"] and not automation["physical_enabled"],
            "Refusing QA data: installation is not empty or physical control is enabled")
    plant = client.call("GET", "/api/ai/context?data_source=plant")
    require(plant["has_data"] is False, "Fresh plant context must have no measured data")
    stand = client.call("GET", "/api/ai/context?data_source=emulation&robot_id=R2")
    require(stand["has_data"] and stand["readings"], "Training stand context has no readings")
    simulation = client.call("GET", "/api/ai/context?data_source=simulation")
    require(simulation["has_data"] and simulation["metrics"], "Simulation context has no metrics")
    ai = client.call("GET", "/api/ai/status")
    if args.require_ai or args.inference:
        require(ai["ready"] and ai["local_only"], "Local AI model is not ready")
    inference = None
    if args.inference:
        started = time.monotonic()
        answer = client.call("POST", "/api/ai/analyze", {
            "question": "Какие отклонения есть у R2 и что проверить сотруднику?",
            "data_source": "emulation", "robot_id": "R2", "allow_command": False}, timeout=180)
        require(answer["answer_kind"] == "model" and answer["facts"]["readings"]
                and answer["recommendations"] and answer["proposal"] is None,
                "Local AI response did not contain facts and recommendations")
        inference = {"model": answer["model"], "seconds": round(time.monotonic()-started, 2),
                     "recommendations": len(answer["recommendations"])}
    config = {"expected_interval_seconds": 3600, "limits": {
        "joint_temperature_c": {"high_warning": 70, "high_critical": 80}}, "error_codes": {}}
    token = client.call("POST", "/api/admin/tokens", {"name": "Isolated release QA", "days": 1})
    integration = Client(args.url)
    integration.call("GET", "/api/state", headers={"Authorization": "Bearer " + token["token"]}, expected=403)
    headers = {"Authorization": "Bearer " + token["token"], "Idempotency-Key": "release-check-hot-0001"}
    packet = {"measurements": [{"timestamp": datetime.now(timezone.utc).isoformat(), "robot_id": ROBOT,
        "cycle_status": "running", "line_section": "Isolated release QA", "joint_temperature_c": 85.0}]}
    receipt = integration.call("POST", "/api/robots/measurements", packet, headers=headers)
    require(integration.call("POST", "/api/robots/measurements", packet, headers=headers) == receipt,
            "Idempotent retry did not return the same receipt")
    require(client.call("GET", "/api/admin/system")["robot_measurements"] == 1, "Retry inserted duplicate data")
    client.call("PUT", "/api/robots/config?" + urlencode({"robot_id": ROBOT}), config)
    alert = await_alert(client, "active")
    require(alert["severity"] == "critical" and alert["recommendation"], "No critical alert with recommendation")
    client.call("POST", f"/api/monitoring/{alert['id']}/acknowledge")
    packet["measurements"][0].update(timestamp=datetime.now(timezone.utc).isoformat(), joint_temperature_c=45.0)
    headers["Idempotency-Key"] = "release-check-normal-0002"
    integration.call("POST", "/api/robots/measurements", packet, headers=headers)
    recovered = await_alert(client, "resolved")
    require(recovered["id"] == alert["id"], "Recovery did not close the original incident")
    client.call("DELETE", f"/api/admin/tokens/{token['id']}")
    result = {"status": "passed", "mode": "fresh-installation", "source_isolation": True,
              "idempotency": True, "ai_ready": ai["ready"], "inference": inference, **verify(client)}
    (root / ".release-check-result.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (CheckFailure, OSError, ValueError, KeyError) as error:
        print(f"Release check failed: {error}", file=sys.stderr)
        sys.exit(1)

import importlib.util
from pathlib import Path
import sys


spec = importlib.util.spec_from_file_location(
    "configure_github", Path(__file__).parents[1] / "scripts" / "configure_github.py"
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def configure(monkeypatch, app_id=None, existing=False):
    calls = []

    def fake_api(path, method="GET", payload=None):
        calls.append((path, method, payload))
        if method == "GET" and path.endswith("deployment-branch-policies"):
            return {"branch_policies": [{"id": 1, "name": "main", "type": "branch"}]}
        if method == "GET" and path.endswith("rulesets"):
            return [
                {"id": i, "name": name}
                for i, name in enumerate(
                    ["notes-permanent-branches", "notes-main-review", "notes-content-writer"], 1
                )
            ] if existing else []
        return {}

    monkeypatch.setattr(setup, "api", fake_api)
    monkeypatch.setattr(sys, "argv", ["configure_github.py"] + (["--app-id", str(app_id)] if app_id else []))
    setup.main()
    return calls


def test_writer_cannot_bypass_deletion_or_main_checks(monkeypatch):
    calls = configure(monkeypatch, app_id=123)
    rules = {body["name"]: body for _, method, body in calls if method == "POST"}
    assert rules["notes-content-writer"]["bypass_actors"] == [
        {"actor_id": 123, "actor_type": "Integration", "bypass_mode": "always"}
    ]
    permanent = rules["notes-permanent-branches"]
    assert permanent["bypass_actors"] == []
    assert {r["type"] for r in permanent["rules"]} == {"deletion", "non_fast_forward"}
    assert set(permanent["conditions"]["ref_name"]["include"]) == {"refs/heads/main", "refs/heads/content"}
    assert rules["notes-main-review"]["bypass_actors"] == []


def test_missing_app_blocks_content_writes(monkeypatch):
    calls = configure(monkeypatch)
    writer = next(body for _, method, body in calls if method == "POST" and body["name"] == "notes-content-writer")
    assert writer["bypass_actors"] == []
    assert {r["type"] for r in writer["rules"]} == {"creation", "update"}


def test_repeated_setup_updates_existing_rulesets(monkeypatch):
    calls = configure(monkeypatch, app_id=123, existing=True)
    assert not any(method == "POST" for _, method, _ in calls)
    updates = [path for path, method, _ in calls if method == "PUT" and "/rulesets/" in path]
    assert len(updates) == 3

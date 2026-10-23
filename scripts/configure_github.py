"""Configure the notes repository's environment and branch rules with gh."""

import argparse
import json
import subprocess


def api(path, method="GET", payload=None):
    args = ["gh", "api", path, "--method", method]
    if payload is not None:
        args += ["--input", "-"]
    result = subprocess.run(
        args, input=json.dumps(payload) if payload is not None else None,
        text=True, encoding="utf-8", capture_output=True, check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="nordicintel/notes")
    parser.add_argument("--app-id", type=int, help="Writer App ID, not installation ID")
    args = parser.parse_args()
    base = f"repos/{args.repo}"
    api(f"{base}/environments/notes-writer", "PUT", {
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}
    })
    policies_path = f"{base}/environments/notes-writer/deployment-branch-policies"
    policies = api(policies_path)["branch_policies"]
    if not any(p["name"] == "main" and p.get("type", "branch") == "branch" for p in policies):
        api(policies_path, "POST", {"name": "main", "type": "branch"})
    for policy in policies:
        if policy["name"] != "main" or policy.get("type", "branch") != "branch":
            api(f"{policies_path}/{policy['id']}", "DELETE")

    rulesets = [
        ("notes-permanent-branches", ["main", "content"], [], [
            {"type": "deletion"}, {"type": "non_fast_forward"},
        ]),
        ("notes-main-review", ["main"], [], [
            {"type": "pull_request", "parameters": {
                "required_approving_review_count": 0,
                "dismiss_stale_reviews_on_push": True,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_review_thread_resolution": True,
            }},
            {"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [{"context": "test (ubuntu-latest)"}, {"context": "test (windows-latest)"}],
            }},
        ]),
        ("notes-content-writer", ["content"],
         [{"actor_id": args.app_id, "actor_type": "Integration", "bypass_mode": "always"}] if args.app_id else [],
         [{"type": "creation"}, {"type": "update"}]),
    ]
    existing = {r["name"]: r["id"] for r in api(f"{base}/rulesets")}
    for name, branches, bypass, rules in rulesets:
        payload = {
            "name": name, "target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": [f"refs/heads/{b}" for b in branches], "exclude": []}},
            "bypass_actors": bypass, "rules": rules,
        }
        suffix = f"/{existing[name]}" if name in existing else ""
        api(f"{base}/rulesets{suffix}", "PUT" if suffix else "POST", payload)
        print(f"Configured {name}")
    if args.app_id is None:
        print("Content creation and updates are blocked until rerun with --app-id.")


if __name__ == "__main__":
    main()

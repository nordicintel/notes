"""Trusted Actions entry point; no shell evaluation of request inputs."""

import json
import os
from pathlib import Path

from .model import NotesError, require
from .storage import snapshot


def main():
    try:
        require(os.environ.get("GITHUB_REF") == "refs/heads/main", "untrusted_ref", "Writer must run from main")
        operation = os.environ["NOTES_OPERATION"]
        payload = json.loads(os.environ["NOTES_PAYLOAD"])
        remote = os.environ["NOTES_REMOTE"]
        with snapshot(remote, bootstrap=operation == "bootstrap") as store:
            result = store.transact(operation, payload, os.environ.get("GITHUB_ACTOR", ""),
                                    os.environ.get("GITHUB_RUN_ID", ""))
    except NotesError as exc:
        result = exc.as_dict()
    except (ValueError, KeyError, TypeError):
        result = NotesError("invalid_payload", "Malformed workflow input").as_dict()
    Path("result.json").write_text(json.dumps(result, ensure_ascii=True), encoding="utf-8")
    return 1 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""JSON CLI; local reads and Actions-dispatched mutations."""

import argparse
import io
import json
import re
import sys
import time
import uuid
import zipfile
from pathlib import Path

from .model import NotesError, create_note, require, valid_id, valid_status
from .storage import run, snapshot

API_VERSION = "2026-03-10"


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise NotesError("invalid_arguments", message)


def parser():
    root = Parser(description="Agent notes API (JSON output)")
    root.add_argument("--repo", default="nordicintel/notes")
    root.add_argument("--timeout", type=float, default=600)
    commands = root.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    add.add_argument("--type", required=True)
    add.add_argument("--status", default="open")
    sources = add.add_mutually_exclusive_group(required=True)
    sources.add_argument("--file")
    sources.add_argument("--content")
    status = commands.add_parser("set-status")
    status.add_argument("ids", nargs="+")
    status.add_argument("--status", required=True)
    delete = commands.add_parser("delete")
    delete.add_argument("ids", nargs="+")
    for command in (add, status, delete):
        command.add_argument("--request-id")
    listing = commands.add_parser("list")
    listing.add_argument("--type")
    listing.add_argument("--status")
    listing.add_argument("--from", dest="date_from")
    listing.add_argument("--to", dest="date_to")
    get = commands.add_parser("get")
    get.add_argument("ids", nargs="+")
    return root


def gh_api(path, data=None):
    args = ["gh", "api", "-H", f"X-GitHub-Api-Version: {API_VERSION}", path]
    if data is not None:
        args += ["--method", "POST", "--input", "-"]
    return run(args, data=json.dumps(data, ensure_ascii=False).encode() if data is not None else None,
               timeout=30)


def dispatch(repo, operation, payload, timeout):
    request_id = payload["request_id"]
    inputs = {"operation": operation, "payload": json.dumps(payload, ensure_ascii=False)}
    require(len(json.dumps(inputs, ensure_ascii=False).encode()) <= 65000,
            "payload_too_large", "Workflow inputs exceed the 65 KB dispatch limit")
    print(f"Request {request_id}: dispatching {operation}", file=sys.stderr)
    run_url = None
    cause = None
    try:
        raw_response = gh_api(f"repos/{repo}/actions/workflows/notes-writer.yml/dispatches",
                              {"ref": "main", "inputs": inputs})
    except NotesError as exc:
        http = re.search(r"\(HTTP (\d{3})\)", str(exc))
        rejected = http and int(http[1]) in {400, 401, 403, 404, 405, 413, 422, 429}
        if exc.code == "command_unavailable" or exc.details.get("exit_code") == 4 or rejected:
            raise NotesError("dispatch_rejected", str(exc), request_id=request_id,
                             cause=exc.as_dict()["error"]) from exc
        raise NotesError("outcome_unknown", "Dispatch was not confirmed; retry with the same input and request ID",
                         request_id=request_id, run_url=None, cause=exc.as_dict()["error"]) from exc
    try:
        response = json.loads(raw_response)
        run_id = response["workflow_run_id"]
        run_url = response.get("html_url", f"https://github.com/{repo}/actions/runs/{run_id}")
        print(run_url, file=sys.stderr)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = json.loads(gh_api(f"repos/{repo}/actions/runs/{run_id}"))
            if state["status"] == "completed":
                artifacts = json.loads(gh_api(f"repos/{repo}/actions/runs/{run_id}/artifacts"))["artifacts"]
                artifact = next((a for a in artifacts if a["name"] == "notes-result" and not a["expired"]), None)
                if artifact is None:
                    break
                raw = gh_api(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip")
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    return json.loads(archive.read("result.json"))
            time.sleep(min(3, max(0, deadline - time.monotonic())))
    except NotesError as exc:
        cause = exc.as_dict()["error"]
    except (ValueError, KeyError, zipfile.BadZipFile) as exc:
        cause = {"code": "invalid_response", "message": str(exc)}
    raise NotesError("outcome_unknown", "Write outcome is unknown; retry with the same input and request ID",
                     request_id=request_id, run_url=run_url, cause=cause)


def execute(args):
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo), "invalid_repo", "Use owner/repository")
    require(args.timeout > 0, "invalid_timeout", "Timeout must be positive")
    if args.command in {"list", "get"}:
        with snapshot(f"https://github.com/{args.repo}.git") as store:
            if args.command == "get":
                return store.get(args.ids)
            return store.list(args.type, args.status, args.date_from, args.date_to)
    payload = {"request_id": args.request_id or str(uuid.uuid4())}
    if args.command == "add":
        try:
            if args.file == "-":
                body = sys.stdin.buffer.read().decode("utf-8")
            elif args.file is not None:
                body = Path(args.file).read_bytes().decode("utf-8")
            else:
                body = args.content
        except (OSError, UnicodeError) as exc:
            raise NotesError("invalid_input", "Cannot read input as UTF-8 Markdown") from exc
        create_note(args.type, args.status, body)
        payload.update(type=args.type, status=args.status, body=body)
    else:
        for note_id in args.ids:
            valid_id(note_id)
        payload["ids"] = args.ids
        if args.command == "set-status":
            payload["status"] = valid_status(args.status)
    return dispatch(args.repo, args.command, payload, args.timeout)


def main():
    try:
        result = execute(parser().parse_args())
    except NotesError as exc:
        result = exc.as_dict()
    print(json.dumps(result, ensure_ascii=True))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())

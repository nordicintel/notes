"""Isolated Git object snapshots and atomic ledger transactions."""

import hashlib
import json
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from .model import NotesError, create_note, decode_note, encode_note, headings, require, valid_id, valid_status, valid_type


def run(args, *, cwd=None, data=None, timeout=None):
    try:
        result = subprocess.run(args, cwd=cwd, input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise NotesError("command_timeout", f"{args[0]} did not finish in time") from exc
    except OSError as exc:
        raise NotesError("command_unavailable", f"Cannot execute {args[0]}") from exc
    if result.returncode:
        raise NotesError("command_failed", result.stderr.decode("utf-8", "replace").strip(),
                         exit_code=result.returncode)
    return result.stdout


class Snapshot:
    def __init__(self, path, remote):
        self.path = path
        self.remote = remote
        self.commit = None

    def git(self, *args, data=None):
        return run(["git", *args], cwd=self.path, data=data)

    def fetch(self, bootstrap=False):
        refs = self.git("ls-remote", "--heads", self.remote, "refs/heads/content").strip()
        if not refs:
            require(bootstrap, "missing_content", "The content branch has not been bootstrapped")
            return
        self.git("fetch", "--no-tags", self.remote, "refs/heads/content")
        self.commit = self.git("rev-parse", "FETCH_HEAD").decode().strip()

    def ids(self):
        if not self.commit:
            return []
        names = self.git("ls-tree", "-r", "--name-only", "-z", self.commit).decode().strip("\0")
        return sorted(names.split("\0")) if names else []

    def note(self, note_id):
        valid_id(note_id)
        require(note_id in self.ids(), "missing_note", f"Note does not exist: {note_id}")
        return decode_note(self.git("show", f"{self.commit}:{note_id}"))

    def get(self, ids):
        result = []
        for note_id in ids:
            metadata, body = self.note(note_id)
            result.append({**metadata, "body": body, "character_count": len(body),
                           "byte_count": len(body.encode("utf-8")), "headings": headings(body)})
        return {"commit": self.commit, "notes": result}

    def list(self, note_type=None, status=None, date_from=None, date_to=None):
        if note_type:
            valid_type(note_type)
        if status:
            valid_status(status)
        for value in (date_from, date_to):
            require(value is None or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value),
                    "invalid_date", "Use ISO dates: YYYY-MM-DD")
        try:
            start = date.fromisoformat(date_from) if date_from else None
            end = date.fromisoformat(date_to) if date_to else None
        except ValueError as exc:
            raise NotesError("invalid_date", "Use ISO dates: YYYY-MM-DD") from exc
        require(not (start and end and start > end), "invalid_date", "Start date exceeds end date")
        result = []
        for note_id in self.ids():
            metadata, _ = self.note(note_id)
            created = date.fromisoformat(metadata["created_at"][:10])
            if ((not note_type or metadata["type"] == note_type)
                and (not status or metadata["status"] == status)
                and (not start or created >= start) and (not end or created <= end)):
                result.append({key: metadata[key] for key in ("id", "title", "type", "status", "created_at")})
        return {"commit": self.commit, "notes": result}

    def transact(self, operation, payload, actor="", workflow_run="", now=None):
        require(operation in {"bootstrap", "add", "set-status", "delete"}, "invalid_operation", "Unknown operation")
        require(isinstance(payload, dict), "invalid_payload", "Payload must be an object")
        request_id = payload.get("request_id")
        require(isinstance(request_id, str) and 1 <= len(request_id) <= 128
                and all(c.isalnum() or c in "-_" for c in request_id), "invalid_request_id", "Invalid request ID")
        allowed = {"bootstrap": {"request_id"}, "add": {"request_id", "type", "status", "body"},
                   "set-status": {"request_id", "ids", "status"}, "delete": {"request_id", "ids"}}[operation]
        require(set(payload) == allowed, "invalid_payload", "Unexpected or missing payload fields")
        digest = hashlib.sha256(json.dumps({"operation": operation, "payload": payload},
                                          sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if self.commit:
            for commit in self.git("rev-list", self.commit).decode().splitlines():
                message = self.git("show", "-s", "--format=%B", commit).decode()
                for line in message.splitlines():
                    if line.startswith("Notes-Ledger: "):
                        record = json.loads(line[len("Notes-Ledger: "):])
                        if record["request_id"] == request_id:
                            require(record["payload_hash"] == digest, "request_conflict", "Request ID was used with a different payload")
                            return {**record["result"], "commit": commit}
        changes = {}
        if operation == "bootstrap":
            require(self.commit is None, "already_initialized", "Content already exists")
            result = {"operation": operation, "request_id": request_id, "ids": []}
        elif operation == "add":
            metadata, raw = create_note(payload["type"], payload["status"], payload["body"], now)
            note_id = metadata["id"]
            history = self.git("log", "--format=%H", self.commit, "--", note_id) if self.commit else b""
            require(not history.strip(), "id_reserved", f"Note ID is already reserved: {note_id}")
            changes[note_id] = raw
            result = {"operation": operation, "request_id": request_id, "notes": [metadata]}
        else:
            ids = payload["ids"]
            require(isinstance(ids, list) and len(ids) > 0 and all(isinstance(x, str) for x in ids), "invalid_ids", "Provide at least one ID")
            require(len(ids) == len(set(ids)), "invalid_ids", "Duplicate IDs are not allowed")
            if operation == "set-status":
                valid_status(payload["status"])
            result_notes = []
            for note_id in ids:
                metadata, body = self.note(note_id)
                if operation == "delete":
                    changes[note_id] = None
                else:
                    metadata["status"] = payload["status"]
                    changes[note_id] = encode_note(metadata, body)
                    result_notes.append(metadata)
            result = {"operation": operation, "request_id": request_id, "ids": ids}
            if operation == "set-status":
                result["notes"] = result_notes
        if self.commit:
            self.git("read-tree", self.commit)
        for note_id, raw in changes.items():
            if raw is None:
                self.git("update-index", "--index-info", data=f"0 {'0' * 40}\t{note_id}\n".encode())
            else:
                blob = self.git("hash-object", "-w", "--stdin", data=raw).decode().strip()
                self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{note_id}")
        tree = self.git("write-tree").decode().strip()
        record = {"request_id": request_id, "payload_hash": digest, "actor": actor,
                  "workflow_run": workflow_run, "result": result}
        message = f"notes: {operation}\n\nNotes-Ledger: {json.dumps(record, ensure_ascii=False)}\n"
        parents = ["-p", self.commit] if self.commit else []
        commit = self.git("-c", "user.name=Notes API", "-c", "user.email=notes-api@users.noreply.github.com",
                          "commit-tree", tree, *parents, data=message.encode()).decode().strip()
        try:
            self.git("push", self.remote, f"{commit}:refs/heads/content")
        except NotesError as exc:
            raise NotesError("outcome_unknown", "Push did not confirm success; retry the same request",
                             request_id=request_id) from exc
        self.commit = commit
        return {**result, "commit": commit}


@contextmanager
def snapshot(remote, bootstrap=False):
    with tempfile.TemporaryDirectory(prefix="notes-api-") as directory:
        store = Snapshot(Path(directory), remote)
        store.git("init", "--bare", "--quiet")
        store.fetch(bootstrap)
        yield store

import io
import json
from pathlib import Path
import shutil
from datetime import datetime, timezone

import pytest

from notes_api import cli
from notes_api.model import NotesError, create_note, decode_note, headings
from notes_api.storage import run, snapshot

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
BODY = '# Café "quoted": title\r\n\r\n## Detail\r\nText 🐈\r\n```md\r\n# Not a heading\r\n```\r\n'


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote.git"
    run(["git", "init", "--bare", "--quiet", str(path)])
    with snapshot(str(path), bootstrap=True) as store:
        store.transact("bootstrap", {"request_id": "bootstrap"})
    return str(path)


def add(store, title="First", request="add", status="open"):
    return store.transact("add", {"request_id": request, "type": "note", "status": status,
                                  "body": f"# {title}\r\nBody\r\n"}, now=NOW)


def test_model_preserves_body_and_extracts_real_headings():
    metadata, raw = create_note("note", "open", BODY, NOW)
    assert metadata["id"] == "note/2026-10-23_cafe_quoted_title.md"
    assert decode_note(raw) == (metadata, BODY)
    assert headings(BODY) == [{"level": 1, "text": 'Café "quoted": title'}, {"level": 2, "text": "Detail"}]


def test_inline_markup_title_and_utc():
    now = datetime.fromisoformat("2026-10-24T00:30:00+02:00")
    metadata, _ = create_note("idea", "open", "# A **bold** `code` [link](url)", now)
    assert metadata["title"] == "A bold code link"
    assert metadata["id"] == "idea/2026-10-23_a_bold_code_link.md"


@pytest.mark.parametrize("body", ["No heading", "```\n# Hidden\n```", "# 😃"])
def test_missing_usable_title(body):
    with pytest.raises(NotesError, match="H1"):
        create_note("note", "open", body, NOW)


def test_transactions_retry_original_result_and_history(remote):
    with snapshot(remote) as store:
        result = add(store)
        note_id = result["notes"][0]["id"]
        update = {"request_id": "status", "ids": [note_id], "status": "done"}
        changed = store.transact("set-status", update)
        assert changed["commit"] != result["commit"]
        assert store.get([note_id])["notes"][0]["body"] == "# First\r\nBody\r\n"
        assert add(store) == result
        with pytest.raises(NotesError) as error:
            add(store, title="Different")
        assert error.value.code == "request_conflict"
        no_op = store.transact("set-status", {**update, "request_id": "no-op"})
        assert no_op["commit"] != changed["commit"]
        assert store.git("rev-parse", no_op["commit"] + "^{tree}") == store.git("rev-parse", changed["commit"] + "^{tree}")
        store.transact("delete", {"request_id": "delete", "ids": [note_id]})
        assert store.list()["notes"] == []
        assert add(store) == result
        with pytest.raises(NotesError) as error:
            add(store, request="new-id")
        assert error.value.code == "id_reserved"
        assert len(store.git("rev-list", store.commit).splitlines()) == 5
    with snapshot(remote) as fresh:
        assert add(fresh) == result


@pytest.mark.parametrize("operation", ["delete", "set-status"])
def test_batch_failure_is_atomic(remote, operation):
    with snapshot(remote) as store:
        note_id = add(store)["notes"][0]["id"]
        before = store.commit
        payload = {"request_id": "batch", "ids": [note_id, "note/2026-10-23_missing.md"]}
        if operation == "set-status":
            payload["status"] = "done"
        with pytest.raises(NotesError) as error:
            store.transact(operation, payload)
        assert error.value.code == "missing_note"
        assert store.commit == before
        assert store.get([note_id])["notes"][0]["status"] == "open"
    with snapshot(remote) as fresh:
        assert fresh.commit == before


def test_listing_filters_and_get_order(remote):
    with snapshot(remote) as store:
        first = add(store)["notes"][0]["id"]
        second = add(store, "Second", "second", "done")["notes"][0]["id"]
        assert [n["id"] for n in store.list()["notes"]] == [first, second]
        assert [n["id"] for n in store.get([second, first])["notes"]] == [second, first]
        assert len(store.list("note", "done", "2026-10-23", "2026-10-23")["notes"]) == 1
        assert store.list(date_from="2026-10-24")["notes"] == []
        assert store.list(note_type="idea")["notes"] == []
        with pytest.raises(NotesError):
            store.list(date_from="2026-10-24", date_to="2026-10-23")
        note = store.get([first])["notes"][0]
        assert note["character_count"] == len(note["body"])
        assert note["byte_count"] == len(note["body"].encode())


def test_snapshot_does_not_change_after_other_writer(remote):
    with snapshot(remote) as before:
        original = before.commit
        with snapshot(remote) as writer:
            add(writer)
        assert before.commit == original
        assert before.list()["notes"] == []


@pytest.mark.parametrize("bad_id", ["../main", "note/../../README.md", "--help", "note\\x.md"])
def test_unsafe_ids(remote, bad_id):
    with snapshot(remote) as store:
        with pytest.raises(NotesError) as error:
            store.get([bad_id])
        assert error.value.code == "invalid_id"


@pytest.mark.parametrize("mode", ["relative", "absolute", "inline", "stdin"])
def test_cli_sources_preserve_bytes(tmp_path, monkeypatch, mode):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "my source.md"
    source.write_bytes(BODY.encode())
    monkeypatch.setattr(cli, "dispatch", lambda repo, operation, payload, timeout: payload)
    options = {"relative": ["--file", source.name], "absolute": ["--file", str(source)],
               "inline": ["--content", BODY], "stdin": ["--file", "-"]}
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(BODY.encode()), encoding="utf-8"))
    result = cli.execute(cli.parser().parse_args(["add", "--type", "note", *options[mode]]))
    assert result["body"] == BODY
    assert source.read_bytes() == BODY.encode()


@pytest.mark.parametrize("options", [[], ["--file", "a", "--content", "b"]])
def test_input_source_required_and_exclusive(options):
    with pytest.raises(NotesError):
        cli.parser().parse_args(["add", "--type", "note", *options])


def test_unreadable_and_non_utf8_input(tmp_path):
    source = tmp_path / "missing.md"
    for exists in [False, True]:
        if exists:
            source.write_bytes(b"\xff")
        with pytest.raises(NotesError) as error:
            cli.execute(cli.parser().parse_args(["add", "--type", "note", "--file", str(source)]))
        assert error.value.code == "invalid_input"


def test_dispatch_result_and_unknown(monkeypatch):
    import zipfile
    output = io.BytesIO()
    result = {"commit": "123", "request_id": "test"}
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("result.json", json.dumps(result))
    responses = iter([json.dumps({"workflow_run_id": 1}).encode(), b'{"status":"completed"}',
                      b'{"artifacts":[{"name":"notes-result","expired":false,"id":2}]}', output.getvalue()])
    monkeypatch.setattr(cli, "gh_api", lambda *args: next(responses))
    assert cli.dispatch("owner/repo", "delete", {"request_id": "test"}, 1) == result
    monkeypatch.setattr(cli, "gh_api", lambda *args: (_ for _ in ()).throw(NotesError("network", "failed")))
    with pytest.raises(NotesError) as error:
        cli.dispatch("owner/repo", "delete", {"request_id": "test"}, 1)
    assert error.value.code == "outcome_unknown"
    assert error.value.details["request_id"] == "test"


def test_failed_push_does_not_publish_partial_transaction(remote, monkeypatch):
    with snapshot(remote) as store:
        before = store.commit
        original = store.git
        def fail_push(*args, **kwargs):
            if args[0] == "push":
                raise NotesError("command_failed", "connection failed")
            return original(*args, **kwargs)
        monkeypatch.setattr(store, "git", fail_push)
        with pytest.raises(NotesError):
            add(store)
    with snapshot(remote) as fresh:
        assert fresh.commit == before
        assert fresh.list()["notes"] == []
        assert add(fresh)["notes"][0]["title"] == "First"


def test_uv_project_keeps_input_relative_to_caller(tmp_path):
    project = Path(__file__).resolve().parents[1]
    (tmp_path / "caller note.md").write_bytes(BODY.encode())
    script = (
        "import sys; from notes_api import cli; "
        "cli.dispatch=lambda repo,op,payload,timeout:payload; "
        "sys.argv=['notes','add','--type','note','--file','caller note.md']; "
        "raise SystemExit(cli.main())"
    )
    raw = run([shutil.which("uv"), "run", "--project", str(project), "python", "-c", script], cwd=tmp_path)
    assert json.loads(raw)["body"] == BODY


def test_bootstrap_empty_and_retry(tmp_path):
    remote = str(tmp_path / "empty.git")
    run(["git", "init", "--bare", "--quiet", remote])
    with pytest.raises(NotesError) as error:
        with snapshot(remote):
            pass
    assert error.value.code == "missing_content"
    with snapshot(remote, bootstrap=True) as store:
        first = store.transact("bootstrap", {"request_id": "boot"})
        assert store.ids() == []
        assert store.transact("bootstrap", {"request_id": "boot"}) == first
        with pytest.raises(NotesError) as error:
            store.transact("bootstrap", {"request_id": "other"})
        assert error.value.code == "already_initialized"


def test_stale_writer_cannot_overwrite_new_content(remote):
    with snapshot(remote) as stale:
        with snapshot(remote) as latest:
            first = add(latest)
        with pytest.raises(NotesError) as error:
            add(stale, "Second", "second")
        assert error.value.code == "outcome_unknown"
    with snapshot(remote) as current:
        assert current.commit == first["commit"]
        assert len(current.list()["notes"]) == 1
        assert add(current, "Second", "second")["notes"][0]["title"] == "Second"


def test_writer_rejects_other_branches(tmp_path, monkeypatch):
    from notes_api import writer
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/content")
    assert writer.main() == 1
    assert json.loads((tmp_path / "result.json").read_text())["error"]["code"] == "untrusted_ref"


def test_configured_snake_case_types_and_alphanumeric_status(monkeypatch):
    from notes_api import model
    monkeypatch.setattr(model, "TYPES", [*model.TYPES, "meeting_notes"])
    metadata, _ = model.create_note("meeting_notes", "v2", "# Discussion", NOW)
    assert model.valid_id(metadata["id"]) == "meeting_notes/2026-10-23_discussion.md"


@pytest.mark.parametrize("value", ["20261023", "2026-W43-5", "2026-10-40", ""])
def test_date_filters_require_calendar_iso_date(value, tmp_path):
    from notes_api.storage import Snapshot
    with pytest.raises(NotesError) as error:
        Snapshot(tmp_path, "unused").list(date_from=value)
    assert error.value.code == "invalid_date"

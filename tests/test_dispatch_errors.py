import pytest

from notes_api import cli
from notes_api.model import NotesError


@pytest.mark.parametrize("error", [
    NotesError("command_unavailable", "Cannot execute gh"),
    NotesError("command_failed", "Run gh auth login", exit_code=4),
    NotesError("command_failed", "Resource not accessible (HTTP 403)", exit_code=1),
    NotesError("command_failed", "Invalid workflow inputs (HTTP 422)", exit_code=1),
])
def test_definite_rejection_preserves_actionable_cause(monkeypatch, error):
    def reject(*args, **kwargs):
        raise error

    monkeypatch.setattr(cli, "gh_api", reject)
    with pytest.raises(NotesError) as caught:
        cli.dispatch("nordicintel/notes", "delete", {"request_id": "retry-1", "ids": []}, 1)
    assert caught.value.code == "dispatch_rejected"
    assert caught.value.details["request_id"] == "retry-1"
    assert caught.value.details["cause"] == error.as_dict()["error"]


def test_unconfirmed_network_dispatch_keeps_retry_identity(monkeypatch):
    def timeout(*args, **kwargs):
        raise NotesError("command_timeout", "gh did not finish in time")

    monkeypatch.setattr(cli, "gh_api", timeout)
    with pytest.raises(NotesError) as caught:
        cli.dispatch("nordicintel/notes", "delete", {"request_id": "retry-1", "ids": []}, 1)
    assert caught.value.code == "outcome_unknown"
    assert caught.value.details["request_id"] == "retry-1"
    assert caught.value.details["cause"]["code"] == "command_timeout"

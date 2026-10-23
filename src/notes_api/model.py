"""Note validation and lossless Markdown envelopes."""

import json
import re
import unicodedata
from datetime import datetime, timezone
from importlib.resources import files

import yaml
from markdown_it import MarkdownIt

TYPES = json.loads(files("notes_api").joinpath("note_types.json").read_text())


class NotesError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self):
        return {"error": {"code": self.code, "message": str(self), **self.details}}


def require(condition, code, message):
    if not condition:
        raise NotesError(code, message)


def valid_type(value):
    require(value in TYPES, "invalid_type", f"Type must be one of {TYPES}")
    return value


def valid_status(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", value),
            "invalid_status", "Status must be a lowercase snake_case label")
    return value


def valid_id(value):
    require(isinstance(value, str) and re.fullmatch(
        r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*/\d{4}-\d{2}-\d{2}_[a-z0-9]+(?:_[a-z0-9]+)*\.md", value),
        "invalid_id", "Expected type/YYYY-MM-DD_title_in_snake_case.md")
    valid_type(value.split("/")[0])
    return value


def headings(body):
    tokens = MarkdownIt().parse(body)
    result = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            inline = tokens[index + 1]
            text = "".join(child.content if child.type != "softbreak" else " "
                           for child in inline.children or []
                           if child.type in {"text", "code_inline", "image", "softbreak"})
            result.append({"level": int(token.tag[1:]), "text": text})
    return result


def create_note(note_type, status, body, now=None):
    valid_type(note_type)
    valid_status(status)
    require(isinstance(body, str), "invalid_body", "Markdown must be text")
    title = next((h["text"] for h in headings(body) if h["level"] == 1), "")
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    require(bool(slug), "invalid_title", "Markdown requires an H1 with an ASCII-normalizable title")
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    note_id = f"{note_type}/{timestamp.date()}_{slug}.md"
    require(len(note_id.encode()) <= 240, "invalid_title", "Title produces an overly long filename")
    metadata = {"schema_version": 1, "id": note_id, "type": note_type, "title": title,
                "created_at": timestamp.isoformat().replace("+00:00", "Z"), "status": status}
    return metadata, encode_note(metadata, body)


def encode_note(metadata, body):
    return ("---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
            + "---\n" + body).encode("utf-8")


def decode_note(raw):
    text = raw.decode("utf-8")
    require(text.startswith("---\n") and "\n---\n" in text[4:], "invalid_note", "Invalid note envelope")
    header, body = text[4:].split("\n---\n", 1)
    metadata = yaml.safe_load(header)
    require(isinstance(metadata, dict) and metadata.get("schema_version") == 1,
            "invalid_note", "Unsupported note metadata")
    return metadata, body

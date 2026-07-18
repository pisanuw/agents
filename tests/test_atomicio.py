"""atomicio.write_text: the one shared atomic writer. State files are committed and pulled by the
other host, so a write must be all-or-nothing (no half-written committed file)."""
from cagent import atomicio


def test_write_creates_parent_and_file(tmp_path):
    p = tmp_path / "sub" / "deep" / "f.json"
    atomicio.write_text(p, '{"a": 1}')
    assert p.read_text() == '{"a": 1}'


def test_overwrite_replaces_atomically_and_leaves_no_temp(tmp_path):
    p = tmp_path / "f.txt"
    atomicio.write_text(p, "old")
    atomicio.write_text(p, "new")
    assert p.read_text() == "new"
    # the tmp file is renamed into place, never left behind
    assert list(tmp_path.glob("*.tmp")) == []


def test_read_jsonl_skips_blank_and_bad_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n\nNOT JSON\n{"b": 2}\n')
    assert atomicio.read_jsonl(p) == [{"a": 1}, {"b": 2}]


def test_read_jsonl_missing_file_is_empty(tmp_path):
    assert atomicio.read_jsonl(tmp_path / "nope.jsonl") == []


def test_read_json_returns_value_or_default(tmp_path):
    p = tmp_path / "q.json"
    p.write_text('["one", "two"]')
    assert atomicio.read_json(p, default=[]) == ["one", "two"]
    assert atomicio.read_json(tmp_path / "missing.json", default=[]) == []   # missing -> default
    p.write_text("{ not json")
    assert atomicio.read_json(p, default={"x": 1}) == {"x": 1}               # unparseable -> default


def test_persona_state_survives_simulated_torn_write(tmp_path, monkeypatch):
    # A persona-state save that fails mid-write must not destroy the existing good file: atomicio
    # writes a temp and only os.replace()s on success, so a crash leaves the old file intact.
    import os
    p = tmp_path / "persona-state.json"
    atomicio.write_text(p, '{"arc_stage": "wisdom"}')

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    try:
        atomicio.write_text(p, '{"arc_stage": "idealism"}')
    except OSError:
        pass
    assert p.read_text() == '{"arc_stage": "wisdom"}'      # old good file untouched
    assert list(tmp_path.glob("*.tmp")) == []              # temp cleaned up

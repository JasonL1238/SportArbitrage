from __future__ import annotations

from scripts.check_agent_docs import discover_adapters


def test_adapter_discovery_finds_nested_files_and_prunes_runtime_data(tmp_path) -> None:
    nested = tmp_path / "src" / "feature"
    nested.mkdir(parents=True)
    adapter = nested / "AGENTS.md"
    adapter.write_text("instructions\n", encoding="utf-8")

    ignored = tmp_path / "data" / "capture"
    ignored.mkdir(parents=True)
    (ignored / "AGENTS.md").write_text("runtime artifact\n", encoding="utf-8")

    found = discover_adapters(tmp_path, "AGENTS.md")

    assert found == {adapter.relative_to(tmp_path).parent: adapter}

"""Prepare and exercise workspace import with isolated workspaces."""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

from aikito.init import init_workspace
from aikito.templating import load_template
from aikito.workspace_import import run_workspace_import


def _workspace(root: Path) -> Path:
    with redirect_stdout(io.StringIO()):
        assert init_workspace(root, root.parent / "home")
    (root / "skills.toml").write_text("skills = []\n", encoding="utf-8")
    (root / "agents/codex.toml").write_text(
        load_template("agents/codex.toml"), encoding="utf-8"
    )
    return root


def main() -> None:
    base = Path(sys.argv[1]).resolve()
    source, target = _workspace(base / "source"), _workspace(base / "target")
    (source / "agents/codex.toml").write_text(
        load_template("agents/codex.toml").replace(
            'display_name = "Codex"', 'display_name = "Imported Codex"'
        ),
        encoding="utf-8",
    )
    (source / "config.toml").write_text(
        (source / "config.toml")
        .read_text(encoding="utf-8")
        .replace("stale_days = 30", "stale_days = 90"),
        encoding="utf-8",
    )
    (source / "global/AGENTS.md").write_text(
        "# Imported global instructions\n", encoding="utf-8"
    )
    skill = source / "skills/import-smoke"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Imported skill\n", encoding="utf-8")
    (source / "skills.toml").write_text('skills = ["import-smoke"]\n', encoding="utf-8")
    note = source / "inbox/deep/import-smoke.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Imported note\n", encoding="utf-8")
    (source / "subagents/import-smoke.md").write_text(
        '---\ndescription: "Smoke"\nagents: ["codex"]\n---\nSmoke.\n',
        encoding="utf-8",
    )
    (source / "mcps/import-smoke.toml").write_text(
        'transport = "remote"\nagents = ["codex"]\n', encoding="utf-8"
    )
    (source / "memory/index.md").write_text("# Memory index\n", encoding="utf-8")
    new_project = source / "projects/new-project"
    (new_project / "memory").mkdir(parents=True)
    (new_project / "agent.toml").write_text(
        'name = "new-project"\npaths = ["~/not-cloned"]\nskills = []\n',
        encoding="utf-8",
    )
    (new_project / "memory/index.md").write_text("# Project index\n", encoding="utf-8")
    for root, path in ((source, "~/source"), (target, "~/target")):
        project = root / "projects/demo"
        project.mkdir()
        (project / "agent.toml").write_text(
            f'name = "demo"\npath = "{path}"\nskills = []\n',
            encoding="utf-8",
        )

    if "--prepare-only" in sys.argv[2:]:
        return

    preview = run_workspace_import(source, target, base / "home", dry_run=True)
    assert not preview.blocked
    assert not (target / "inbox/deep/import-smoke.md").exists()
    run_workspace_import(source, target, base / "home", dry_run=False)
    assert (target / "inbox/deep/import-smoke.md").is_file()
    assert (target / "skills/import-smoke/SKILL.md").is_file()
    assert (target / "subagents/import-smoke.md").is_file()
    assert (target / "mcps/import-smoke.toml").is_file()
    assert (target / "memory/index.md").is_file()
    assert (target / "projects/new-project/agent.toml").is_file()
    assert (target / "projects/new-project/memory/index.md").is_file()
    assert 'display_name = "Imported Codex"' in (
        target / "agents/codex.toml"
    ).read_text(encoding="utf-8")
    assert "stale_days = 90" in (target / "config.toml").read_text(encoding="utf-8")
    assert (target / "global/AGENTS.md").read_text(
        encoding="utf-8"
    ) == "# Imported global instructions\n"
    assert '"~/source"' in (target / "projects/demo/agent.toml").read_text(
        encoding="utf-8"
    )


if __name__ == "__main__":
    main()

"""Prepare and exercise workspace import with isolated workspaces."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

from smoke_assertions import require_absent, require_file

from aikito.init import init_workspace
from aikito.templating import load_template


def _workspace(root: Path) -> Path:
    with redirect_stdout(io.StringIO()):
        assert init_workspace(root, root.parent / "home")
    (root / "skills.toml").write_text("skills = []\n", encoding="utf-8")
    (root / "agents/codex.toml").write_text(
        load_template("agents/codex.toml"), encoding="utf-8"
    )
    return root


def exercise_cli(base: Path, *, blocked: bool, partial: bool, execution: bool) -> None:
    source, target, home = base / "source", base / "target", base / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(
        os.environ,
        AIKITO_DIR=str(target),
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        APPDATA=str(home / ".config"),
    )

    def command(
        *args: str, expected: int = 0, log: str | None = None, combine: bool = False
    ) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "aikito", "import", "workspace", str(source), *args],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        captured = output if combine else result.stdout
        if log is not None:
            (base / log).write_text(captured, encoding="utf-8")
        print(output, end="")
        assert result.returncode == expected, (
            f"Import {base.name} {args}: expected exit {expected}, "
            f"got {result.returncode}\n{output}"
        )
        return captured

    def matches(output: str, expected: str) -> bool:
        return (
            expected.casefold() in output.casefold()
            if os.name == "nt"
            else expected in output
        )

    def contains(path: Path, expected: str) -> None:
        actual = path.read_text(encoding="utf-8")
        if os.name == "nt":
            actual, expected = actual.casefold(), expected.casefold()
        assert expected in actual, (path, expected, actual)

    def missing(relative: str) -> None:
        require_absent(target / relative, kind="path" if os.name == "nt" else "file")

    journal = ".local/state/aikito/workspace-transactions/pending.json"
    if blocked:
        output = command("--dry-run", expected=1, log="preview.out", combine=True)
        for expected in (
            "[BLOCKED] config.toml (config:inbox.path)",
            "1 note under inbox/",
        ):
            assert matches(output, expected), (base, expected, output)
        if os.name != "nt":
            assert matches(output, "would leave them unmanaged"), output
        command(expected=1, log="apply.out", combine=True)
        for relative in ("inbox/existing.md", "config.toml"):
            require_file(target / relative)
        for relative in (
            "notes/inbox/deep/import-smoke.md",
            "skills/import-smoke/SKILL.md",
            journal,
        ):
            missing(relative)
        contains(target / "config.toml", 'path = "inbox"')
    elif partial:
        command("--dry-run", expected=2, log="preview.out")
        missing("memory/notes/independent.md")
        command(
            "--keep-target",
            "global-memory:notes/independent.md",
            expected=2,
            log="skip.out",
        )
        missing("memory/notes/independent.md")
        output = command(expected=2, log="partial.out", combine=os.name != "nt")
        assert matches(output, "[PARTIAL]"), output
        for relative in ("memory/notes/independent.md", "memory/notes/keep.md"):
            require_file(target / relative)
        if os.name != "nt":
            contains(target / "memory/notes/keep.md", "Target keep")
        choices = []
        for name in ("keep", "keep-again", "take", "take-again"):
            choices.extend(
                (
                    "--keep-target" if name.startswith("keep") else "--take-source",
                    f"global-memory:notes/{name}.md",
                )
            )
        command("--dry-run", *choices)
        contains(target / "memory/notes/take.md", "Target take")
        command(*choices)
        for name in ("keep", "keep-again", "take", "take-again"):
            side = "Target" if name.startswith("keep") else "Source"
            contains(target / f"memory/notes/{name}.md", f"{side} {name}")
        output = command(*choices, log="repeat.out")
        assert matches(output, "[SUMMARY] CREATE 0, UPDATE 0, CONFLICT 0, BLOCKED 0"), (
            output
        )
        missing(journal)
    elif execution:
        command()
        require_file(target / "notes/inbox/deep/import-smoke.md")
        require_file(target / "config.toml")
        contains(target / "config.toml", "stale_days = 90")
        contains(target / "config.toml", "check = false")
        command()
        missing(journal)
    else:
        command("--dry-run")
        output = command("--dry-run", "--verbose", log="import-verbose.out")
        assert matches(output, "[SUMMARY]"), output
        assert matches(output, "[UPDATE] skills.toml (skill-selection:import-smoke)"), (
            output
        )
        assert not matches(output, "[MERGE]"), output
        missing("inbox/deep/import-smoke.md")
        command()
        for relative in (
            "inbox/deep/import-smoke.md",
            "skills/import-smoke/SKILL.md",
            "subagents/import-smoke.md",
            "mcps/import-smoke.toml",
            "memory/index.md",
            "projects/new-project/agent.toml",
            "projects/new-project/memory/index.md",
            "agents/codex.toml",
            "config.toml",
            "global/AGENTS.md",
        ):
            require_file(target / relative)
        contains(target / "agents/codex.toml", "Imported Codex")
        contains(target / "config.toml", "stale_days = 90")
        contains(target / "global/AGENTS.md", "Imported global instructions")
    (base / "checked.txt").write_text(
        "Import CLI acceptance passed\n", encoding="utf-8"
    )
    print(f"[SUCCESS] Workspace import CLI checks passed: {base.name}")


def main() -> None:
    base = Path(sys.argv[1]).resolve()
    source, target = _workspace(base / "source"), _workspace(base / "target")
    blocked = "--blocked-inbox-case" in sys.argv[2:]
    partial = "--conflict-case" in sys.argv[2:]
    inbox = "notes/inbox" if blocked or "--execution-case" in sys.argv[2:] else "inbox"
    if partial:
        for name in ("keep", "keep-again", "take", "take-again"):
            for root, side in ((source, "Source"), (target, "Target")):
                (root / "memory/notes" / f"{name}.md").write_text(
                    f"# {side} {name}\n", encoding="utf-8"
                )
        (source / "memory/notes/independent.md").write_text(
            "# Independent\n", encoding="utf-8"
        )
    if blocked:
        existing = target / "inbox/existing.md"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text("# Existing target note\n", encoding="utf-8")
    (source / "agents/codex.toml").write_text(
        load_template("agents/codex.toml").replace(
            'display_name = "Codex"', 'display_name = "Imported Codex"'
        ),
        encoding="utf-8",
    )
    (source / "config.toml").write_text(
        (source / "config.toml")
        .read_text(encoding="utf-8")
        .replace("stale_days = 30", "stale_days = 90")
        .replace('path = "inbox"', f'path = "{inbox}"')
        .replace("check = true", "check = false"),
        encoding="utf-8",
    )
    (source / "global/AGENTS.md").write_text(
        "# Imported global instructions\n", encoding="utf-8"
    )
    skill = source / "skills/import-smoke"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Imported skill\n", encoding="utf-8")
    (source / "skills.toml").write_text('skills = ["import-smoke"]\n', encoding="utf-8")
    note = source / inbox / "deep/import-smoke.md"
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

    exercise_cli(
        base,
        blocked=blocked,
        partial=partial,
        execution="--execution-case" in sys.argv[2:],
    )


if __name__ == "__main__":
    main()

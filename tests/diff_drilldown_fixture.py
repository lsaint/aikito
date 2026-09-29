"""Create an isolated multi-checkout workspace for CLI smoke tests."""

import json
import sys
from pathlib import Path


def create_fixture(root: Path) -> None:
    for directory in (
        "agents",
        "subagents",
        "mcps",
        "projects/demo",
        "skills/example/scripts",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "layout.toml").write_text("version = 2\n", encoding="utf-8")
    (root / "mcps/broken.toml").write_text("invalid = [", encoding="utf-8")
    (root / "skills/example/SKILL.md").write_text("# Example\n", encoding="utf-8")
    (root / "skills/example/scripts/check.py").write_text(
        "canonical\n", encoding="utf-8"
    )
    checkouts = [root / "first", root / "second"]
    for checkout in checkouts:
        runtime = checkout / ".agents/skills/example"
        (runtime / "scripts").mkdir(parents=True, exist_ok=True)
        (runtime / "SKILL.md").write_text("# Example\n", encoding="utf-8")
        (runtime / "scripts/check.py").write_text(
            f"local {checkout.name}\n", encoding="utf-8"
        )
    paths = json.dumps([path.as_posix() for path in checkouts])
    (root / "projects/demo/agent.toml").write_text(
        f'paths = {paths}\nsync_mode = "copy"\nskills = ["example"]\n', encoding="utf-8"
    )


if __name__ == "__main__":
    create_fixture(Path(sys.argv[1]).resolve())

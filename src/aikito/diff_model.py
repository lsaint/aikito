"""Data model for drifted resources."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DriftDiff:
    """Structured representation of a drifted resource and its unified diff."""

    kind: str  # "project_skill", "mcp", "subagent"
    name: str  # skill name, mcp server name, or subagent name
    diff: str
    agent: str | None = None
    project: str | None = None
    file: str | None = None
    checkout: str | None = None

    @property
    def display_label(self) -> str:
        """User-facing label for the drifted resource."""
        if self.kind == "project_skill":
            label = f"Project {self.project}/skill {self.name} — {self.file}"
            return f"{label} ({self.checkout})" if self.checkout else label
        if self.kind == "mcp":
            return f"MCP {self.agent}/{self.name}"
        if self.kind == "subagent":
            return f"Subagent {self.agent}/{self.name}"
        return self.name

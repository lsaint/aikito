"""Project context token footprint estimation.

Estimates the token overhead exposed to an Agent entering a project workspace:
- Global and Project instructions (full text)
- Effective skills discovery metadata (<name>: <description> or <name>)
- Always-loaded skills (full SKILL.md, e.g. durable-memory)

Uses a zero-dependency Ceil-of-Sum byte approximation: ceil(len(utf-8 bytes) / 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .frontmatter import _parse_markdown_frontmatter
from .global_skills import load_global_skills_list

ALWAYS_LOADED_SKILLS: frozenset[str] = frozenset({"durable-memory"})


def estimate_tokens(text: str) -> int:
    """Estimate token count for a text string using byte length ceil(bytes / 4)."""
    if not text:
        return 0
    return (len(text.encode("utf-8")) + 3) // 4


def format_token_estimate(tokens: int) -> str:
    """Format token count into display string.

    Rules:
    - tokens <= 0 -> '~0'
    - 0 < tokens < 1000 -> '~{tokens}'
    - tokens >= 1000 -> '~{tokens / 1000:.1f}k' (fixed 1 decimal place, e.g. '~1.0k')
    """
    if tokens <= 0:
        return "~0"
    if tokens < 1000:
        return f"~{tokens}"
    return f"~{tokens / 1000:.1f}k"


def extract_skill_description(skill_dir: Path) -> str | None:
    """Extract description field from SKILL.md frontmatter, returning None if missing or empty."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        content = skill_md.read_text(encoding="utf-8", errors="ignore")
        meta, _ = _parse_markdown_frontmatter(content)
        desc = meta.get("description")
        if isinstance(desc, str):
            desc_clean = desc.strip()
            return desc_clean if desc_clean else None
    except Exception:
        pass
    return None


def get_skill_context_bytes(
    skill_name: str,
    skills_dir: Path,
    always_loaded_skills: frozenset[str] = ALWAYS_LOADED_SKILLS,
) -> int:
    """Calculate the context byte footprint for a single skill.

    - If always loaded (e.g. durable-memory): full SKILL.md bytes.
    - Otherwise: discovery metadata '<name>: <description>' or '<name>' if no description.
    - If SKILL.md is missing or unreadable: returns 0 bytes.
    """
    skill_dir = skills_dir / skill_name
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return 0

    if skill_name in always_loaded_skills:
        try:
            content = skill_md.read_text(encoding="utf-8", errors="replace")
            return len(content.encode("utf-8")) if content.strip() else 0
        except Exception:
            return 0

    desc = extract_skill_description(skill_dir)
    text = f"{skill_name}: {desc}" if desc else skill_name
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class GlobalContextCache:
    """Precomputed cache of global instruction and skills footprint."""

    global_instruction_bytes: int
    global_skills: tuple[str, ...]
    skill_bytes_map: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        aikito_dir: Path,
        always_loaded_skills: frozenset[str] = ALWAYS_LOADED_SKILLS,
    ) -> GlobalContextCache:
        global_agents = aikito_dir / "global" / "AGENTS.md"
        global_instruction_bytes = 0
        if global_agents.is_file():
            try:
                content = global_agents.read_text(encoding="utf-8", errors="replace")
                if content.strip():
                    global_instruction_bytes = len(content.encode("utf-8"))
            except Exception:
                pass

        global_skills = load_global_skills_list(aikito_dir)
        skills_dir = aikito_dir / "skills"
        skill_bytes_map: dict[str, int] = {}
        for s_name in global_skills:
            skill_bytes_map[s_name] = get_skill_context_bytes(
                s_name, skills_dir, always_loaded_skills=always_loaded_skills
            )

        return cls(
            global_instruction_bytes=global_instruction_bytes,
            global_skills=global_skills,
            skill_bytes_map=skill_bytes_map,
        )


def estimate_project_context(
    aikito_dir: Path,
    project_dir: Path,
    project_skill_names: tuple[str, ...],
    global_cache: GlobalContextCache | None = None,
    always_loaded_skills: frozenset[str] = ALWAYS_LOADED_SKILLS,
) -> int:
    """Calculate the total estimated context token footprint for a project.

    Uses Ceil of Sum over:
    - Global instructions (<workspace>/global/AGENTS.md)
    - Project instructions (<workspace>/projects/<name>/AGENTS.md)
    - Effective skills (global skills ∪ project skills, deduplicated)
    """
    if global_cache is None:
        global_cache = GlobalContextCache.load(
            aikito_dir, always_loaded_skills=always_loaded_skills
        )

    total_bytes = global_cache.global_instruction_bytes

    proj_agents = project_dir / "AGENTS.md"
    if proj_agents.is_file():
        try:
            content = proj_agents.read_text(encoding="utf-8", errors="replace")
            if content.strip():
                total_bytes += len(content.encode("utf-8"))
        except Exception:
            pass

    effective_skills = set(global_cache.global_skills) | set(project_skill_names)
    skills_dir = aikito_dir / "skills"

    for s_name in sorted(effective_skills):
        if s_name in global_cache.skill_bytes_map:
            total_bytes += global_cache.skill_bytes_map[s_name]
        else:
            total_bytes += get_skill_context_bytes(
                s_name, skills_dir, always_loaded_skills=always_loaded_skills
            )

    if total_bytes <= 0:
        return 0

    return (total_bytes + 3) // 4

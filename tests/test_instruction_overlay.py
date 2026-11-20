"""Comprehensive tests for project personal instructions overlay and skill overlay."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from aikito.compat import safe_symlink
from aikito.doctor import (
    check_environment,
    check_projects,
    run_doctor,
    run_doctor_fixes,
)
from aikito.git_ops import (
    add_git_exclude_entries,
    check_git_exclude_entries,
    get_git_exclude_path,
)
from aikito.instructions import (
    OVERLAY_INSTRUCTION_BLOCK_END,
    OVERLAY_INSTRUCTION_BLOCK_START,
    OVERLAY_MANAGED_BLOCK,
    build_project_instruction_batch,
    ensure_global_overlay_guidance,
    execute_instruction_plan,
    plan_instructions,
)
from aikito.project_sync import (
    apply_project_sync_batch,
    build_project_sync_batch,
)
from layout_helpers import write_agents


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo with isolated config."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "core.excludesfile", "/dev/null"],
        check=True,
        capture_output=True,
    )


def _commit_file(repo: Path, rel_path: str, content: str) -> Path:
    """Write, git add -f, and commit a file in a git repo."""
    file_path = repo / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-f", rel_path],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", f"add {rel_path}"],
        check=True,
        capture_output=True,
    )
    return file_path


class InstructionOverlayTests(TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name).resolve()
        self.home = self.root / "home"
        self.ws = self.root / "workspace"
        self.co = self.root / "checkout"
        self.home.mkdir()
        self.ws.mkdir()
        self.co.mkdir()

        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".agents" / "skills").mkdir(parents=True)

        write_agents(
            self.ws,
            "[agents.codex]\n"
            'display_name = "Codex"\n'
            'instruction_path = ".codex/AGENTS.md"\n'
            'project_instruction_path = "AGENTS.md"\n'
            'skills_path = ".agents/skills"\n'
            "[agents.claude-code]\n"
            'display_name = "Claude Code"\n'
            'instruction_path = ".claude/CLAUDE.md"\n'
            'project_instruction_path = "AGENTS.md"\n'
            'skills_path = ".claude/skills"\n',
        )

        (self.ws / "global").mkdir()
        self.global_agents = self.ws / "global" / "AGENTS.md"
        self.global_agents.write_text("# Global rules\n", encoding="utf-8")

        self.proj_dir = self.ws / "projects" / "demo"
        self.proj_dir.mkdir(parents=True)
        (self.proj_dir / "agent.toml").write_text(
            f'name = "demo"\npaths = ["{self.co.as_posix()}"]\nskills = []\n',
            encoding="utf-8",
        )
        self.canon_agents = self.proj_dir / "AGENTS.md"
        self.canon_agents.write_text("# Personal project rules\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.td.cleanup()

    # 1. 回归：老布局（未跟踪路径加已有 symlink）同步前后完全一致
    def test_1_regression_untracked_and_existing_symlink(self) -> None:
        batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        plan = plan_instructions(batch, self.home)
        self.assertTrue(plan.can_apply)
        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].target_path, self.co / "AGENTS.md")
        self.assertEqual(plan.operations[0].action, "CREATE")

        res = execute_instruction_plan(plan, self.home)
        self.assertTrue(res.success)
        self.assertTrue((self.co / "AGENTS.md").is_symlink())

        # Second sync is no-op
        plan2 = plan_instructions(batch, self.home)
        self.assertTrue(plan2.can_apply)
        self.assertEqual(plan2.operations[0].action, "NOOP")

    # 2. 被跟踪的 AGENTS.md 进入 overlay，不再挡住 skills 和 memory 同步；团队文件内容和 git 状态都不变
    def test_2_tracked_agents_enters_overlay_without_blocking(self) -> None:
        _init_git_repo(self.co)
        team_content = "# Team instructions\nMust follow team rules.\n"
        team_file = _commit_file(self.co, "AGENTS.md", team_content)

        # Create a skill in workspace and select it in agent.toml
        skill_dir = self.ws / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# My skill\n", encoding="utf-8")
        (self.proj_dir / "agent.toml").write_text(
            f'name = "demo"\npaths = ["{self.co.as_posix()}"]\nskills = ["my-skill"]\n',
            encoding="utf-8",
        )

        data = {
            "name": "demo",
            "paths": [self.co.as_posix()],
            "skills": ["my-skill"],
            "sync_mode": "link",
        }
        batch = build_project_sync_batch(self.ws, self.home, "demo", data)
        self.assertTrue(batch.can_apply)
        self.assertIsNotNone(batch.instruction_plan)
        self.assertTrue(batch.instruction_plan.can_apply)

        # Target should be .agents/AGENTS.md in overlay mode
        inst_ops = batch.instruction_plan.operations
        self.assertTrue(
            any(op.target_path == self.co / ".agents" / "AGENTS.md" for op in inst_ops)
        )

        # Apply batch
        res = apply_project_sync_batch(batch, data, self.home)
        self.assertTrue(res.is_success)

        # Verify team file is untouched regular file
        self.assertFalse(team_file.is_symlink())
        self.assertEqual(team_file.read_text(encoding="utf-8"), team_content)

        # Verify skill was applied
        self.assertTrue((self.co / ".agents" / "skills" / "my-skill").is_symlink())
        # Verify overlay was created
        self.assertTrue((self.co / ".agents" / "AGENTS.md").is_symlink())

        # Verify git status on team file is clean
        st = subprocess.run(
            ["git", "-C", str(self.co), "status", "--porcelain", "--", "AGENTS.md"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(st.stdout.strip(), "")

    # 3. overlay 下创建 .agents/AGENTS.md 链接，exclude 写入正确且幂等，worktree 场景下也能正确处理
    def test_3_overlay_exclude_and_worktree(self) -> None:
        _init_git_repo(self.co)
        _commit_file(self.co, "AGENTS.md", "team")

        data = {"name": "demo", "paths": [self.co.as_posix()], "skills": []}
        batch = build_project_sync_batch(self.ws, self.home, "demo", data)
        res = apply_project_sync_batch(batch, data, self.home)
        self.assertTrue(res.is_success)

        # Check exclude
        exclude_status = check_git_exclude_entries(self.co, [".agents/AGENTS.md"])
        self.assertTrue(exclude_status[".agents/AGENTS.md"])

        # Idempotent re-sync
        batch_resync = build_project_sync_batch(self.ws, self.home, "demo", data)
        res2 = apply_project_sync_batch(batch_resync, data, self.home)
        self.assertTrue(res2.is_success)
        ex_path = get_git_exclude_path(self.co)
        self.assertIsNotNone(ex_path)
        lines = ex_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines.count("/.agents/AGENTS.md"), 1)

        # Worktree scenario
        wt_dir = self.root / "worktree"
        subprocess.run(
            [
                "git",
                "-C",
                str(self.co),
                "worktree",
                "add",
                str(wt_dir),
                "-b",
                "wt-test",
            ],
            check=True,
            capture_output=True,
        )
        (self.proj_dir / "agent.toml").write_text(
            f'name = "demo"\npaths = ["{self.co.as_posix()}", "{wt_dir.as_posix()}"]\nskills = []\n',
            encoding="utf-8",
        )
        wt_data = {
            "name": "demo",
            "paths": [self.co.as_posix(), wt_dir.as_posix()],
            "skills": [],
        }
        wt_batch = build_project_sync_batch(self.ws, self.home, "demo", wt_data)
        self.assertTrue(wt_batch.can_apply)
        wt_res = apply_project_sync_batch(wt_batch, wt_data, self.home)
        self.assertTrue(wt_res.is_success)
        self.assertTrue((wt_dir / ".agents" / "AGENTS.md").is_symlink())

        # Unlink in self.co by removing its link
        (self.co / ".agents" / "AGENTS.md").unlink()
        # Verify wt_dir's exclude remains active and is not deleted
        wt_status = check_git_exclude_entries(wt_dir, [".agents/AGENTS.md"])
        self.assertTrue(wt_status[".agents/AGENTS.md"])
        # wt_dir git status remains clean
        status_proc = subprocess.run(
            ["git", "-C", str(wt_dir), "status", "--porcelain", "-u"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertNotIn(".agents/AGENTS.md", status_proc.stdout)

    # 4. 全局引导语 managed block 只写入一次、幂等；全局指令为空时给出警告
    def test_4_global_guidance_block_idempotency_and_empty_warning(self) -> None:
        _init_git_repo(self.co)
        _commit_file(self.co, "AGENTS.md", "team")
        data = {"name": "demo", "paths": [self.co.as_posix()], "skills": []}

        # First sync writes the block
        batch = build_project_sync_batch(self.ws, self.home, "demo", data)
        res = apply_project_sync_batch(batch, data, self.home)
        self.assertTrue(res.is_success)
        content = self.global_agents.read_text(encoding="utf-8")
        self.assertIn(OVERLAY_INSTRUCTION_BLOCK_START, content)
        self.assertIn(OVERLAY_INSTRUCTION_BLOCK_END, content)

        # Second sync does not duplicate
        batch2 = build_project_sync_batch(self.ws, self.home, "demo", data)
        res2 = apply_project_sync_batch(batch2, data, self.home)
        self.assertTrue(res2.is_success)
        content2 = self.global_agents.read_text(encoding="utf-8")
        self.assertEqual(content2.count(OVERLAY_INSTRUCTION_BLOCK_START), 1)

        # Empty global instructions
        self.global_agents.write_text("   \n", encoding="utf-8")
        ok, msg = ensure_global_overlay_guidance(self.global_agents)
        self.assertFalse(ok)
        self.assertIn("empty", msg or "")

    # 5. canonical 为空时 overlay 链接按生命周期清理
    def test_5_canonical_empty_cleans_up_overlay(self) -> None:
        _init_git_repo(self.co)
        _commit_file(self.co, "AGENTS.md", "team")
        data = {"name": "demo", "paths": [self.co.as_posix()], "skills": []}

        # Setup overlay
        batch = build_project_sync_batch(self.ws, self.home, "demo", data)
        res = apply_project_sync_batch(batch, data, self.home)
        self.assertTrue(res.is_success)
        overlay_link = self.co / ".agents" / "AGENTS.md"
        self.assertTrue(overlay_link.is_symlink())

        # Empty canonical
        self.canon_agents.write_text("", encoding="utf-8")
        batch2 = build_project_sync_batch(self.ws, self.home, "demo", data)
        self.assertTrue(batch2.can_apply)
        res2 = apply_project_sync_batch(batch2, data, self.home)
        self.assertTrue(res2.is_success)
        self.assertFalse(overlay_link.exists())
        self.assertFalse(overlay_link.is_symlink())
        # Overlay link unlinked, exclude entry safely retained (add-only)
        status = check_git_exclude_entries(self.co, [".agents/AGENTS.md"])
        self.assertTrue(status[".agents/AGENTS.md"])

    # 6. claude-code 迁移：只删除 aikito 自己的 .claude/CLAUDE.md symlink，外来文件保留
    def test_6_claude_code_migration_unlinks_owned_symlink(self) -> None:
        claude_md = self.co / ".claude" / "CLAUDE.md"
        claude_md.parent.mkdir(parents=True)
        safe_symlink(self.canon_agents.resolve(), claude_md)
        self.assertTrue(claude_md.is_symlink())

        batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        plan = plan_instructions(batch, self.home)
        stale_ops = [op for op in plan.operations if op.target_path == claude_md]
        self.assertEqual(len(stale_ops), 1)
        self.assertEqual(stale_ops[0].action, "UNLINK")

        res = execute_instruction_plan(plan, self.home)
        self.assertTrue(res.success)
        self.assertFalse(claude_md.exists())

        # External regular file or foreign symlink preserved
        foreign_target = self.root / "foreign.md"
        foreign_target.write_text("foreign", encoding="utf-8")
        safe_symlink(foreign_target, claude_md)
        batch2 = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        self.assertFalse(any(t.path == claude_md for t in batch2.stale_targets))

    # 7. typechange 和重复内容场景只提示、不修改
    def test_7_typechange_and_duplicate_content_diagnostics(self) -> None:
        _init_git_repo(self.co)
        team_para1 = "This is a team instruction paragraph that exceeds eighty characters in total length for testing purposes."
        team_para2 = "Here is a second team instruction paragraph that also definitely exceeds eighty characters in total."
        team_content = f"# Heading\n\n{team_para1}\n\n{team_para2}\n"
        team_file = _commit_file(self.co, "AGENTS.md", team_content)

        # Canonical has 1 matching paragraph out of 2 team paragraphs (50% ratio, boundary covered)
        self.canon_agents.write_text(
            f"# Personal\n\n{team_para1}\n\nOther personal notes here.\n",
            encoding="utf-8",
        )

        sec = check_projects(self.ws, self.home)
        dup_findings = [f for f in sec.findings if "duplicated paragraph" in f.message]
        self.assertEqual(len(dup_findings), 1)
        self.assertEqual(dup_findings[0].status, "WARN")

        # Typechange test: replace team AGENTS.md with symlink
        team_file.unlink()
        safe_symlink(self.canon_agents.resolve(), team_file)
        sec2 = check_projects(self.ws, self.home)
        tc_findings = [f for f in sec2.findings if "git typechange" in f.message]
        self.assertEqual(len(tc_findings), 1)
        self.assertEqual(tc_findings[0].status, "WARN")

    # 8. Claude Code 版本低于 2.1.277 时 doctor 报错
    def test_8_claude_code_version_check_in_doctor(self) -> None:
        with patch("shutil.which", return_value="/fake/claude"):
            # Old version: 2.1.276
            old_proc = subprocess.CompletedProcess(
                args=["claude", "--version"],
                returncode=0,
                stdout="2.1.276 (Claude Code)",
            )
            with patch("subprocess.run", return_value=old_proc):
                sec = check_environment(self.ws, self.home)
                fail_findings = [
                    f
                    for f in sec.findings
                    if f.status == "FAIL" and "2.1.276" in f.message
                ]
                self.assertEqual(len(fail_findings), 1)

            # Modern version: 2.1.277
            ok_proc = subprocess.CompletedProcess(
                args=["claude", "--version"],
                returncode=0,
                stdout="2.1.277 (Claude Code)",
            )
            with patch("subprocess.run", return_value=ok_proc):
                sec2 = check_environment(self.ws, self.home)
                ok_findings = [
                    f
                    for f in sec2.findings
                    if f.status == "OK" and "2.1.277" in f.message
                ]
                self.assertEqual(len(ok_findings), 1)

    # 9. link 模式的 skill 按条目写入 exclude，幂等；取消选择后对应条目被移除；copy 模式不写 exclude
    def test_9_link_mode_skill_exclude_lifecycle(self) -> None:
        _init_git_repo(self.co)
        skill_dir = self.ws / "skills" / "tool-a"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# tool\n", encoding="utf-8")

        # Link mode
        data = {
            "name": "demo",
            "paths": [self.co.as_posix()],
            "skills": ["tool-a"],
            "sync_mode": "link",
        }
        batch = build_project_sync_batch(self.ws, self.home, "demo", data)
        res = apply_project_sync_batch(batch, data, self.home)
        self.assertTrue(res.is_success)

        status = check_git_exclude_entries(self.co, [".agents/skills/tool-a"])
        self.assertTrue(status[".agents/skills/tool-a"])

        # Deselect: link is removed, exclude entry safely retained (add-only)
        data_empty = {
            "name": "demo",
            "paths": [self.co.as_posix()],
            "skills": [],
            "sync_mode": "link",
        }
        batch_empty = build_project_sync_batch(self.ws, self.home, "demo", data_empty)
        res_empty = apply_project_sync_batch(batch_empty, data_empty, self.home)
        self.assertTrue(res_empty.is_success)
        self.assertFalse((self.co / ".agents" / "skills" / "tool-a").is_symlink())
        status_empty = check_git_exclude_entries(self.co, [".agents/skills/tool-a"])
        self.assertTrue(status_empty[".agents/skills/tool-a"])

        # Copy mode does not add to exclude
        skill_b = self.ws / "skills" / "tool-b"
        skill_b.mkdir(parents=True)
        (skill_b / "SKILL.md").write_text("# tool b\n", encoding="utf-8")
        data_copy = {
            "name": "demo",
            "paths": [self.co.as_posix()],
            "skills": ["tool-b"],
            "sync_mode": "copy",
        }
        batch_copy = build_project_sync_batch(self.ws, self.home, "demo", data_copy)
        res_copy = apply_project_sync_batch(batch_copy, data_copy, self.home)
        self.assertTrue(res_copy.is_success)
        status_copy = check_git_exclude_entries(self.co, [".agents/skills/tool-b"])
        self.assertFalse(status_copy[".agents/skills/tool-b"])

    # 10. 选中 skill 与 git 跟踪的团队 skill 同名：团队目录内容和 git 状态不变，其他资源照常同步，输出包含改名提示的警告
    def test_10_team_tracked_skill_skipped_with_warning(self) -> None:
        _init_git_repo(self.co)
        team_skill_file = _commit_file(
            self.co, ".agents/skills/shared/SKILL.md", "# Team skill\n"
        )

        # User has canonical skill named 'shared'
        my_skill = self.ws / "skills" / "shared"
        my_skill.mkdir(parents=True)
        (my_skill / "SKILL.md").write_text("# Personal skill\n", encoding="utf-8")

        data = {
            "name": "demo",
            "paths": [self.co.as_posix()],
            "skills": ["shared"],
            "sync_mode": "link",
        }
        batch = build_project_sync_batch(self.ws, self.home, "demo", data)
        self.assertTrue(batch.can_apply)

        # Check operation is SKIP with warning finding
        op = batch.skill_plan.operations[0]
        self.assertEqual(op.action, "SKIP")
        self.assertEqual(op.rule_id, "INV-TR-21")
        self.assertIn("tracked by team git", op.reason)
        self.assertTrue(op.is_authorized)

        # Apply does not modify team skill file
        res = apply_project_sync_batch(batch, data, self.home)
        self.assertTrue(res.is_success)
        self.assertEqual(team_skill_file.read_text(encoding="utf-8"), "# Team skill\n")

    # 11. 选中 skill 与未跟踪的同名目录冲突：维持原有冲突行为，--force 才接管
    def test_11_untracked_foreign_skill_directory_conflicts(self) -> None:
        _init_git_repo(self.co)
        untracked = self.co / ".agents" / "skills" / "foreign"
        untracked.mkdir(parents=True)
        (untracked / "SKILL.md").write_text("# Foreign\n", encoding="utf-8")

        my_skill = self.ws / "skills" / "foreign"
        my_skill.mkdir(parents=True)
        (my_skill / "SKILL.md").write_text("# Canonical\n", encoding="utf-8")

        data = {
            "name": "demo",
            "paths": [self.co.as_posix()],
            "skills": ["foreign"],
            "sync_mode": "link",
        }
        batch = build_project_sync_batch(self.ws, self.home, "demo", data, force=False)
        self.assertFalse(batch.can_apply)
        self.assertTrue(batch.skill_plan.has_conflicts)

    # 12. 团队在 .agents/skills/ 下新增 skill 时不受 exclude 影响，git status 能正常看到
    def test_12_team_skill_not_hidden_by_exclude(self) -> None:
        _init_git_repo(self.co)
        # Add link mode skill which writes .agents/skills/my-link
        add_git_exclude_entries(
            self.co, [".agents/skills/my-link", ".agents/AGENTS.md"]
        )

        # Team creates new skill
        new_team_skill = self.co / ".agents" / "skills" / "team-new" / "SKILL.md"
        new_team_skill.parent.mkdir(parents=True)
        new_team_skill.write_text("# Team\n", encoding="utf-8")

        st = subprocess.run(
            ["git", "-C", str(self.co), "status", "--porcelain", "-u"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn(".agents/skills/team-new/SKILL.md", st.stdout)

    # 13. claude-code.toml 迁移：值为旧默认值时 doctor 报告，--fix 只改这个键、保留其他内容；自定义值不报告也不修改；修复后再次运行 doctor 结果干净
    def test_13_claude_code_toml_migration(self) -> None:
        claude_file = self.ws / "agents" / "claude-code.toml"
        old_text = (
            '# Comment here\n[agents.claude-code]\nproject_instruction_path = ".claude/CLAUDE.md"\n'
            'skills_path = ".claude/skills"\n'
        )
        claude_file.write_text(old_text, encoding="utf-8")

        # Doctor reports
        rep = run_doctor(self.ws, self.home)
        warns = [
            f
            for sec in rep.sections
            for f in sec.findings
            if "outdated default '.claude/CLAUDE.md'" in f.message
        ]
        self.assertEqual(len(warns), 1)

        # Doctor --fix
        fixes = run_doctor_fixes(self.ws, self.home)
        self.assertTrue(any("claude-code.toml" in fix for fix in fixes))
        new_content = claude_file.read_text(encoding="utf-8")
        self.assertIn('project_instruction_path = "AGENTS.md"', new_content)
        self.assertIn("# Comment here", new_content)

        # Subsequent doctor run is clean
        rep2 = run_doctor(self.ws, self.home)
        warns2 = [
            f
            for sec in rep2.sections
            for f in sec.findings
            if "outdated default '.claude/CLAUDE.md'" in f.message
        ]
        self.assertEqual(len(warns2), 0)

    # 14. 迁移后同步：指向 canonical 的 .claude/CLAUDE.md symlink 被卸载，普通文件和外来 symlink 保留
    def test_14_post_migration_sync_unlinks_claude_md(self) -> None:
        claude_link = self.co / ".claude" / "CLAUDE.md"
        claude_link.parent.mkdir(parents=True)
        safe_symlink(self.canon_agents.resolve(), claude_link)

        data = {"name": "demo", "paths": [self.co.as_posix()], "skills": []}
        batch = build_project_sync_batch(self.ws, self.home, "demo", data)
        res = apply_project_sync_batch(batch, data, self.home)
        self.assertTrue(res.is_success)
        self.assertFalse(claude_link.exists())

    # 15. stale 交互：overlay 模式下 .agents/AGENTS.md 不被卸载；非 overlay 模式下残留的 .agents/AGENTS.md 被卸载；overlay 与非 overlay 切换后只剩一个活跃目标
    def test_15_stale_targets_interaction(self) -> None:
        _init_git_repo(self.co)
        team_file = _commit_file(self.co, "AGENTS.md", "team")

        # In overlay mode, .agents/AGENTS.md is created and NOT in stale
        batch1 = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        self.assertFalse(
            any(
                t.path == self.co / ".agents" / "AGENTS.md"
                for t in batch1.stale_targets
            )
        )
        plan1 = plan_instructions(batch1, self.home)
        self.assertTrue(plan1.can_apply)
        res1 = execute_instruction_plan(plan1, self.home)
        self.assertTrue(res1.success)
        overlay_link = self.co / ".agents" / "AGENTS.md"
        self.assertTrue(overlay_link.is_symlink())

        # Switch to non-overlay: team file removed
        team_file.unlink()
        subprocess.run(
            ["git", "-C", str(self.co), "rm", "AGENTS.md"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.co), "commit", "-m", "rm team file"],
            check=True,
            capture_output=True,
        )

        batch2 = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        # In non-overlay mode, .agents/AGENTS.md is stale and AGENTS.md is formal
        self.assertTrue(any(t.path == overlay_link for t in batch2.stale_targets))
        plan2 = plan_instructions(batch2, self.home)
        self.assertTrue(plan2.can_apply)
        res2 = execute_instruction_plan(plan2, self.home)
        self.assertTrue(res2.success)

        # Only one active target now: root AGENTS.md
        self.assertTrue((self.co / "AGENTS.md").is_symlink())
        self.assertFalse(overlay_link.exists())

    # 16. 全局引导语：--dry-run 不写文件；首次写入有日志；重复同步不重复写；块内容过时只替换块内；标记不完整时不写入并警告；块外内容一字不变
    def test_16_global_guidance_dry_run_and_resilience(self) -> None:
        initial_content = "# Global Header\nSome rules\n"
        self.global_agents.write_text(initial_content, encoding="utf-8")

        # Dry run does not write
        ok, msg = ensure_global_overlay_guidance(self.global_agents, dry_run=True)
        self.assertTrue(ok)
        self.assertIn("[DRY-RUN]", msg or "")
        self.assertEqual(
            self.global_agents.read_text(encoding="utf-8"), initial_content
        )

        # First write
        ok, msg = ensure_global_overlay_guidance(self.global_agents, dry_run=False)
        self.assertTrue(ok)
        self.assertIn("added overlay instruction block", msg or "")
        after_write = self.global_agents.read_text(encoding="utf-8")
        self.assertTrue(after_write.startswith(initial_content.rstrip()))
        self.assertIn(OVERLAY_MANAGED_BLOCK, after_write)

        # Duplicate call is no-op
        ok, msg = ensure_global_overlay_guidance(self.global_agents, dry_run=False)
        self.assertTrue(ok)
        self.assertIsNone(msg)

        # Outdated content inside block replaced, text outside unchanged
        tampered = (
            f"{initial_content}\n\n"
            f"{OVERLAY_INSTRUCTION_BLOCK_START}\nOld outdated instruction text\n{OVERLAY_INSTRUCTION_BLOCK_END}\n\n"
            "Footer note outside.\n"
        )
        self.global_agents.write_text(tampered, encoding="utf-8")
        ok, msg = ensure_global_overlay_guidance(self.global_agents, dry_run=False)
        self.assertTrue(ok)
        self.assertIn("refreshed overlay instruction block", msg or "")
        refreshed = self.global_agents.read_text(encoding="utf-8")
        self.assertIn(OVERLAY_MANAGED_BLOCK, refreshed)
        self.assertIn("Footer note outside.", refreshed)

        # Broken markers: only START
        broken = f"{initial_content}\n{OVERLAY_INSTRUCTION_BLOCK_START}\nsome text\n"
        self.global_agents.write_text(broken, encoding="utf-8")
        ok, msg = ensure_global_overlay_guidance(self.global_agents, dry_run=False)
        self.assertFalse(ok)
        self.assertIn("Corrupted overlay instruction block", msg or "")
        # File unchanged
        self.assertEqual(self.global_agents.read_text(encoding="utf-8"), broken)

    # 17. 非 git 项目：doctor 不报告 missing git exclude entry 警告，doctor --fix 不报错
    def test_17_non_git_project_no_exclude_warning(self) -> None:
        # self.co is deliberately NOT initialized as a git repo
        data = {"name": "demo", "paths": [self.co.as_posix()], "skills": []}
        batch = build_project_sync_batch(self.ws, self.home, "demo", data)
        res = apply_project_sync_batch(batch, data, self.home)
        self.assertTrue(res.is_success)

        # Doctor checks
        sec = check_projects(self.ws, self.home)
        exclude_warns = [
            f for f in sec.findings if "missing git exclude entry" in f.message
        ]
        self.assertEqual(len(exclude_warns), 0)

        # Doctor fix runs cleanly without error or adding bogus entries
        fixes = run_doctor_fixes(self.ws, self.home)
        exclude_fixes = [f for f in fixes if "added git exclude" in f]
        self.assertEqual(len(exclude_fixes), 0)

    # 18. monorepo 子目录项目：exclude 规则包含子目录前缀，根目录与子目录 git status 均保持干净
    def test_18_monorepo_subdirectory_exclude_patterns(self) -> None:
        repo_root = self.root / "monorepo"
        _init_git_repo(repo_root)
        _commit_file(repo_root, "README.md", "# Monorepo\n")

        pkg_dir = repo_root / "packages" / "pkg1"
        pkg_dir.mkdir(parents=True)
        _commit_file(pkg_dir, "AGENTS.md", "team rules")

        sub_proj = self.ws / "projects" / "subdemo"
        sub_proj.mkdir(parents=True)
        (sub_proj / "memory" / "notes").mkdir(parents=True)
        (sub_proj / "agent.toml").write_text(
            f'name = "subdemo"\npaths = ["{pkg_dir.as_posix()}"]\nskills = ["tool-sub"]\nsync_mode = "link"\n',
            encoding="utf-8",
        )
        (sub_proj / "AGENTS.md").write_text("personal rules", encoding="utf-8")
        skill_sub = self.ws / "skills" / "tool-sub"
        skill_sub.mkdir(parents=True)
        (skill_sub / "SKILL.md").write_text("# tool sub", encoding="utf-8")

        data = {
            "name": "subdemo",
            "paths": [pkg_dir.as_posix()],
            "skills": ["tool-sub"],
            "sync_mode": "link",
        }
        batch = build_project_sync_batch(self.ws, self.home, "subdemo", data)
        res = apply_project_sync_batch(batch, data, self.home)
        self.assertTrue(res.is_success)

        # Verify exclude file at repo root contains /packages/pkg1/...
        ex_path = get_git_exclude_path(pkg_dir)
        self.assertIsNotNone(ex_path)
        ex_content = ex_path.read_text(encoding="utf-8")
        self.assertIn("/packages/pkg1/.agents/AGENTS.md", ex_content)
        self.assertIn("/packages/pkg1/.agents/memory", ex_content)
        self.assertIn("/packages/pkg1/.agents/skills/tool-sub", ex_content)

        # Check git status from repo root and subpackage: both must be clean
        proc_root = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "-u"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc_root.stdout.strip(), "")

        # Check check_git_exclude_entries from pkg_dir
        status = check_git_exclude_entries(
            pkg_dir,
            [
                ".agents/AGENTS.md",
                ".agents/memory",
                ".agents/skills/tool-sub",
            ],
        )
        self.assertTrue(all(status.values()))

    # 19. doctor --fix 忽略不属于 aikito 的 symlink
    def test_19_doctor_fix_ignores_non_aikito_symlinks(self) -> None:
        _init_git_repo(self.co)
        # Create external non-aikito targets
        external_dir = self.root / "external"
        external_dir.mkdir()
        external_skill = external_dir / "external-skill"
        external_skill.mkdir()
        (external_skill / "SKILL.md").write_text("# external", encoding="utf-8")
        external_agents = external_dir / "external-agents.md"
        external_agents.write_text("external agents", encoding="utf-8")

        # Create symlinks inside .agents pointing to external targets
        skills_dir = self.co / ".agents" / "skills"
        skills_dir.mkdir(parents=True)
        foreign_skill_link = skills_dir / "external-skill"
        safe_symlink(external_skill, foreign_skill_link)

        foreign_overlay_link = self.co / ".agents" / "AGENTS.md"
        safe_symlink(external_agents, foreign_overlay_link)

        # Doctor check should NOT report missing exclude warnings for these foreign symlinks
        sec = check_projects(self.ws, self.home)
        foreign_warns = [
            f
            for f in sec.findings
            if "external-skill" in f.message or "external-agents" in f.message
        ]
        self.assertEqual(len(foreign_warns), 0)

        # Doctor fix should NOT add them to info/exclude
        fixes = run_doctor_fixes(self.ws, self.home)
        foreign_fixes = [
            f for f in fixes if "external-skill" in f or "external-agents" in f
        ]
        self.assertEqual(len(foreign_fixes), 0)
        ex_path = get_git_exclude_path(self.co)
        if ex_path and ex_path.is_file():
            content = ex_path.read_text(encoding="utf-8")
            self.assertNotIn("external-skill", content)

    # 20. 仓库只有团队 CLAUDE.md（无 AGENTS.md）时整个项目也进入 overlay 模式
    def test_20_team_claude_md_only_triggers_project_overlay(self) -> None:
        _init_git_repo(self.co)
        _commit_file(self.co, "CLAUDE.md", "# Team Claude rules\n")
        # Ensure root AGENTS.md does NOT exist
        self.assertFalse((self.co / "AGENTS.md").exists())

        data = {"name": "demo", "paths": [self.co.as_posix()], "skills": []}
        batch = build_project_sync_batch(self.ws, self.home, "demo", data)
        self.assertTrue(batch.can_apply)
        res = apply_project_sync_batch(batch, data, self.home)
        self.assertTrue(res.is_success)

        # Personal instructions linked to .agents/AGENTS.md (overlay mode)
        overlay_link = self.co / ".agents" / "AGENTS.md"
        self.assertTrue(overlay_link.is_symlink())
        # Team CLAUDE.md remains intact as regular file
        self.assertTrue((self.co / "CLAUDE.md").is_file())
        self.assertFalse((self.co / "CLAUDE.md").is_symlink())
        # Root AGENTS.md is NOT created
        self.assertFalse((self.co / "AGENTS.md").exists())

    # 21. typechange 状态判断准确性：路径带 T 不误判，真正的类型转换能识别
    def test_21_git_typechange_accuracy_with_t_in_path(self) -> None:
        from aikito.git_ops import is_git_typechange

        _init_git_repo(self.co)
        test_file = _commit_file(self.co, "src/TestComponent.py", "class Test: pass\n")

        # Regular file modification has ' M' status, path contains 'T'
        test_file.write_text("class TestModified: pass\n", encoding="utf-8")
        self.assertFalse(is_git_typechange(test_file, self.co))

        # Real typechange: replace regular file with symlink
        test_file.unlink()
        safe_symlink(self.canon_agents, test_file)
        self.assertTrue(is_git_typechange(test_file, self.co))

    # 22. copy 模式在新机器上（无本地状态记录）不会被误判为团队 skill 跳过
    def test_22_copy_mode_on_new_machine_not_skipped_as_team_skill(self) -> None:
        _init_git_repo(self.co)
        # Canonical skill in workspace
        skill_dir = self.ws / "skills" / "my-copied-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# my skill content\n", encoding="utf-8")

        # Repository has git-tracked copy of this skill (cloned onto new machine)
        team_copy = _commit_file(
            self.co,
            ".agents/skills/my-copied-skill/SKILL.md",
            "# my skill content\n",
        )

        data = {
            "name": "demo",
            "paths": [self.co.as_posix()],
            "skills": ["my-copied-skill"],
            "sync_mode": "copy",
        }
        batch = build_project_sync_batch(self.ws, self.home, "demo", data)
        self.assertTrue(batch.can_apply)
        # It should NOT be skipped with INV-TR-21
        skip_ops = [
            op
            for op in batch.skill_plan.operations
            if op.action == "SKIP" and op.rule_id == "INV-TR-21"
        ]
        self.assertEqual(len(skip_ops), 0)

        res = apply_project_sync_batch(batch, data, self.home)
        self.assertTrue(res.is_success)
        self.assertTrue(team_copy.is_file())

    # 23. link 切换到 copy 后，exclude 中的条目被删除，skill 目录与文件不被 git 忽略
    def test_23_link_to_copy_removes_git_exclude_so_skill_not_ignored(self) -> None:
        _init_git_repo(self.co)
        skill_dir = self.ws / "skills" / "demo-tool"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# demo tool content\n", encoding="utf-8")

        # 1. First sync with link mode
        data_link = {
            "name": "demo",
            "paths": [self.co.as_posix()],
            "skills": ["demo-tool"],
            "sync_mode": "link",
        }
        batch1 = build_project_sync_batch(self.ws, self.home, "demo", data_link)
        self.assertTrue(batch1.can_apply)
        res1 = apply_project_sync_batch(batch1, data_link, self.home)
        self.assertTrue(res1.is_success)

        link_target = self.co / ".agents" / "skills" / "demo-tool"
        self.assertTrue(link_target.is_symlink())
        ex_path = get_git_exclude_path(self.co)
        self.assertIsNotNone(ex_path)
        ex_lines = ex_path.read_text(encoding="utf-8").splitlines()
        self.assertIn("/.agents/skills/demo-tool", ex_lines)

        # In link mode, git check-ignore should report ignored
        env = {
            k: v for k, v in subprocess.os.environ.items() if not k.startswith("GIT_")
        }
        p_check1 = subprocess.run(
            [
                "git",
                "-C",
                str(self.co),
                "check-ignore",
                "-q",
                ".agents/skills/demo-tool",
            ],
            env=env,
            check=False,
        )
        self.assertEqual(p_check1.returncode, 0)

        # 2. Switch to copy mode
        data_copy = {
            "name": "demo",
            "paths": [self.co.as_posix()],
            "skills": ["demo-tool"],
            "sync_mode": "copy",
        }
        batch2 = build_project_sync_batch(self.ws, self.home, "demo", data_copy)
        self.assertTrue(batch2.can_apply)
        res2 = apply_project_sync_batch(batch2, data_copy, self.home)
        self.assertTrue(res2.is_success)

        # Now target must be a real directory, not symlink
        self.assertTrue(link_target.is_dir())
        self.assertFalse(link_target.is_symlink())
        copied_file = link_target / "SKILL.md"
        self.assertTrue(copied_file.is_file())

        # Exclude entry must have been removed
        ex_lines_after = ex_path.read_text(encoding="utf-8").splitlines()
        self.assertNotIn("/.agents/skills/demo-tool", ex_lines_after)
        self.assertNotIn(".agents/skills/demo-tool", ex_lines_after)

        # git check-ignore on .agents/skills/demo-tool/SKILL.md must return non-zero (not ignored)
        p_check2 = subprocess.run(
            [
                "git",
                "-C",
                str(self.co),
                "check-ignore",
                "-v",
                ".agents/skills/demo-tool/SKILL.md",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(
            p_check2.returncode, 1, f"Expected unignored but got: {p_check2.stdout}"
        )

    # 24. 未被 git 跟踪的 symlink 指向被跟踪文件，不会被误判为团队文件而切换 overlay
    def test_24_untracked_symlink_pointing_to_tracked_file_not_misidentified(
        self,
    ) -> None:
        from aikito.git_ops import is_git_tracked

        _init_git_repo(self.co)
        # Commit a legitimate team documentation file
        _commit_file(self.co, "docs/guide.md", "# Guide\n")

        # User creates an untracked symlink .claude/CLAUDE.md -> ../docs/guide.md
        claude_dir = self.co / ".claude"
        claude_dir.mkdir(parents=True)
        symlink_claude = claude_dir / "CLAUDE.md"
        symlink_claude.symlink_to("../docs/guide.md")

        # Verify is_git_tracked on the symlink returns False (does not resolve leaf)
        self.assertFalse(is_git_tracked(symlink_claude, self.co))

        # Check plan_instructions
        batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        plan = plan_instructions(batch, self.home)
        self.assertTrue(plan.can_apply)

        # The project should NOT enter overlay mode (should target root AGENTS.md, not .agents/AGENTS.md)
        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].target_path, self.co / "AGENTS.md")
        self.assertEqual(plan.operations[0].action, "CREATE")

    # 25. GitRepoTracker 按需按路径查询，不扫描全仓库且能精准缓存
    def test_25_git_repo_tracker_pathspec_and_caching(self) -> None:
        from aikito.git_ops import GitRepoTracker, clear_git_tracker_cache

        _init_git_repo(self.co)
        clear_git_tracker_cache()
        _commit_file(self.co, "AGENTS.md", "team agents\n")
        _commit_file(self.co, "other/unrelated.txt", "unrelated\n")

        tracker = GitRepoTracker(self.co)
        # Preloaded pathspecs cover AGENTS.md
        self.assertIn("AGENTS.md", tracker.tracked_files)
        # unrelated.txt was not in default pathspecs, not yet in tracked_files
        self.assertNotIn("other/unrelated.txt", tracker.tracked_files)

        # Querying unrelated.txt triggers on-demand targeted query
        self.assertTrue(tracker.is_tracked(self.co / "other" / "unrelated.txt"))
        self.assertIn("other/unrelated.txt", tracker.tracked_files)

    # 26. Tracked team instruction deleted from working tree still triggers overlay mode
    def test_26_deleted_tracked_file_remains_overlay_mode_avoiding_typechange(
        self,
    ) -> None:
        from aikito.agents import detect_team_instruction
        from aikito.git_ops import clear_git_tracker_cache

        _init_git_repo(self.co)
        clear_git_tracker_cache()
        _commit_file(self.co, "AGENTS.md", "team instructions\n")

        # Physically remove AGENTS.md from working tree (e.g. rm AGENTS.md)
        (self.co / "AGENTS.md").unlink()
        self.assertFalse((self.co / "AGENTS.md").exists())

        # detect_team_instruction still returns overlay mode True because git tracks it
        is_overlay, public_path = detect_team_instruction(self.co)
        self.assertTrue(is_overlay)
        self.assertEqual(public_path, self.co / "AGENTS.md")

# Aikito PyPI (pip) 打包与分发改造计划

本文档规划将 Aikito 改造为标准 Python Package 并发布至 PyPI 的完整技术实现方案。为确保高稳定性、低回滚风险以及严格控制变更爆炸半径，本方案采取**双阶段分批发布（Two-Stage Rollout）**、**严格界定模块命名边界**并明确**过渡 Stub 的生命周期与淘汰版本**。

---

## 一、 核心架构决策与工程边界

### 1. 双阶段解耦发布策略（Two-Stage Rollout）
一次性同时修改包结构、资源定位、Homebrew 安装方式并向生产 PyPI 发布，回滚代价极高（PyPI 版本不可覆写或删除，Formula 故障会波及所有用户）。因此发布严格解耦为两个阶段：

* **R1 阶段（结构性架构迁移，如 v1.29.0 / v1.29.0-rc1）**：
  * **目标**：完成 `src/aikito/` 目录重组、`importlib.resources` 资源定位、全量 `tests/` 重写、CI 统一改用 `python -m aikito`，以及 Homebrew Formula 同步改造。
  * **验证**：仅在 GitHub Release 及 **TestPyPI**（`https://test.pypi.org`）进行验证，不在生产 PyPI 正式发布。
  * **收益**：隔离 PyPI 不可变性风险；在 Formula 或 Windows 路径出现意外时可从容修复。
* **R2 阶段（PyPI 正式分发与生态切换，如 v1.30.0）**：
  * **目标**：在 R1 经充分验证稳定后，开启生产 PyPI Trusted Publisher 自动发布流水线；全面更新 `README` 安装文档（首推 `uv tool`）；**彻底移除 `bin/` 过渡兼容 Stub**。

---

### 2. 反对“双入口长期共存”：收敛为 `python -m aikito` 并明确 Stub 淘汰期
* **问题**：`bin/aikito` 代理桩（`sys.path.insert + src`）与安装后的 CLI（console_scripts）会形成两套平行的模块检索与资源定位路径，长期共存是持续的复杂度税与隐患源。
* **决议**：
  * **收敛统一入口**：开发环境、文档与 CI 的 50+ 处命令行调用**一律统一改为 `python3 -m aikito ...`**（或已安装环境下的 `aikito` 命令）。
  * **过渡 Stub 定位**：`bin/aikito`、`bin/aikito.cmd`、`bin/aikito.ps1` 仅作为 R1 阶段短期过渡使用，运行期输出 Deprecation 警告（`DeprecationWarning: Invoking via bin/aikito is deprecated and will be removed in v1.30.0. Use 'python -m aikito' or install via pip/uv.`）。
  * **淘汰时间表**：**在 R2（v1.30.0）版本中彻底删除 `bin/` 目录**，不再保留任何源码内的 stub 脚本。

---

### 3. 变更边界收敛：严禁在本次改动中重命名模块
* **决议**：
  * 迁入 `src/aikito/` 后，**完全保留原文件名**（即 `src/aikito/aikito_init.py`、`src/aikito/aikito_sync.py` 等）。
  * 内部与测试导入形式统一为 `from aikito import aikito_init`。
  * **明确禁止**在本次打包改造中顺带将 `aikito_init.py` 简化重构为 `init.py`（或 `aikito.sync`）。模块精简化留待后续独立的日常重构，严格控制本次改造的代码扩散范围。

---

### 4. 唯一版本源与发布门禁（Single Source of Truth & Release Gates）
* **版本常数收敛**：
  * 将 `src/aikito/__init__.py` 中的 `__version__ = "X.Y.Z"` 作为**全项目唯一版本源**。
  * 彻底移除 `bin/` 内的硬编码版本号；`pyproject.toml` 声明 `dynamic = ["version"]`，指向 `src/aikito/__init__.py`。
* **发布前门禁**：
  * 在 `.agents/skills/aikito-release/references/changelog.md` 中新增门禁：发布前必须通过 HTTP API 检查 PyPI 上该版本是否存在（`curl -fsIL https://pypi.org/pypi/aikito/<version>/json` 必须返回 404）。若冲突则阻断并提示递增版本。
  * **提前占名**：在 R1 实施前，先行在 PyPI 注册并发布 0.0.1 最小占位包，锁定 `aikito` 名称所有权。

---

### 5. 静态资源定位与 `TEMPLATES_DIR` 契约
* **移除死代码 Fallback**：
  * 移除 `parents[2] / "templates"` 等掩盖打包遗漏的死代码。
  * 无论是 wheel 解压安装还是源码可编辑安装，包资源均位于文件系统真实路径，使用健壮解析逻辑：
    ```python
    import importlib.resources
    from pathlib import Path

    def _resolve_templates_dir() -> Path:
        try:
            ref = importlib.resources.files("aikito").joinpath("templates")
            path = Path(str(ref))
            if path.is_dir():
                return path
        except (ImportError, OSError):
            pass
        raise RuntimeError(
            "Aikito templates directory is missing. "
            "Ensure the package was installed correctly with package data."
        )

    # 保持模块级常量与 __all__ 导出，兼容模块内 6 处调用
    TEMPLATES_DIR: Path = _resolve_templates_dir()
    ```
* **空目录边界防御**：
  * 确认 `templates/mcps`、`subagents`、`memory/notes` 等空目录不被 git 跟踪且不进 wheel，由 `aikito_init` 运行期的 `mkdir -p` 保证创建。

---

### 6. `pyproject.toml` 改造与 Ignore 白名单校准
* **继承现有配置**：保留现有 `[tool.ruff]` 与 `[tool.ruff.lint]` 规范，更新 `[tool.pytest.ini_options]` 的 `pythonpath = ["src"]`。
* **删除过渡 Loader**：废除 `bin/aikito_cli_loader.py`，入口直接配置为 `[project.scripts] aikito = "aikito.cli:main"`。
* **元数据修复**：
  * 使用 SPDX 格式 `license = "MIT"` 并声明 `license-files = ["LICENSE"]`。
  * `README.md` 中的图片引用替换为 GitHub raw 绝对 URL，防止 PyPI 页面 404。
  * `sdist` 显式纳入 `/tests`、`/LICENSE`、`/README.md`。
* **白名单修正**：将 `.gitignore` 中的 `!/templates/**/AGENTS.md` 更新为 `!/src/aikito/templates/**/AGENTS.md`。

---

### 7. 全量测试套件 (`tests/`) 迁移
* 将所有单测文件中的 flat import 改为包导入（如 `from aikito import aikito_init`）。
* 修正所有 Mock Patch 目标字符串（如 `patch("aikito.aikito_init.CLI_SOURCE_ROOT")`）。
* 迁移后强制通过 `ruff check tests/` 与全量 `pytest -v`。

---

### 8. 源码树防护与命令提示路径修复
* 重构 `CLI_SOURCE_ROOT`：在 `site-packages` 环境下为 `None`；仅当当前环境同时存在 `.git`、`pyproject.toml` 与 `src/aikito` 时才启用源码树拦截。
* 修正 `bin/aikito_mcp.py:2176` 硬编码提示为系统命令 `aikito auth mcp {spec.agent} {spec.server}`。

---

### 9. Homebrew Formula 同步改造（结构性发布）
* 同步修改 `~/homebrew-tap/Formula/aikito.rb`：
  * 安装逻辑调整为安装 `src/` 并生成入口，或使用 `Language::Python::Virtualenv` 进行 wheel 安装。
  * 同步更新 `test do` 路径断言至 `src/` 下对应资源。
  * 按照 `.agents/skills/aikito-release/references/homebrew-tap.md` 执行本地 tap audit 与 test。

---

### 10. CI/CD 与 Smoke Test 加固
* **Wheel 逐文件精确断言**：比对 `git ls-files src/aikito/templates` 与 wheel 内解压文件，确保 **19 个模板文件 1:1 集合严格相等**，并校验 `web/assets/logo.png` 的 SHA256 二进制完整性。
* **CI 命令收敛**：CI 统一改用 `python3 -m aikito ...`；新增独立虚拟环境下的 `pip install .` 测试。
* **Publish Workflow 硬化**（R2 启用）：固定 Actions Commit SHA，配置 `permissions: { contents: read, id-token: write }`、`environment: pypi` 与构建产物 Attestation。

---

## 二、 阶段化发布与实施清单 (Checklist)

### 阶段 0：前置占名与安全准备
- [x] 在 PyPI 注册并发布 `aikito` 0.0.1 最小占位包锁定包名所有权
- [x] 在 PyPI 配置与 GitHub `lsaint/aikito` 关联的 Trusted Publisher (OIDC) 绑定（可先指向 TestPyPI）

---

### 阶段 1 (R1)：包结构改造与多端联动（发布 v1.29.0 / v1.29.0-rc1）
*此阶段不向生产 PyPI 推送，重点在于完成代码架构解耦并验证本地生态稳定性。*

- [x] **代码与静态资源迁移**
  - [x] 创建 `src/aikito/__init__.py` 并声明 `__version__ = "1.29.0"`
  - [x] 迁移 `bin/aikito_*.py` 至 `src/aikito/`（保持原文件名不变）
  - [x] 提取 `src/aikito/cli.py` 与 `src/aikito/__main__.py`，删除硬编码版本号
  - [x] 迁入 `templates/`（19 个跟踪文件）与 `web/` 至 `src/aikito/`
  - [x] 重构 `aikito_templates.py`（保留 `TEMPLATES_DIR` 常量，移除死 fallback）与 `aikito_web.py`
  - [x] 删除 `bin/aikito_cli_loader.py`
  - [x] 修复 `src/aikito/aikito_init.py` 的 `CLI_SOURCE_ROOT` 判定与 `SOURCE_CHECKOUT_MARKERS`
  - [x] 修复 `src/aikito/aikito_mcp.py` 的 `[AUTH]` 提示路径
- [x] **配置与规则**
  - [x] 改造 `pyproject.toml`（Hatchling 后端、动态版本、sdist 包含 tests、SPDX 协议）
  - [x] 更新 `.gitignore` 中的模板白名单（`!/src/aikito/templates/**/AGENTS.md`）
  - [x] 替换 `README.md` 中的图片引用为 GitHub raw 绝对路径
- [x] **测试套件全面适配**
  - [x] 修改 `tests/` 下各文件 import 为 `from aikito import aikito_...`
  - [x] 修正所有 `unittest.mock.patch` 字符串路径
  - [x] 跑通 `ruff check` 与全量 `pytest -v`
- [x] **临时过渡 Stub 与 CI 收敛**
  - [x] 在 `bin/aikito`、`bin/aikito.cmd`、`bin/aikito.ps1` 中加入调用 Deprecation 警告（注明 v1.30.0 移除）
  - [x] 将 `.github/workflows/ci.yml` 中 50+ 处 `python3 bin/aikito` 统一收敛为 `python3 -m aikito`
  - [x] 在 `ci.yml` 的 `smoke-test` 中增加 wheel 19 个模板文件 1:1 逐文件对比与 `logo.png` 哈希断言
  - [x] 在 `ci.yml` 中新增独立 venv 下的 `pip install .` 验证
  - [x] 更新 `windows-smoke-test` 与 `install.ps1`
- [x] **Homebrew Formula 联动与 Release 门禁**
  - [x] 修改 `~/homebrew-tap/Formula/aikito.rb` 并通过 `brew audit` / `brew test`
  - [x] 在 `aikito-release` skill 的 `references/changelog.md` 中添加 PyPI 404 冲突检查门禁
  - [x] 发布 R1 Tag并在 TestPyPI 完成验证安装

---

### 阶段 2 (R2)：PyPI 正式发布与全面切换（发布 v1.30.0）
*在 R1 经两周以上稳定运行、Homebrew 与 Windows 用户验证无回归后执行。*

- [ ] **彻底清理遗留代码**
  - [x] **正式删除 `bin/` 目录**（彻底移除 `bin/aikito`、`bin/aikito.cmd`、`bin/aikito.ps1`）
  - [x] 更新 Windows `install.ps1` 直接使用 `uv tool` 或包安装，移除对 `bin/` 的依赖
- [ ] **启用生产 PyPI 自动化流水线**
  - [x] 创建并启用 `.github/workflows/publish-pypi.yml`（SHA 固定、权限、OIDC、Provenance Attestation）
  - [ ] 执行正式发布，推送到生产 PyPI
- [ ] **文档与用户指引更新**
  - [x] 更新 `README.md` 与 `README.zh-CN.md` 中的 Linux / 跨平台安装指引（首推 `uv tool install aikito`，次选 `pipx install aikito --python 3.12`）
  - [x] 将本地开发与调试指引统一更新为 `python -m aikito`
- [ ] **Homebrew Formula 升级为标准 PyPI Virtualenv 模式**
  - [x] 更新 `aikito-release` skill 中 `references/homebrew-tap.md` 改用 PyPI sdist 源与校验
  - [ ] 在 v1.30.0 发布至 PyPI 后，更新 `Formula/aikito.rb` 为 `Language::Python::Virtualenv` 模式

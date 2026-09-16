# Installation

Keep the CLI installation separate from the canonical user workspace. Prefer
the cross-platform package installation:

```bash
uv tool install aikito
aikito --version
```

Use Homebrew on macOS or Linux when it better fits the host:

```bash
brew install lsaint/tap/aikito
```

Alternatively, use pipx:

```bash
pipx install aikito
```

On Windows, enable Developer Mode before setup so Aikito can create symbolic
links without administrator privileges. Use `uv tool` or `pipx`, reopen the
terminal if the command is not yet on `PATH`, and verify with
`aikito --version`.

Use the selected package manager's upgrade command when upgrading an existing
installation. Clone the source repository only when developing Aikito itself,
and keep that checkout separate from the user workspace.

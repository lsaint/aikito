# Aikito Cloud

[简体中文](remote-sync.zh-CN.md)

!!! note "Synchronization options"
    Aikito uses Git for multi-machine synchronization by default and does not require
    a dedicated server. You can also develop your own synchronization server using
    the [Remote Protocol](architecture/remote-protocol.md), or use Aikito Cloud.
    This guide covers connecting to Aikito Cloud.

    Synchronizing with Aikito Cloud also requires the
    [encrypted runtime](installation.md#macos-linux-with-homebrew).

Connect your Aikito workspace to Aikito Cloud to keep your resources up to date
across machines. You need an Aikito Cloud account and the connection command
shown on your account page.

## Connect your first machine

If you received an invitation, open its link and set your account password.
Otherwise, sign in with your existing account.

Use your existing Aikito workspace. If you have not created one, run:

```bash
aikito init workspace ~/aikito
```

Copy the connection command from your account page and run it in an interactive
terminal. For example, replacing the example URL with your service address:

```bash
aikito remote bind https://sync.example.com
```

1. Open the link printed in the terminal if the browser does not open automatically.
2. Sign in, compare the connection code (such as `dc9b-4280`) with the terminal,
   and approve the connection you just started.
3. Return to the terminal. On the first connection, Aikito initializes encryption
   and saves your recovery code locally. It prints the saved file's location;
   no manual save confirmation is required.

Run your first synchronization:

```bash
aikito remote sync
```

This command applies changes. To inspect them first, optionally run
`aikito remote sync --dry-run` before synchronizing. Connecting alone does not
transfer resources or enable background synchronization.

The commands use your active workspace. If you manage more than one, add
`--workspace /path/to/workspace` to each `aikito remote` command.

## Keep changes synchronized automatically

Start background synchronization on each connected machine:

```bash
aikito remote start
aikito remote status
```

Check that status reports the background service as running. Changes synchronize
while the service is running and the machine can reach your service. Local work
remains available when offline; synchronization resumes when connectivity returns.

To pause background synchronization:

```bash
aikito remote stop
```

You can still synchronize manually with `aikito remote sync`.

## Connect another machine

Install a compatible Aikito client and initialize a workspace on the new machine:

```bash
aikito init workspace ~/aikito
aikito remote bind https://sync.example.com
```

Use the same service address and account. Approve the browser connection, then
enter your recovery code when the terminal asks for it. Aikito verifies the code,
keeps the device's decryption key, and saves a local copy of the recovery code.

Synchronize the existing content and enable background synchronization:

```bash
aikito remote sync
aikito remote start
```

After downloading, [synchronize and verify your local Agent setup](sync-existing-setup.md)
to connect the resources to the Agents installed on this machine.

Synchronization transfers Aikito-managed resources, not your projects' source
repositories. Clone project code separately and
[connect its local path](project-setup.md). You do not need to copy hidden local
state from the other machine.

## Find and back up your recovery code

The default file location on macOS and Linux is:

```text
~/.config/aikito/encrypted-remote/recovery.txt
```

If `XDG_CONFIG_HOME` is set, the file is under
`$XDG_CONFIG_HOME/aikito/encrypted-remote/recovery.txt`. On Windows it is under
`%APPDATA%\aikito\encrypted-remote\recovery.txt`, with `~/.config` as the fallback
when `APPDATA` is not set.

Open the file locally when you need to connect another machine. Each record
contains a recovery code plus the service address and workspace path where it
was saved. Use those labels to find the relevant `code:` value. Reusing the
same synchronization space updates its record rather than adding another copy.
The file is private to your user and stays outside the workspace and synchronization.

Keep a separate copy in a password manager or another safe location: a file on
this machine will not help if the machine itself is lost. Your account password
lets you sign in; it cannot replace a lost recovery code or decrypt your content.

Codes are saved when first generated or successfully entered during recovery.
Reconnecting an older device that only has its decryption key cannot reconstruct
a recovery code that was never backed up.

## Reconnect or disconnect

If an account password reset invalidates your device connection, reconnect with:

```bash
aikito remote bind https://sync.example.com --rebind
```

Aikito reuses your account's existing synchronized content. If this machine still
has its decryption key, it normally does not ask for the recovery code again.

To disconnect the workspace and stop its background synchronization:

```bash
aikito remote unbind
```

Disconnecting preserves local resources and the content stored by your account.
For command options, see the [CLI reference](cli-reference.md#remote-store-synchronization).
For a Git-based workspace transfer, see
[Connect a Workspace on Another Machine](workspace-portability.md).

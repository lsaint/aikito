"""
PowerShell argument completion script generator for Aikito CLI.

Generates a native PowerShell Register-ArgumentCompleter script based on
argparse reflection schema.
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def generate_powershell(parser: argparse.ArgumentParser | None = None) -> str:
    """Generate a native PowerShell completion script for aikito."""
    from aikito_completion import _get_schema

    schema = _get_schema(parser)
    top_flags = json.dumps(sorted(schema["flags"]))
    top_commands = json.dumps(sorted(schema["commands"].keys()))

    # Build commands and subcommands lookup tables for PowerShell
    cmd_sub_dict: dict[str, list[str]] = {}
    cmd_flags_dict: dict[str, list[str]] = {}
    sub_flags_dict: dict[str, list[str]] = {}

    for cmd, data in sorted(schema["commands"].items()):
        subs = sorted(data["subcommands"].keys())
        cmd_sub_dict[cmd] = subs
        cmd_flags_dict[cmd] = sorted(data["flags"])
        for sub, sub_data in sorted(data["subcommands"].items()):
            sub_flags_dict[f"{cmd} {sub}"] = sorted(sub_data["flags"])

    cmd_subs_json = json.dumps(cmd_sub_dict, indent=4)
    cmd_flags_json = json.dumps(cmd_flags_dict, indent=4)
    sub_flags_json = json.dumps(sub_flags_dict, indent=4)

    return f"""# Aikito shell completion for PowerShell.
#
# Installation:
#   Add the following line to your PowerShell profile ($PROFILE):
#     Invoke-Expression (& aikito completion powershell | Out-String)
#
#   To view or edit your profile, run:
#     notepad $PROFILE

Register-ArgumentCompleter -Native -CommandName aikito -ScriptBlock {{
    param($wordToComplete, $commandAst, $cursorPosition)

    $topFlags = {top_flags}
    $topCommands = {top_commands}
    $cmdSubs = ConvertFrom-Json @'
{cmd_subs_json}
'@
    $cmdFlags = ConvertFrom-Json @'
{cmd_flags_json}
'@
    $subFlags = ConvertFrom-Json @'
{sub_flags_json}
'@

    $tokens = @($commandAst.Elements | ForEach-Object {{ $_.Extent.Text }} | Where-Object {{ $_ -ne $null -and $_.Trim() -ne '' }})
    $count = $tokens.Count

    # If the current word being typed is an empty string or partial word at the end
    $isTypingNewWord = $cursorPosition -gt $commandAst.Extent.EndOffset -or $wordToComplete -eq ''
    $argIndex = if ($isTypingNewWord) {{ $count }} else {{ $count - 1 }}

    $cmd = if ($count -gt 1) {{ $tokens[1] }} else {{ '' }}
    $sub = if ($count -gt 2) {{ $tokens[2] }} else {{ '' }}
    $pair = if ($cmd -and $sub) {{ "$cmd $sub" }} else {{ '' }}

    $results = [System.Collections.Generic.List[System.Management.Automation.CompletionResult]]::new()

    function Add-Candidate([string]$val, [string]$tooltip) {{
        if ($val -like "$wordToComplete*") {{
            $tip = if ($tooltip) {{ $tooltip }} else {{ $val }}
            $results.Add([System.Management.Automation.CompletionResult]::new($val, $val, 'ParameterValue', $tip))
        }}
    }}

    function Invoke-Candidates([string]$category) {{
        try {{
            $raw = & aikito completion candidates $category 2>$null
            if ($raw) {{
                $lines = $raw -split "`r?`n" | Where-Object {{ $_ -ne '' }}
                foreach ($line in $lines) {{
                    if ($line -match "^([^\t]+)\t(.*)$") {{
                        Add-Candidate $matches[1] $matches[2]
                    }} else {{
                        Add-Candidate $line $line
                    }}
                }}
            }}
        }} catch {{}}
    }}

    if ($argIndex -le 1) {{
        if ($wordToComplete -like '-*') {{
            foreach ($flag in $topFlags) {{ Add-Candidate $flag $flag }}
        }} else {{
            foreach ($c in $topCommands) {{ Add-Candidate $c "Command: $c" }}
        }}
        return $results
    }}

    if ($argIndex -eq 2) {{
        if ($wordToComplete -like '-*') {{
            if ($cmdFlags.$cmd) {{
                foreach ($f in $cmdFlags.$cmd) {{ Add-Candidate $f $f }}
            }}
        }} else {{
            if ($cmdSubs.$cmd) {{
                foreach ($s in $cmdSubs.$cmd) {{ Add-Candidate $s "Subcommand: $s" }}
            }}
            if ($cmd -in @('adopt', 'init')) {{
                Invoke-Candidates 'paths'
            }}
        }}
        return $results
    }}

    if ($argIndex -ge 3) {{
        if ($wordToComplete -like '-*') {{
            if ($subFlags.$pair) {{
                foreach ($f in $subFlags.$pair) {{ Add-Candidate $f $f }}
            }}
            if ($cmdFlags.$cmd) {{
                foreach ($f in $cmdFlags.$cmd) {{ Add-Candidate $f $f }}
            }}
            return $results
        }}

        switch -Regex ($pair) {{
            '^(show|edit|rename|rm|remove) memory$' {{
                Invoke-Candidates 'memory-completions'
                break
            }}
            '^(show|edit|rm|remove) inbox$' {{
                Invoke-Candidates 'inbox-completions'
                break
            }}
            '^(show|edit) skills?$' {{
                Invoke-Candidates 'skills'
                break
            }}
            '^(show|edit) subagents?$' {{
                Invoke-Candidates 'subagents'
                break
            }}
            '^(show|edit) mcps?$' {{
                Invoke-Candidates 'mcps'
                break
            }}
            '^(show|edit) instructions$' {{
                Add-Candidate 'global' 'Global instruction'
                Add-Candidate '.' 'Current directory project instruction'
                Invoke-Candidates 'projects'
                break
            }}
            '^maintain memory$' {{
                Add-Candidate 'global' 'Global memory'
                Add-Candidate '.' 'Current directory project memory'
                Invoke-Candidates 'projects'
                break
            }}
            '^show projects?$' {{
                Invoke-Candidates 'projects'
                break
            }}
            '^sync project$' {{
                if ($argIndex -eq 3) {{
                    Invoke-Candidates 'projects'
                }} elseif ($argIndex -eq 4) {{
                    Invoke-Candidates 'paths'
                }}
                break
            }}
            '^init project$' {{
                if ($argIndex -eq 4) {{
                    Invoke-Candidates 'paths'
                }}
                break
            }}
            '^init workspace$' {{
                Invoke-Candidates 'paths'
                break
            }}
            '^completion candidates$' {{
                @('projects', 'skills', 'memories', 'subagents', 'mcps', 'inbox', 'paths') | ForEach-Object {{ Add-Candidate $_ $_ }}
                break
            }}
        }}
        return $results
    }}

    return $results
}}
"""

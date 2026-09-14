# Helper for stop_admin.bat: print the process tree around a PID.
#
# Kept as a real .ps1 rather than an inline -Command string because batch and
# PowerShell fight over quoting: batch does not escape {}, %, or |, so an
# inline one-liner has to be mangled beyond readability (and silently breaks
# when PowerShell sees doubled braces). A file sidesteps all of it.
#
# Usage:
#   _proc_tree.ps1 -RootPid 1234 -Mode descendants
#   _proc_tree.ps1 -RootPid 1234 -Mode ancestors
#
# Output: space separated PIDs on a single line, or nothing when there are none.
#   descendants -> deepest first (children before their parent)
#   ancestors   -> nearest ancestor first (parent, grandparent, ...)
#
# NOTE: the parameter is -RootPid, NOT -Pid. $Pid is a read-only automatic
# variable in PowerShell, so binding to it fails with
# "Cannot overwrite variable Pid because it is read-only or constant".
param(
    [Parameter(Mandatory = $true)][int]$RootPid,
    [Parameter(Mandatory = $true)][ValidateSet('descendants', 'ancestors')][string]$Mode
)

$all = Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId

if ($Mode -eq 'descendants') {
    $result = New-Object System.Collections.ArrayList
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($RootPid)
    while ($queue.Count -gt 0) {
        $cur = $queue.Dequeue()
        foreach ($c in $all) {
            if ($c.ParentProcessId -eq $cur) {
                [void]$result.Add([int]$c.ProcessId)
                $queue.Enqueue([int]$c.ProcessId)
            }
        }
    }
    # Reverse so children come before the parents that link them to the root.
    [array]::Reverse($result)
    ($result -join ' ')
    exit 0
}

# ancestors: walk up until we revisit a PID (guards against a looping chain).
#
# SAFETY: the walk stops as soon as a parent looks like it is NOT part of this
# app (no "python" in its image name). Without that guard the chain runs all
# the way up to explorer.exe / the shell / an editor, and the caller would then
# taskkill its way out of the very terminal the user launched us from.
# The app tree is only ever  python -> python, so the check is reliable.
$result = New-Object System.Collections.ArrayList
$seen = @{}
$seen[$RootPid] = $true
$cur = $RootPid
while ($true) {
    $self = $all | Where-Object { $_.ProcessId -eq $cur } | Select-Object -First 1
    if (-not $self) { break }
    $parent = [int]$self.ParentProcessId
    if ($parent -le 0 -or $seen.ContainsKey($parent)) { break }

    $pinfo = Get-CimInstance Win32_Process -Filter "ProcessId = $parent" -ErrorAction SilentlyContinue
    if (-not $pinfo) { break }
    # Stop at the first non-python parent: that is the shell, not our app.
    if ($pinfo.Name -notlike 'python*') { break }

    [void]$result.Add($parent)
    $seen[$parent] = $true
    $cur = $parent
}
($result -join ' ')
exit 0

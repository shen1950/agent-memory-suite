param(
    [switch]$DesktopShortcuts
)
$ErrorActionPreference = 'Stop'

# 定位 Python（要求 3.10+）
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw '没找到 python，请先安装 Python 3.10+ 并加入 PATH' }
$pyExe = $py.Source
# 注意：不要用 -c '...' 传内嵌双引号的代码，PowerShell 调原生程序时会把引号吃掉
$out = (& $pyExe --version 2>&1 | Out-String)
$m = [regex]::Match($out, 'Python (\d+)\.(\d+)')
if (-not $m.Success) { throw "解析不出 Python 版本：$out" }
$ver = "$($m.Groups[1].Value).$($m.Groups[2].Value)"
if ([version]$ver -lt [version]'3.10') { throw "Python 版本过低：$ver，需要 3.10+" }
$pyw = Join-Path (Split-Path $pyExe) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $pyExe }   # 没有 pythonw 就退回带窗口的 python

$root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$home_  = $HOME
$bin    = Join-Path $home_ '.local\bin'
$afDir  = Join-Path $home_ '.agentfind'
$hubDir = Join-Path $home_ '.agenthub'

foreach ($d in @($bin, $afDir, $hubDir)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

# 1) 程序本体
Copy-Item (Join-Path $root 'agentfind.py') (Join-Path $afDir 'agentfind.py') -Force
Copy-Item (Join-Path $root 'agenthub.py')  (Join-Path $hubDir 'agenthub.py') -Force
Copy-Item (Join-Path $root 'agentfind.ico') (Join-Path $afDir 'agentfind.ico') -Force
Write-Host "已安装程序到 $afDir 与 $hubDir"

# 2) 命令入口（Git Bash 走无后缀脚本，cmd/PowerShell 走 .cmd）
$shAf = "#!/bin/sh`nPYTHONIOENCODING=utf-8 exec python -X utf8 `"`$HOME/.agentfind/agentfind.py`" `"`$@`"`n"
$shHub = "#!/bin/sh`nPYTHONIOENCODING=utf-8 exec python -X utf8 `"`$HOME/.agenthub/agenthub.py`" `"`$@`"`n"
$cmdAf = "@echo off`r`nsetlocal`r`nset PYTHONIOENCODING=utf-8`r`npython -X utf8 `"%USERPROFILE%\.agentfind\agentfind.py`" %*`r`nendlocal`r`n"
$cmdHub = "@echo off`r`nsetlocal`r`nset PYTHONIOENCODING=utf-8`r`npython -X utf8 `"%USERPROFILE%\.agenthub\agenthub.py`" %*`r`nendlocal`r`n"
[System.IO.File]::WriteAllText((Join-Path $bin 'agentfind'), $shAf)
[System.IO.File]::WriteAllText((Join-Path $bin 'agenthub'), $shHub)
[System.IO.File]::WriteAllText((Join-Path $bin 'agentfind.cmd'), $cmdAf)
[System.IO.File]::WriteAllText((Join-Path $bin 'agenthub.cmd'), $cmdHub)
Write-Host "已生成命令入口 $bin"

# 3) PATH
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -notlike "*$bin*") {
    [Environment]::SetEnvironmentVariable('Path', "$userPath;$bin", 'User')
    Write-Host "已把 $bin 加入用户 PATH（新开终端生效）"
} else {
    Write-Host 'PATH 里已有该目录'
}

# 4) 可选：桌面双击入口
if ($DesktopShortcuts) {
    # 注意：不要用 pythonw.exe。部分装了国产安全软件的机器上，pythonw.exe 能 listen 但
    # 永远收不到回环连接（防火墙按程序名放行，只放行了 python.exe），双击图标看起来就像程序坏了。
    # 这里用 wscript 以隐藏窗口方式启动 python.exe，效果相同且能被放行。
    $vbsRun = @"
Set s = CreateObject("WScript.Shell")
s.CurrentDirectory = "$afDir"
s.Run Chr(34) & "$pyExe" & Chr(34) & " -X utf8 " & Chr(34) & "$afDir\agentfind.py" & Chr(34) & " serve --no-open", 0, False
WScript.Sleep 2500
s.Run "http://127.0.0.1:8765/", 1, False
"@
    $vbsStop = @"
Set s = CreateObject("WScript.Shell")
s.Run Chr(34) & "$pyExe" & Chr(34) & " -X utf8 " & Chr(34) & "$afDir\agentfind.py" & Chr(34) & " --stop", 0, False
"@
    $runVbs = Join-Path $afDir 'start-hidden.vbs'
    $stopVbs = Join-Path $afDir 'stop.vbs'
    [System.IO.File]::WriteAllText($runVbs, $vbsRun, [System.Text.Encoding]::Unicode)
    [System.IO.File]::WriteAllText($stopVbs, $vbsStop, [System.Text.Encoding]::Unicode)

    $ws = New-Object -ComObject WScript.Shell
    $desk = [Environment]::GetFolderPath('Desktop')
    $icon = Join-Path $afDir 'agentfind.ico'

    $lnk = $ws.CreateShortcut((Join-Path $desk '找 AI 对话.lnk'))
    $lnk.TargetPath = "$env:SystemRoot\System32\wscript.exe"
    $lnk.Arguments = "$q$runVbs$q"
    $lnk.IconLocation = "$icon,0"
    $lnk.WorkingDirectory = $afDir
    $lnk.Description = '跨 Agent 对话检索（无命令行窗口）'
    $lnk.Save()

    $lnk2 = $ws.CreateShortcut((Join-Path $desk '关闭找 AI 对话.lnk'))
    $lnk2.TargetPath = "$env:SystemRoot\System32\wscript.exe"
    $lnk2.Arguments = "$q$stopVbs$q"
    $lnk2.IconLocation = "$icon,0"
    $lnk2.WorkingDirectory = $afDir
    $lnk2.Description = '停止检索服务，索引保留'
    $lnk2.Save()
    Write-Host '已在桌面创建「找 AI 对话 / 关闭找 AI 对话」（wscript 隐藏窗口启动 python.exe）'
}

Write-Host ''
Write-Host '完成。新开一个终端后：'
Write-Host '  agentfind                  起本地网页并打开浏览器'
Write-Host '  agentfind --status         看各产品会话数'
Write-Host '  agenthub mem search "关键词"  查共享记忆'

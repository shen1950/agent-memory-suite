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
ok = False
For i = 1 To 30
    WScript.Sleep 400
    On Error Resume Next
    Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    http.Option(6) = False
    http.Open "GET", "http://127.0.0.1:8765/api/meta", False
    http.Send
    If Err.Number = 0 Then
        If http.Status = 200 Then ok = True
    End If
    On Error GoTo 0
    If ok Then Exit For
Next
If ok Then
    s.Run "http://127.0.0.1:8765/", 1, False
Else
    MsgBox "等了 12 秒，服务端口还是没响应，所以没有打开页面（打开了也是打不开）。" & vbCrLf & _
           "请在终端里跑一次 agentfind 看报错，或双击「关闭找 AI 对话」后重新打开。", _
           vbExclamation, "找 AI 对话"
End If
"@
    # 关闭：把 --stop 的输出落到临时文件再读回来弹框。
    # 不要用 WshShell.Exec(...).StdIn.ReadAll()——装了国产安全软件的机器上这条会抛
    # "错误的文件模式"（连 cmd /c echo 都读不出来），弹框根本不执行，用户点了像没反应。
    $stopCmd = @"
@echo off
"$pyExe" -X utf8 "$afDir\agentfind.py" --stop > "%TEMP%\agentfind_stop.txt" 2>&1
"@
    # .cmd 由 cmd.exe 按本地 ANSI 码页解析，用 Unicode 写会让中文用户名路径失效
    [System.IO.File]::WriteAllText((Join-Path $afDir 'stop.cmd'), $stopCmd, [System.Text.Encoding]::Default)
    $vbsStop = @"
Set s = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
tmp = s.ExpandEnvironmentStrings("%TEMP%") & "\agentfind_stop.txt"
If fso.FileExists(tmp) Then fso.DeleteFile tmp, True
s.Run Chr(34) & "$afDir\stop.cmd" & Chr(34), 0, True
msg = "已经关掉检索服务了，索引文件都留着，下次双击「找 AI 对话」还能用。"
If fso.FileExists(tmp) Then
    Set st = CreateObject("ADODB.Stream")
    st.Type = 2
    st.Charset = "utf-8"
    st.Open
    st.LoadFromFile tmp
    txt = Trim(st.ReadText(-1))
    st.Close
    If Len(txt) > 0 Then msg = txt
End If
MsgBox msg, vbInformation, "关闭找 AI 对话"
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
    $lnk.Arguments = "`"$runVbs`""
    $lnk.IconLocation = "$icon,0"
    $lnk.WorkingDirectory = $afDir
    $lnk.Description = '跨 Agent 对话检索（无命令行窗口）'
    $lnk.Save()

    $lnk2 = $ws.CreateShortcut((Join-Path $desk '关闭找 AI 对话.lnk'))
    $lnk2.TargetPath = "$env:SystemRoot\System32\wscript.exe"
    $lnk2.Arguments = "`"$stopVbs`""
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

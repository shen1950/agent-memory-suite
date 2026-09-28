param(
    [switch]$RemoveData
)
$ErrorActionPreference = 'SilentlyContinue'

$home_ = $HOME
$bin   = Join-Path $home_ '.local\bin'

# 先停服务（会按命令行匹配杀掉所有 agentfind 进程）
$af = Join-Path $home_ '.agentfind\agentfind.py'
if (Test-Path $af) { & python -X utf8 $af --stop | Out-Null }

# 命令入口
foreach ($f in @('agentfind', 'agentfind.cmd', 'agenthub', 'agenthub.cmd')) {
    Remove-Item (Join-Path $bin $f) -Force
}

# 桌面快捷方式
$desk = [Environment]::GetFolderPath('Desktop')
Remove-Item (Join-Path $desk '找 AI 对话.lnk') -Force
Remove-Item (Join-Path $desk '关闭找 AI 对话.lnk') -Force

if ($RemoveData) {
    Remove-Item (Join-Path $home_ '.agentfind') -Recurse -Force
    Remove-Item (Join-Path $home_ '.agenthub') -Recurse -Force
    Write-Host '已删除程序与全部数据（索引、共享记忆、派工留痕）'
} else {
    Write-Host '已移除命令与快捷方式；索引与共享记忆保留在 ~/.agentfind 与 ~/.agenthub'
    Write-Host '要连数据一起删：uninstall.ps1 -RemoveData'
}

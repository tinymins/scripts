@echo off
setlocal enabledelayedexpansion

set "scriptName=converter_mkv_h265_vbr_10000_15000_seq.bat"
set "contextMenu=MKV H.265 VBR-10000-15000"

:: 获取批处理文件的路径
for %%I in ("%~dp0%scriptName%") do set "scriptPath=%%~fI"

:: 文件类型数组
set "fileTypes=.mkv .mp4 .avi .mov"

:: 遍历文件类型
for %%F in (%fileTypes%) do (
    :: 注册表键的路径
    set "keyName=HKCR\SystemFileAssociations\%%F\shell\FFmpeg convert to..."
    set "subKeyName=HKCR\SystemFileAssociations\%%F\shell\FFmpeg convert to...\shell\!contextMenu!"

    :: 创建一级菜单注册表键
    reg add "!keyName!" /v "Icon" /t REG_SZ /d "%SystemRoot%\system32\shell32.dll,5" /f
    reg add "!keyName!" /v "MUIVerb" /t REG_SZ /d "FFmpeg convert to..." /f
    reg add "!keyName!" /v "SubCommands" /t REG_SZ /d "" /f

    :: 创建二级菜单注册表键
    reg add "!subKeyName!" /v "MUIVerb" /t REG_SZ /d "!contextMenu!" /f
    reg add "!subKeyName!\command" /ve /d """%scriptPath%"" ""%%V""" /f
)

endlocal

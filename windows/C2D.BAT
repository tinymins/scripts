::::::::::::::::::::::::::::::::::::::::::
::　名称：WIN7个人文件夹转移工具　　　　::
::　作者：茗伊          　　　　　　　　::
::　版本：v2.0 2013-2-20　　　　　　　　::
::　用途：可以转移WIN7操作系统下的　　　::
::　　　　个人文件夹（使用硬链接方式）　::
::　　注：如需转移临时文件请务必使用第　::
::　　　　三方引导，否则会出现文件占用　::
::::::::::::::::::::::::::::::::::::::::::
@echo off
color 0A
title 个人文件C→D - By ZYM @ ZhaiYiMing.CoM
setlocal EnableDelayedExpansion
mode con cols=90 lines=30 & REM cols是设置宽度，lines是设置高度
REM 确认管理员权限
set UAC=0
bcdedit>nul
if errorlevel 1 set UAC=1
if %UAC%==1 (
	color CE
	set exitconf=N
	echo 没有管理员权限，请以管理员权限运行该程序！
	set /p exitconf=输入y忽略管理员权限验证，造成的后果自行承担：
	if /i NOT !exitconf!==Y (goto :end)
)
REM 已确认管理员权限

REM 设置分区盘符
:setDriverName
color 0A
cls
set tempFlag=n
set currentSystemDriver=C
set currentBackupDriver=D
set targetBackupDriver=D
echo 【分区设置】
echo ＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝
set /p currentSystemDriver=请输入当前文件系统下需转移数据的Win7系统所在分区盘符(默认：C):
set /p currentBackupDriver=请输入当前文件系统下文件转移目标所在分区盘符(默认：D):
set /p  targetBackupDriver=请输入目标文件系统下文件转移目标所在分区盘符(默认：D):
echo ＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝
color 0E
echo 当前文件系统下需转移数据的Win7系统所在分区："%currentSystemDriver%:\"
echo 当前文件系统下文件转移目标所在分区：　　　　"%currentBackupDriver%:\"
echo 目标文件系统下文件转移目标所在分区：　　　　"%targetBackupDriver%:\"
set /p tempFlag=请输入Y确认以上信息,否则请重新输入(Y/N)：
if /i NOT %tempFlag%==Y goto :setDriverName

REM 每轮操作开始时的变量初始化
:start
color 0A
echo off
cls
set userLine=N
set moveAppData=N
set moveAppDataLocal=N
set moveAppDataLocalLow=N
set moveAppDataRoaming=N
set userConform=Null

REM 选择需要转移的用户
echo Shift +F10
echo =======================
echo 本机用户列表：
set n=0
for /f "delims=" %%i in ('dir %currentSystemDriver%:\Users /b /a:d /o') do (
	set /a n+=1
	set user!n!=%%i
	echo !n!.%%i
)
set totalUsers=!n!
echo =======================
set /p userLine=请选择需要转移文件的用户(输入0退出,X to input yourself):
if %userLine%==0 goto :end
if /i %userLine%==N ( goto:start )
if /i %userLine%==X (
	set sourceUserName=
	set /p sourceUserName="请输入需要转移文件的用户名: "
	set targetUserName=!sourceUserName!
	set /p targetUserName="请输入您希望转移到的文件夹(默认'!sourceUserName!'): "
) else (
	if %userLine% GTR %totalUsers% (
		color 0E
		echo 输入的用户编号超出范围
		pause
		goto :start
	)
	if %userLine% LSS 1 (
		color 0E
		echo 输入的用户编号无效
		pause
		goto :start
	)
	call set sourceUserName=%%user%userLine%%%
	set targetUserName=!sourceUserName!
)
if "%sourceUserName%"=="" (
	color 0E
	echo 输入的源用户名错误
	pause
	goto :start
)
if "%targetUserName%"=="" (
	color 0E
	echo 输入的目标用户名错误
	goto :start
)

REM 选择需要转移的文件夹
set /p moveAppData="是否转移"AppData"文件夹？(该文件夹包含应用程序数据及缓存)(Y/N): "
if /i NOT %moveAppData%==y (
	set /p moveAppDataLocal="是否转移"AppData\Local"文件夹？(该文件夹包含某些应用程序数据及缓存 如Chrome浏览器)(Y/N): "
	set /p moveAppDataLocalLow="是否转移"AppData\LocalLow"文件夹？(该文件夹用来存放共享数据)(Y/N): "
	set /p moveAppDataRoaming="是否转移"AppData\Roaming"文件夹？(该文件夹存放一些使用程序后产生的数据文件，如空间听音乐 登入的号码等而缓存的一些数据)(Y/N): "
)

REM 确认信息
:userConfirm
color 0E
echo =======================
echo ☆☆☆☆☆☆请确认信息(Y/N)☆☆☆☆☆☆
echo ●需要转移的用户文件夹：　　　　"%currentSystemDriver%:\Users\%sourceUserName%\"
echo ●希望转移到的目标文件夹：　　　"%currentBackupDriver%:\Users\%targetUserName%\"
echo ●目标文件夹在目标系统中的位置："%targetBackupDriver%:\Users\%targetUserName%\"
echo ●希望转移的目录如下：
if /i %moveAppData%==y echo ◎整个"AppData"文件夹(包括Local,LocalLow,Roaming文件夹)
if /i %moveAppDataLocal%==y echo ◎"AppData\\Local"文件夹
if /i %moveAppDataLocalLow%==y echo ◎"AppData\\LocalLow"文件夹
if /i %moveAppDataRoaming%==y echo ◎"AppData\\Roaming"文件夹
set /p userConform="请确认(Y/N):"
if /i %userConform%==N goto :start
if /i NOT %userConform%==Y goto :userConfirm

REM 准备就绪，开始转移。
color 0A
echo ＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝
echo 按任意键开始用户"%sourceUserName%"个人文件转移
pause
@echo on

@REM 判断用户类型
@set userType=1
@if /i "%sourceUserName%"=="All Users" ( @set userType=0 )
@if /i "%sourceUserName%"=="Default" ( @set userType=0 )
@if /i "%sourceUserName%"=="Default User" ( @set userType=0 )
@if /i "%sourceUserName%"=="Public" ( @set userType=0 )
@REM 开始转移数据
@if /i %moveAppData%==Y (
	call :MoveFolder "" "AppData" %userType%
) else (
	if /i %moveAppDataLocal%==Y (
		call :MoveFolder "AppData\" "Local" %userType%
	) else (
		call :MoveFolder "AppData\Local\" "TslGame" %userType%
		call :MoveFolder "AppData\Local\" "JianyingPro" %userType%
		call :MoveFolder "AppData\Local\" "Plex" %userType%
	)
	if /i %moveAppDataLocalLow%==Y call :MoveFolder "" "AppData\LocalLow" %userType%
	if /i %moveAppDataRoaming%==Y call :MoveFolder "" "AppData\Roaming" %userType%
)
@call :MoveFolder "" "Saved Games" %userType%
@call :MoveFolder "" "Contacts" %userType%
@call :MoveFolder "" "Links" %userType%
@call :MoveFolder "" "Favorites" %userType%
@call :MoveFolder "" "Searches" %userType%
@call :MoveFolder "" "Videos" %userType%
@call :MoveFolder "" "Pictures" %userType%
@call :MoveFolder "" "Documents" %userType%
@call :MoveFolder "" "Music" %userType%
@call :MoveFolder "" "Downloads" %userType%
@call :MoveFolder "" "Desktop" %userType%
@call :MoveFolder "" "Virtual Machines" %userType%
@call :MoveFolder "" ".SwitchHosts" %userType%
@call :MoveFolder "" ".android" %userType%
@call :MoveFolder "" ".idlerc" %userType%
@call :MoveFolder "" ".ssh" %userType%
@call :MoveFolder "" ".vscode" %userType%
@call :MoveFolder "" ".config" %userType%
@echo ＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝
@echo %sourceUserName%用户文件转移结束...
@pause
@goto :start

:end
@echo 茗伊 @ QQ8421854
@pause
@exit

:MoveFolder
	@set fldPath=%1
	@set fldPath=%fldPath:"=%
	@set fldName=%2
	@set fldName=%fldName:"=%
	@set userType=%3
	@REM 判断如果已经是硬链接则直接删除目录点
	@for /f "tokens=*" %%a in ('dir /b /a:l "%currentSystemDriver%:\Users\%sourceUserName%\%fldPath%" 2^>nul') do @(
		@if "%%a"=="%fldName%" @rmdir "%currentSystemDriver%:\Users\%sourceUserName%\%fldPath%%fldName%" /S /Q
	)
	@REM 目标位置不存在则创建
	@if not exist "%currentBackupDriver%:\Users\%targetUserName%\%fldPath%%fldName%" @mkdir "%currentBackupDriver%:\Users\%targetUserName%\%fldPath%%fldName%"
	@REM 原始位置存在则移动目录树并创建硬链接 否则根据用户类型决定是否创建
	@if exist "%currentSystemDriver%:\Users\%sourceUserName%\%fldPath%%fldName%" (
		@robocopy "%currentSystemDriver%:\Users\%sourceUserName%\%fldPath%%fldName%" "%currentBackupDriver%:\Users\%targetUserName%\%fldPath%%fldName%" /E /COPYALL /XJ
		@rmdir "%currentSystemDriver%:\Users\%sourceUserName%\%fldPath%%fldName%" /S /Q
		@mklink /J "%currentSystemDriver%:\Users\%sourceUserName%\%fldPath%%fldName%" "%targetBackupDriver%:\Users\%targetUserName%\%fldPath%%fldName%"
	) else (
		@if "%userType%"=="1" @mklink /J "%currentSystemDriver%:\Users\%sourceUserName%\%fldPath%%fldName%" "%targetBackupDriver%:\Users\%targetUserName%\%fldPath%%fldName%"
	)
	@goto :eof

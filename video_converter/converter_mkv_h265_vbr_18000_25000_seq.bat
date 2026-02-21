@echo off
setlocal enabledelayedexpansion
%~d0
cd %~dp0

set "outputSuffix=.h265"
set "outputExtName=mkv"

for %%I in (%*) do (
  set "inputFile=%%~I"
  set "inputName=%%~dpnI"
  set "outputFile=%%~dpnI!outputSuffix!.!outputExtName!"
  set "counter=1"

  echo Input File:  "!inputFile!"

  :loop
  if exist "!outputFile!" (
    set "outputFile=!inputName! (!counter!)!outputSuffix!.!outputExtName!"
    set /a counter+=1
    goto :loop
  )

  echo Output File: "!outputFile!"

  "%~dp0..\.vendor\ffmpeg\ffmpeg.exe" -i "!inputFile!" ^
    -map 0 ^
    -c:v hevc_nvenc ^
    -b:v 18000k ^
    -maxrate 25000k ^
    -rc:v vbr ^
    -preset p7 ^
    -tune hq ^
    -profile:v main ^
    -rc-lookahead 32 ^
    -spatial_aq 1 ^
    -bf 4 ^
    -pass 1 ^
    -f null /dev/null
  "%~dp0..\.vendor\ffmpeg\ffmpeg.exe" -i "!inputFile!" ^
    -map 0 ^
    -c:v hevc_nvenc ^
    -b:v 18000k ^
    -maxrate 25000k ^
    -rc:v vbr ^
    -preset p7 ^
    -tune hq ^
    -profile:v main ^
    -rc-lookahead 32 ^
    -spatial_aq 1 ^
    -bf 4 ^
    -pass 2 ^
    -c:a copy ^
    "!outputFile!"

  @REM copy /B "!outputFile!" +,, & copy /B "!inputFile!" +,, "!outputFile!"
  "%~dp0..\.vendor\nircmd\nircmd.exe" clonefiletime "!inputFile!" "!outputFile!"
)

endlocal
pause

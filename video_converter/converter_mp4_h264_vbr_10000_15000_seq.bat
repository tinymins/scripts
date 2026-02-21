@echo off
setlocal enabledelayedexpansion
%~d0
cd %~dp0

set "outputSuffix=.h264"
set "outputExtName=mp4"

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

  "%~dp0..\third_party\ffmpeg\ffmpeg.exe" -i "!inputFile!" ^
    -map 0 ^
    -c:v h264_nvenc ^
    -b:v 18000k ^
    -maxrate 25000k ^
    -rc:v vbr ^
    -preset p7 ^
    -profile:v high ^
    -bf 4 ^
    -r 60 ^
    -pass 1 ^
    -f null /dev/null
  "%~dp0..\third_party\ffmpeg\ffmpeg.exe" -i "!inputFile!" ^
    -map 0 ^
    -c:v h264_nvenc ^
    -b:v 18000k ^
    -maxrate 25000k ^
    -rc:v vbr ^
    -preset p7 ^
    -profile:v high ^
    -bf 4 ^
    -r 60 ^
    -pass 2 ^
    -c:a copy ^
    "!outputFile!"

  @REM copy /B "!outputFile!" +,, & copy /B "!inputFile!" +,, "!outputFile!"
  "%~dp0..\third_party\nircmd\nircmd.exe" clonefiletime "!inputFile!" "!outputFile!"
)

endlocal
pause

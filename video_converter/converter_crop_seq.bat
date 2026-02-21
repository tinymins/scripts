@echo off
setlocal enabledelayedexpansion
%~d0
cd %~dp0

set "outputSuffix=.crop"
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

  "%~dp0..\.vendor\ffmpeg\ffmpeg.exe" -i "!inputFile!" ^
    -vf "crop=1280:590:0:130" ^
    "!outputFile!"

  @REM copy /B "!outputFile!" +,, & copy /B "!inputFile!" +,, "!outputFile!"
  "%~dp0..\.vendor\nircmd\nircmd.exe" clonefiletime "!inputFile!" "!outputFile!"
)

endlocal
pause

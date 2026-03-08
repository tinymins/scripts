@echo off
for /f "tokens=1" %%d in ('manage-bde -status ^| findstr /r "^Volume"') do (
    manage-bde -status %%d | findstr /c:"Protection On" >nul && (
        echo Decrypting %%d
        manage-bde -off %%d
    )
)
pause

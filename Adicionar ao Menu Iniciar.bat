@echo off
setlocal

set "CONTROLE_BAT=%~dp0Abrir Controle de Projeto.bat"
set "CONTROLE_ICO=%~dp0assets\Controle de Projeto Logo.ico"
set "CONTROLE_ATALHO=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Controle de Projeto.lnk"
set "ATALHO_ANTIGO=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Controle de Projetos Tekla.lnk"

if not exist "%CONTROLE_BAT%" (
    echo Nao foi possivel localizar o inicializador do programa.
    pause
    exit /b 1
)

if not exist "%CONTROLE_ICO%" (
    echo Nao foi possivel localizar o icone do programa.
    pause
    exit /b 1
)

if exist "%ATALHO_ANTIGO%" (
    del "%ATALHO_ANTIGO%" >nul 2>&1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$atalho = (New-Object -ComObject WScript.Shell).CreateShortcut($env:CONTROLE_ATALHO); $atalho.TargetPath = $env:CONTROLE_BAT; $atalho.WorkingDirectory = Split-Path -Parent $env:CONTROLE_BAT; $atalho.IconLocation = $env:CONTROLE_ICO + ',0'; $atalho.Description = 'Controle de Projeto'; $atalho.Save()"

if errorlevel 1 (
    echo Nao foi possivel adicionar o atalho ao Menu Iniciar.
    pause
    exit /b 1
)

echo Controle de Projeto adicionado ao Menu Iniciar com sucesso.
pause

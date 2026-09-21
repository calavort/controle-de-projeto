@echo off
setlocal
chcp 65001 >nul

rem Cria o atalho "Controle de Projeto" no Menu Iniciar apontando DIRETO para o
rem pyw.exe: assim o programa abre sem console e sem o flash preto. A reserva,
rem quando nao houver pyw/pythonw, e o "Abrir Controle de Projeto.bat".

set "APPDIR=%~dp0"
set "SCRIPT=%APPDIR%CONTROLE DE PROJETO.py"
set "FALLBACK=%APPDIR%Abrir Controle de Projeto.bat"
set "ICON=%APPDIR%assets\Controle de Projeto Logo.ico"
set "STARTMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "LNK=%STARTMENU%\Controle de Projeto.lnk"
set "ATALHO_ANTIGO=%STARTMENU%\Controle de Projetos Tekla.lnk"

if not exist "%SCRIPT%" (
    echo Nao encontrei "CONTROLE DE PROJETO.py" nesta pasta.
    pause
    exit /b 1
)

if exist "%ATALHO_ANTIGO%" del "%ATALHO_ANTIGO%" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -Command "$q=[char]34; $py=(Get-Command pyw -ErrorAction SilentlyContinue).Source; if(-not $py){$py=(Get-Command pythonw -ErrorAction SilentlyContinue).Source}; $w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut($env:LNK); if($py){$s.TargetPath=$py; $s.Arguments=$q+$env:SCRIPT+$q}else{$s.TargetPath=$env:FALLBACK}; $s.WorkingDirectory=$env:APPDIR; if(Test-Path $env:ICON){$s.IconLocation=$env:ICON}; $s.WindowStyle=7; $s.Description='Controle de Projeto'; $s.Save()"

if exist "%LNK%" (
    echo.
    echo Atalho "Controle de Projeto" criado no Menu Iniciar ^(sem console, sem flash^).
) else (
    echo.
    echo Nao foi possivel criar o atalho.
)
echo.
pause

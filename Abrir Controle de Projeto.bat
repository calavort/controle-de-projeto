@echo off
REM Abre o Controle de Projeto SEM a janela preta (console). O programa sobe o
REM servidor local e abre no navegador sozinho.
REM
REM Este .bat e a RESERVA: o atalho do Menu Iniciar aponta direto para o pyw.exe,
REM que nao pisca nem esta janela. Para ver os erros, rode num terminal:
REM     python "CONTROLE DE PROJETO.py"
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw "%~dp0CONTROLE DE PROJETO.py"
) else (
  start "" pythonw "%~dp0CONTROLE DE PROJETO.py"
)

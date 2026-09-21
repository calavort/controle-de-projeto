@echo off
REM RESERVA. O jeito certo de abrir e o "Controle_de_Projeto.pyw" - o Windows
REM abre .pyw com o pythonw.exe, que nao tem console nenhum, nem para piscar.
REM
REM Este .bat existe so para nao quebrar atalhos antigos que apontavam para ele.
REM Rode uma vez o "criar_atalho_menu_iniciar.pyw" e o atalho do Menu Iniciar
REM passa a abrir direto pelo pythonw, sem flash.
cd /d "%~dp0"
start "" "%~dp0Controle_de_Projeto.pyw"

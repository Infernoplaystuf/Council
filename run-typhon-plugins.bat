@echo off
REM ============================================================
REM  run-typhon-plugins.bat - "Typhon (plugins)": THIS checkout's Council,
REM  run beside today's Typhon on a vault, an app folder and a plugin home
REM  of its own. Read docs\typhon_plugins_branch.md first.
REM
REM  For this window only (nothing is set for the PC), it sets
REM    COUNCIL_VAULT_ROOT   %USERPROFILE%\Typhon-plugins-vault
REM                         the side vault: its Designer projects, its own
REM                         classifier store (classifiers\), its settings
REM    COUNCIL_APP_DIR      %USERPROFILE%\Typhon-plugins-app
REM                         the side twin of ~\.council
REM    TYPHON_PLUGIN_HOME   %USERPROFILE%\Typhon-plugins-app\typhon
REM                         the side twin of ~\.council\typhon (the plugin
REM                         home: base set, approvals, third-party plugins)
REM    COUNCIL_SKIP_PATH_MIGRATION=1
REM                         an old build's files in the app folder are never
REM                         moved into the side vault (the Council moves
REM                         them at every start otherwise)
REM    FRAME_CLASSES_STORE  cleared: a project's classifier_store.json says
REM                         where its classifiers are
REM    TYPHON_PLUGINS_SIDE=1  says this window is the side Council's
REM  and then starts the Council from this folder through run-windows.bat,
REM  so it finds its Python and model the same way.
REM
REM  Nothing is created under %USERPROFILE%\.council. A side folder there, or
REM  one that is (or holds, or sits in) the vault or app folder this window
REM  already names, is refused before anything starts.
REM
REM  Usage:
REM    run-typhon-plugins.bat            start "Typhon (plugins)"'s Council
REM    run-typhon-plugins.bat --where    print the folders, start nothing
REM    set TYPHON_PLUGINS_VAULT=D:\tp-vault   (other side folders, set first)
REM    set TYPHON_PLUGINS_APP=D:\tp-app
REM  Any other argument goes on to the Council as typed.
REM ============================================================
setlocal enableextensions disabledelayedexpansion

set "HERE=%~dp0"
if not defined USERPROFILE (
    echo [typhon-plugins] USERPROFILE is not set, so the side folders have no place.
    exit /b 2
)

REM -- The side folders: the defaults, or the ones asked for ------------
set "SIDE_VAULT=%USERPROFILE%\Typhon-plugins-vault"
if defined TYPHON_PLUGINS_VAULT set "SIDE_VAULT=%TYPHON_PLUGINS_VAULT%"
set "SIDE_APP=%USERPROFILE%\Typhon-plugins-app"
if defined TYPHON_PLUGINS_APP set "SIDE_APP=%TYPHON_PLUGINS_APP%"
REM Full paths, so the checks below compare like with like.
for %%I in ("%SIDE_VAULT%") do set "SIDE_VAULT=%%~fI"
for %%I in ("%SIDE_APP%") do set "SIDE_APP=%%~fI"
for %%I in ("%USERPROFILE%\.council") do set "REAL_APP=%%~fI"
set "SIDE_HOME=%SIDE_APP%\typhon"

REM -- Refusals ---------------------------------------------------------
REM Never under the Council's own app folder (~\.council, the real vault's
REM home). Never the vault or app folder this window already names - unless
REM this window IS a side Council's (TYPHON_PLUGINS_SIDE) naming the same
REM side folders, as a Council started from the side Council's would.
call :refuse_inside "%SIDE_VAULT%" "%REAL_APP%" "the side vault" || exit /b 3
call :refuse_inside "%SIDE_APP%" "%REAL_APP%" "the side app folder" || exit /b 3
if defined COUNCIL_VAULT_ROOT (
    for %%I in ("%COUNCIL_VAULT_ROOT%") do call :refuse_overlap "%SIDE_VAULT%" "%%~fI" "the side vault" "COUNCIL_VAULT_ROOT" || exit /b 3
)
if defined COUNCIL_APP_DIR (
    for %%I in ("%COUNCIL_APP_DIR%") do call :refuse_overlap "%SIDE_APP%" "%%~fI" "the side app folder" "COUNCIL_APP_DIR" || exit /b 3
)

if /i "%~1"=="--where" goto :where

REM -- This window's environment ------------------------------------------
set "COUNCIL_VAULT_ROOT=%SIDE_VAULT%"
set "COUNCIL_APP_DIR=%SIDE_APP%"
set "TYPHON_PLUGIN_HOME=%SIDE_HOME%"
set "COUNCIL_SKIP_PATH_MIGRATION=1"
set "FRAME_CLASSES_STORE="
set "TYPHON_PLUGINS_SIDE=1"

if not exist "%SIDE_VAULT%\" mkdir "%SIDE_VAULT%" || exit /b 4
if not exist "%SIDE_APP%\" mkdir "%SIDE_APP%" || exit /b 4
if not exist "%SIDE_HOME%\" mkdir "%SIDE_HOME%" || exit /b 4

REM -- Python: run-windows.bat's own order first (.council_python beside it,
REM COUNCIL_PYTHON, .venv, the conda env wizardCouncil); when none of those
REM is there, the conda env "council" this branch is tested in.
if exist "%HERE%.council_python" goto :python_chosen
if defined COUNCIL_PYTHON goto :python_chosen
if exist "%HERE%.venv\Scripts\python.exe" goto :python_chosen
set "WIZARD="
for %%B in ("%USERPROFILE%\miniconda3" "%USERPROFILE%\anaconda3" "%USERPROFILE%\miniforge3" "%LOCALAPPDATA%\miniconda3" "%ProgramData%\miniconda3" "%ProgramData%\Anaconda3" "C:\miniconda3" "C:\Anaconda3") do (
    if exist "%%~B\envs\wizardCouncil\python.exe" set "WIZARD=1"
)
if defined WIZARD goto :python_chosen
for %%B in ("%USERPROFILE%\miniconda3" "%USERPROFILE%\anaconda3" "%USERPROFILE%\miniforge3" "%LOCALAPPDATA%\miniconda3" "%ProgramData%\miniconda3" "%ProgramData%\Anaconda3" "C:\miniconda3" "C:\Anaconda3") do (
    if not defined COUNCIL_PYTHON if exist "%%~B\envs\council\python.exe" set "COUNCIL_PYTHON=%%~B\envs\council\python.exe"
)
:python_chosen

echo [typhon-plugins] checkout:     %HERE%
echo [typhon-plugins] vault:        %COUNCIL_VAULT_ROOT%
echo [typhon-plugins] app folder:   %COUNCIL_APP_DIR%
echo [typhon-plugins] plugin home:  %TYPHON_PLUGIN_HOME%
set "CUT=%HERE:\.claude\worktrees\=%"
if not "%CUT%"=="%HERE%" echo [typhon-plugins] WARNING: this checkout is under .claude\worktrees, which tools clean up on their own - an app built from it breaks when it goes. Use a checkout at a path you chose, such as %USERPROFILE%\Typhon-plugins.
call "%HERE%run-windows.bat" %*
endlocal & exit /b %ERRORLEVEL%

:where
echo checkout=%HERE%
echo vault=%SIDE_VAULT%
echo app=%SIDE_APP%
echo plugin_home=%SIDE_HOME%
echo classifiers=%SIDE_VAULT%\classifiers
echo projects=%SIDE_VAULT%\gui_projects
endlocal & exit /b 0

REM -- :refuse_inside <path> <root> <what>: fails when <path> is <root> or
REM inside it. Both are full paths, so <root> (it starts with a drive) can
REM only be found at the front of <path>; cmd's text replace ignores case.
:refuse_inside
setlocal enabledelayedexpansion
set "P=%~1\"
set "CUT=!P:%~2\=!"
if not "!CUT!"=="!P!" goto :inside
endlocal & exit /b 0
:inside
echo [typhon-plugins] Refused: %~3 %~1 is inside %~2, the Council's own app folder, where the plugin Typhon must never write.
endlocal & exit /b 1

REM -- :refuse_overlap <path> <other> <what> <variable>: fails when the two
REM are one folder or one holds the other - except in a side Council's own
REM window naming the same side folder.
:refuse_overlap
setlocal enabledelayedexpansion
set "A=%~1\"
set "B=%~2\"
if /i "!A!"=="!B!" if "%TYPHON_PLUGINS_SIDE%"=="1" (endlocal & exit /b 0)
set "CUT_A=!A:%~2\=!"
set "CUT_B=!B:%~1\=!"
if not "!CUT_A!"=="!A!" goto :overlap
if not "!CUT_B!"=="!B!" goto :overlap
endlocal & exit /b 0
:overlap
echo [typhon-plugins] Refused: %~3 %~1 overlaps %~2, which %~4 already names in this window - the folder of the Council started here, not a side one.
endlocal & exit /b 1

@echo off
rem Diagnostic build: capturing full environment/sys.path info before
rem attempting the real launch, since the exact same absolute-path
rem pythonw.exe binary works when tested from the dev session but fails
rem with ModuleNotFoundError when double-clicked for real -- something
rem about the environment itself differs, not the interpreter or the path.
cd /d "C:\Users\aaron\Claude and Projects\Projects\BackstageHero_LibraryHygiene"

echo ==== launch at %DATE% %TIME% ==== > launch_log.txt
echo --- env vars --- >> launch_log.txt
echo APPDATA=%APPDATA% >> launch_log.txt
echo USERPROFILE=%USERPROFILE% >> launch_log.txt
echo PYTHONPATH=%PYTHONPATH% >> launch_log.txt
echo PYTHONNOUSERSITE=%PYTHONNOUSERSITE% >> launch_log.txt
echo PYTHONHOME=%PYTHONHOME% >> launch_log.txt
echo PATH=%PATH% >> launch_log.txt
echo --- python diagnostics --- >> launch_log.txt
"C:\Python314\pythonw.exe" -c "import sys, site, os; f=open('diag_out.txt','w'); f.write('executable=' + sys.executable + chr(10)); f.write('prefix=' + sys.prefix + chr(10)); f.write('ENABLE_USER_SITE=' + str(site.ENABLE_USER_SITE) + chr(10)); f.write('USER_SITE=' + str(site.getusersitepackages()) + chr(10)); f.write('sys.path=' + chr(10).join(sys.path) + chr(10)); f.close()" >> launch_log.txt 2>&1
type diag_out.txt >> launch_log.txt 2>nul
del diag_out.txt 2>nul

echo --- actual app launch --- >> launch_log.txt
"C:\Python314\pythonw.exe" gui.py >> launch_log.txt 2>&1
echo ==== exited with code %ERRORLEVEL% at %DATE% %TIME% ==== >> launch_log.txt

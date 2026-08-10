@echo off
rem Diagnostic build: capturing full environment/sys.path info before
rem attempting the real launch, since the exact same absolute-path
rem pythonw.exe binary works when tested from the dev session but fails
rem with ModuleNotFoundError when double-clicked for real -- something
rem about the environment itself differs, not the interpreter or the path.
rem %~dp0 is the folder this .bat lives in. It used to be a hardcoded absolute
rem path to the main checkout, which meant a copy sitting in a git worktree
rem silently launched the OTHER checkout's code -- so testing a branch by
rem double-clicking its own launcher actually tested main. Relative keeps the
rem launcher honest about which code it is starting.
cd /d "%~dp0"

echo ==== launch at %DATE% %TIME% ==== > launch_log.txt
echo --- env vars --- >> launch_log.txt
echo APPDATA=%APPDATA% >> launch_log.txt
echo USERPROFILE=%USERPROFILE% >> launch_log.txt
echo PYTHONPATH=%PYTHONPATH% >> launch_log.txt
echo PYTHONNOUSERSITE=%PYTHONNOUSERSITE% >> launch_log.txt
echo PYTHONHOME=%PYTHONHOME% >> launch_log.txt
echo PATH=%PATH% >> launch_log.txt
echo --- python diagnostics --- >> launch_log.txt
rem find_spec answers the actual question -- "can THIS interpreter, in THIS
rem environment, locate customtkinter" -- without needing try/except, which
rem does not fit on a single cmd line. It returns None rather than raising
rem when the module is missing, so one line covers both outcomes.
rem
rem yt_dlp is probed the same way because it, not customtkinter, is what
rem actually failed on 2026-08-05 and again on 2026-08-08. Its origin is the
rem line that matters: a real install ends in \__init__.py, while a namespace
rem package -- the directory resolving without its __init__.py, which is the
rem fault both incidents showed -- reports None here and leaves yt_dlp with no
rem YoutubeDL attribute.
"C:\Python314\pythonw.exe" -c "import sys, site, os, importlib.util as u; f=open('diag_out.txt','w'); f.write('executable=' + sys.executable + chr(10)); f.write('prefix=' + sys.prefix + chr(10)); f.write('cwd=' + os.getcwd() + chr(10)); f.write('ENABLE_USER_SITE=' + str(site.ENABLE_USER_SITE) + chr(10)); f.write('USER_SITE=' + str(site.getusersitepackages()) + chr(10)); f.write('USER_SITE_EXISTS=' + str(os.path.isdir(site.getusersitepackages())) + chr(10)); s=u.find_spec('customtkinter'); f.write('customtkinter=' + (s.origin if s else 'NOT FOUND') + chr(10)); y=u.find_spec('yt_dlp'); f.write('yt_dlp=' + (str(y.origin) if y else 'NOT FOUND') + chr(10)); f.write('sys.path=' + chr(10).join(sys.path) + chr(10)); f.close()" >> launch_log.txt 2>&1
type diag_out.txt >> launch_log.txt 2>nul
del diag_out.txt 2>nul

rem A crash on startup used to be completely silent: pythonw has no console, so
rem a failed import meant no window, no message, nothing at all -- the only
rem evidence was in this log, and you had to already know to go read it. That
rem is the actual defect; the import failure itself was never reproducible.
rem
rem Labels rather than if(...) blocks on purpose: cmd expands %ERRORLEVEL%
rem inside a parenthesised block at PARSE time, so reading it there would
rem report the value from before the command ran. Delayed expansion would also
rem work; labels avoid needing it at all.

echo --- actual app launch --- >> launch_log.txt
"C:\Python314\pythonw.exe" gui.py >> launch_log.txt 2>&1
echo ==== exited with code %ERRORLEVEL% at %DATE% %TIME% ==== >> launch_log.txt
if %ERRORLEVEL% EQU 0 goto :done

rem Retries, because the 2026-07-19 failure was an import that worked before
rem and after and could not be reproduced from a shell. A transient -- another
rem Python process rewriting the bytecode cache, antivirus briefly locking a
rem .pyc -- should not cost a launch.
rem
rem On 2026-08-08 both attempts failed 0.3s apart with the same
rem namespace-package import of yt_dlp, so the single retry burned its one
rem chance inside the same instant that caused the fault. A 5s wait was added,
rem and 2026-08-09 then showed 5s too short as well: attempts at 20:02:28 and
rem 20:02:33 both failed, and the very same pythonw.exe imported yt_dlp
rem cleanly when re-probed at 20:15 with nothing reinstalled in between.
rem
rem That is the fourth occurrence (2026-07-19, 08-05, 08-08, 08-09) and the
rem second in a restart-shaped context -- it clusters around a previous
rem instance exiting, which fits __pycache__ rewriting or an AV scan holding
rem files after process exit.
rem
rem ---- why a LADDER rather than one longer wait ----
rem
rem Nobody has ever recorded how long this condition actually persists: the
rem launcher gave up after two attempts, so every occurrence only bounds it
rem from below. 2026-08-09 is the sole data point with an upper bound too,
rem and it is uselessly wide: somewhere between 5 seconds and 13 minutes.
rem
rem Three spaced retries fix that as a side effect of trying harder. Each
rem attempt stamps its own time and exit code into launch_log.txt, so
rem whichever one finally succeeds tells us the recovery window to within one
rem interval -- the log becomes the instrument, with no separate probe to
rem maintain and nothing extra to run on a healthy launch. Cumulative waits
rem are 20s / 60s / 120s (~3.5 min worst case), and every second of it is
rem paid only on a launch that had already failed twice.
rem
rem timeout is the readable choice but needs a real console; it aborts with
rem "input redirection is not supported" when stdin is redirected, which is how
rem this runs from a scheduler or a test harness. ping against loopback is the
rem portable fallback and waits n-1 seconds.

echo --- attempt 1 failed, retrying after 20s --- >> launch_log.txt
timeout /t 20 /nobreak >nul 2>&1 || ping -n 21 127.0.0.1 >nul 2>&1
"C:\Python314\pythonw.exe" gui.py >> launch_log.txt 2>&1
echo ==== attempt 2 exited with code %ERRORLEVEL% at %DATE% %TIME% ==== >> launch_log.txt
if %ERRORLEVEL% EQU 0 goto :done

echo --- attempt 2 failed, retrying after 40s --- >> launch_log.txt
timeout /t 40 /nobreak >nul 2>&1 || ping -n 41 127.0.0.1 >nul 2>&1
"C:\Python314\pythonw.exe" gui.py >> launch_log.txt 2>&1
echo ==== attempt 3 exited with code %ERRORLEVEL% at %DATE% %TIME% ==== >> launch_log.txt
if %ERRORLEVEL% EQU 0 goto :done

echo --- attempt 3 failed, retrying after 60s --- >> launch_log.txt
timeout /t 60 /nobreak >nul 2>&1 || ping -n 61 127.0.0.1 >nul 2>&1
"C:\Python314\pythonw.exe" gui.py >> launch_log.txt 2>&1
echo ==== attempt 4 exited with code %ERRORLEVEL% at %DATE% %TIME% ==== >> launch_log.txt
if %ERRORLEVEL% EQU 0 goto :done

rem Four times over ~3.5 minutes is not transient. Stop leaving the user to
rem guess and show the log.
echo. >> launch_log.txt
echo BackstageHero could not start after 4 attempts over ~3.5 minutes. >> launch_log.txt
echo Check the 'customtkinter=' and 'yt_dlp=' lines near the top of this log. >> launch_log.txt
echo NOT FOUND means the library is missing: pip install -r requirements.txt >> launch_log.txt
echo A yt_dlp of None means the package resolved without its __init__.py -- >> launch_log.txt
echo reinstall it with: pip install --force-reinstall yt-dlp >> launch_log.txt
start "" notepad.exe launch_log.txt

:done

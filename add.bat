@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"

where git >nul 2>&1 || (echo ERROR: Git is not installed.& goto fail)
where python >nul 2>&1 || (echo ERROR: Python is not installed.& goto fail)
python -c "import yt_dlp" >nul 2>&1 || (echo ERROR: Install yt-dlp with: python -m pip install -U "yt-dlp[default]"& goto fail)
git rev-parse --is-inside-work-tree >nul 2>&1 || (echo ERROR: Download using git clone, not ZIP.& goto fail)
if not exist "publish_safe.py" (echo ERROR: publish_safe.py is missing.& goto fail)

REM Avoid overwriting any existing local changes.
for /f "delims=" %%L in ('git status --porcelain --untracked-files^=all 2^>nul') do (
  echo ERROR: Working folder has uncommitted or untracked files.
  echo Run git status and resolve or back up files first.
  goto fail
)

echo Updating from GitHub...
git pull --ff-only origin main || goto fail
set "URL="
set /p "URL=YouTube URL (blank = process queue): "
REM Avoid unsafe expansion of user-supplied command text: use environment variable in Python.
python -c "import os,subprocess,sys; u=os.environ.get('URL','').split(); sys.exit(subprocess.call([sys.executable,'prep.py','--queue',*u]) if u else subprocess.call([sys.executable,'prep.py','--queue']))"
if errorlevel 3 goto fail
if errorlevel 1 if not errorlevel 2 goto fail
REM exit 2 means some URLs failed; publish the remaining queue and successful videos.

echo Uploading with automatic reconciliation...
python publish_safe.py || goto fail

echo SUCCESS. GitHub is updated.
pause
exit /b 0
:fail
echo.
echo STOPPED. No forced push or destructive cleanup was attempted.
pause
exit /b 1

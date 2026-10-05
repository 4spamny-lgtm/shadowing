@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
echo Getting latest from GitHub...
git pull --rebase --autostash -q
echo.
set "URL="
set /p "URL=YouTube URL (several: separate with spaces, Enter only = process queue): "
python prep.py --queue !URL!
echo.
echo Uploading to GitHub...
git add -A
git diff --cached --quiet && (echo Nothing new to upload.) || (git commit -q -m "Update videos" && git push)
echo.
pause

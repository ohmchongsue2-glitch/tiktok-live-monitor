@echo off
cd /d %~dp0
py -m pip install -r requirements.txt
py -m playwright install chromium
if not exist config.json copy config.example.json config.json
 echo.
echo ติดตั้งเสร็จแล้ว เปิด config.json ใส่ agent_key แล้วดับเบิลคลิก run.bat
pause

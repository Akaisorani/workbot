@echo off
setlocal
if "%WORKBOT_PYTHON%"=="" (
  echo WorkBot managed WeLink gateway: WORKBOT_PYTHON is not set 1>&2
  exit /b 2
)
if "%WORKBOT_ROOT%"=="" (
  echo WorkBot managed WeLink gateway: WORKBOT_ROOT is not set 1>&2
  exit /b 2
)
"%WORKBOT_PYTHON%" "%WORKBOT_ROOT%\scripts\workbot-welink-gateway.py" %*
exit /b %ERRORLEVEL%

@echo off
rem avr: the ai-workflow CLI by a shorter name. "avr start AVR-236" is "python aw.py start AVR-236".
if "%AI_WORKFLOW_PYTHON%"=="" (python "%~dp0..\aw.py" %*) else ("%AI_WORKFLOW_PYTHON%" "%~dp0..\aw.py" %*)

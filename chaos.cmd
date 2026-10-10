@echo off
rem Shortcut: "chaos plan", "chaos auto --top 3", "chaos stop", ... from the project folder.
"%~dp0.venv\Scripts\python.exe" -m chaos.run %*

@echo off
chcp 65001 >nul
title WeChat - Claude Code bridge (auto-start)
cd /d "%~dp0"
start /MIN python wechat_bridge.py
echo Bridge started in background.
timeout /t 3 >nul

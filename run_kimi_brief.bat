@echo off
set PYTHONIOENCODING=utf-8
cd /d "C:\Users\mrcar\OneDrive\Desktop\Kimi News Feed"
"C:\Users\mrcar\AppData\Local\Programs\Python\Python314\python.exe" ai_news_pipeline.py >> kimi_brief.log 2>&1

@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo MySQL 데이터를 parquet으로 내보냅니다.
echo.
python -m pip install --quiet pymysql sqlalchemy cryptography pandas pyarrow requests
python tools\export_from_mysql.py
echo.
pause

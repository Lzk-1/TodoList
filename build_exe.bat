@echo off
REM ============================================================
REM  TodoList Windows 打包脚本
REM  功能：用 PyInstaller 把 app.py + web/ 打包成单文件 exe
REM  产物：dist\TodoList.exe
REM  使用：双击本脚本 或 在 cmd 中执行 build_exe.bat
REM ============================================================

setlocal
cd /d "%~dp0"

echo [1/4] 检查 Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo 错误：未检测到 Python，请先安装 Python 3.8+ 并勾选 Add to PATH
    echo 下载地址：https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [2/4] 安装 PyInstaller...
pip install pyinstaller >nul 2>&1
if errorlevel 1 (
    echo 错误：PyInstaller 安装失败，请检查网络后重试
    pause
    exit /b 1
)

echo [3/4] 开始打包（可能需要 1-2 分钟）...
pyinstaller --onefile --name TodoList --add-data "web;web" --clean --noconfirm app.py
if errorlevel 1 (
    echo 错误：打包失败
    pause
    exit /b 1
)

echo [4/4] 打包完成！
echo 产物位置：dist\TodoList.exe
echo 使用方法：把 dist\TodoList.exe 拷到任意目录双击运行
echo 数据文件 data.db 会自动生成在 exe 同级目录
echo.
pause

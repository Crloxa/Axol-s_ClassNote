"""启动脚本：只监听 127.0.0.1。

用法：在 backend/ 目录下执行  ../.venv/Scripts/python run.py
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)

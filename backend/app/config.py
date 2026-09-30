"""应用配置：数据目录解析。

数据目录优先级：
1. 环境变量 AXOL_DATA_DIR（用户自选数据目录）
2. 默认 <项目根>/data（已被 .gitignore 排除，不出仓库）
"""
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent      # backend/
PROJECT_DIR = APP_DIR.parent                           # 仓库根目录

DATA_DIR = Path(os.environ.get("AXOL_DATA_DIR") or (PROJECT_DIR / "data")).resolve()

MAX_UPLOAD_BYTES = 60 * 1024 * 1024  # 单文件上限 60MB


def lesson_dir(lesson_id: str) -> Path:
    d = DATA_DIR / "lessons" / lesson_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

"""统一部署前缀；内部路由和生成产物的文件路径保持原有语义。"""

import os
import re

BASE_PATH = os.environ.get("STUDIO_BASE_PATH", "").rstrip("/")
if not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*", BASE_PATH):
    raise ValueError("STUDIO_BASE_PATH must be an absolute URL path, such as /studio")


def public_url(path: str) -> str:
    return BASE_PATH + path

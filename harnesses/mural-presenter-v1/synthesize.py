#!/usr/bin/env python3
"""Synthesis 薄入口；生成路径完全复用 ``infer.py`` 的共享 Kernel。"""
from __future__ import annotations

from infer import main
from runtime.config import RunMode


if __name__ == "__main__":
    raise SystemExit(main(forced_mode=RunMode.SYNTHESIS))

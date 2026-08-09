#!/usr/bin/env python3
"""Direct Web Demo Harness for the upstream sn-ppt-standard Skill."""
from __future__ import annotations

import sense_present_v2 as adapter


load_dotenv = adapter.load_dotenv

HARNESS_PROFILE = "sense-present-standard"


def build_config(args):
    config = adapter.build_config(args)
    config["direct_skill"] = "sn-ppt-standard"
    config["harness_profile"] = HARNESS_PROFILE
    return config


def _task(task):
    task = dict(task)
    task["seed"] = {**task["seed"], "ppt_output": "static_html"}
    task["config"] = {
        **task["config"],
        "direct_skill": "sn-ppt-standard",
        "harness_profile": HARNESS_PROFILE,
    }
    return task


def worker(task):
    return adapter.worker(_task(task))


def revision_worker(task):
    return adapter.revision_worker(_task(task))

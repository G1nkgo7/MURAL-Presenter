from runtime.language_policy import (
    extract_language_policy,
    language_drift_warning,
    language_policy_prompt,
    visible_output_language_reminder,
)


def test_language_policy_prompt_carries_runtime_languages():
    prompt = language_policy_prompt(
        {"working_language": "zh", "presentation_language": "zh"},
        audience="critic",
    )

    assert "working_language: zh" in prompt
    assert "presentation_language: zh" in prompt
    assert "Visual Critic output must use working_language" in prompt
    assert "delegate_wait/orchestration_update" in prompt


def test_extract_language_policy_section():
    policy = language_policy_prompt({"working_language": "zh", "presentation_language": "zh"})
    system = "Pinned skill text\n\n" + policy + "\n\n## Later section\nKeep going"

    assert extract_language_policy(system) == policy


def test_language_drift_warning_for_large_english_visible_text():
    text = (
        "P5 at full resolution resolves the subject clearly and gives enough context "
        "for the final page. The composition is readable, the caption hierarchy is "
        "stable, and the remaining issues are non-blocking for the current deck. "
        "The owner can proceed with final handoff after updating the catalog notes."
    )

    warning = language_drift_warning(
        text,
        {"working_language": "zh", "presentation_language": "zh"},
    )

    assert warning is not None
    assert warning["code"] == "language_drift"


def test_language_drift_warning_ignores_chinese_visible_text():
    text = "P5 当前全分辨率图像主体清楚，图注层级稳定；剩余问题不阻塞本页交接，可以继续更新素材登记并完成收尾。"

    assert language_drift_warning(
        text,
        {"working_language": "zh", "presentation_language": "zh"},
    ) is None


def test_visible_output_language_reminder_for_zh_status_updates():
    reminder = visible_output_language_reminder(
        {"working_language": "zh", "presentation_language": "zh"},
        tool_names=("delegate_wait",),
    )

    assert "必须使用中文" in reminder
    assert "Status update" in reminder
    assert "presentation_language=zh" in reminder


def test_visible_output_language_reminder_skips_low_risk_tools():
    assert visible_output_language_reminder(
        {"working_language": "zh", "presentation_language": "zh"},
        tool_names=("read_file",),
    ) == ""


def test_visible_output_language_reminder_for_english_working_language():
    reminder = visible_output_language_reminder(
        {"working_language": "en", "presentation_language": "zh"},
        tool_names=("delegate_wait",),
    )

    assert "must be in English" in reminder
    assert "presentation_language=zh" in reminder

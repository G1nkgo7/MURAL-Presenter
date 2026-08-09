from app import titles


LONG_QUERY = (
    "帮我制作一份 12 页左右的 PPT，围绕周星驰《功夫女足》展开分析。"
    "首先梳理影片创作背景、剧情与人物设定；横向对比《少林足球》。"
)


def test_fallback_title_extracts_named_work_and_intent():
    assert titles.fallback_title(LONG_QUERY) == "周星驰《功夫女足》PPT分析"


def test_fallback_title_handles_intro_prompt():
    assert titles.fallback_title("做个介绍李清照的ppt，5页 静态的") == "李清照介绍PPT"


def test_display_title_replaces_legacy_query_prefix():
    assert titles.display_title(LONG_QUERY[:80], LONG_QUERY) == "周星驰《功夫女足》PPT分析"


def test_display_title_preserves_curated_title():
    assert titles.display_title("体育喜剧 IP 的困境与未来", LONG_QUERY) == "体育喜剧 IP 的困境与未来"

from app.services.retrieve import rank_pages, score_text


def test_title_match_outranks_body_only():
    pages = [
        {
            "id": 1,
            "title": "账户余额规则",
            "page_type": "business",
            "path": "a.md",
            "content": "无关内容",
            "tags": [],
        },
        {
            "id": 2,
            "title": "其他",
            "page_type": "business",
            "path": "b.md",
            "content": "账户余额规则在清算时校验",
            "tags": [],
        },
    ]
    ranked = rank_pages("现货 余额 不足", pages, top_k=2)
    assert ranked[0]["id"] == 1
    assert ranked[0]["score"] > ranked[1]["score"]
    assert "snippet" in ranked[0]


def test_empty_query_returns_empty():
    assert (
        rank_pages(
            "",
            [
                {
                    "id": 1,
                    "title": "x",
                    "page_type": "business",
                    "path": "a.md",
                    "content": "y",
                    "tags": [],
                }
            ],
            top_k=5,
        )
        == []
    )


def test_score_text_weights():
    score = score_text(
        "balance 余额",
        title="余额规则",
        content="account balance check",
        tags=["balance"],
    )
    # ascii in tags/content + CJK bigram in title — must be positive & ordered
    assert score >= 7.0
    weak = score_text(
        "balance 余额",
        title="其他",
        content="无关",
        tags=[],
    )
    assert score > weak


def test_clean_query_strips_generate_instruction():
    from app.services.retrieve import clean_retrieve_query

    q = clean_retrieve_query(
        "开盘集合竞价的成交价格撮合规则。生成正常/边界/异常测试用例。"
    )
    assert "生成" not in q
    assert "测试用例" not in q
    assert "成交价格" in q


def test_body_phrase_outranks_unrelated_title():
    """Query phrases in body should score higher than unrelated title-only docs."""
    q = "指定交易 撤销指定"
    related = score_text(
        q,
        title="第二节 委托",
        content="投资者变更指定交易的，应当向已指定的会员提出撤销的意思表示。",
        tags=["原文"],
    )
    unrelated = score_text(
        q,
        title="临时停市公告",
        content="因系统故障实行临时停市。",
        tags=[],
    )
    assert related > unrelated
    assert related > 0

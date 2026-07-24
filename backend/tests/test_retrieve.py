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
    # 余额 in title (+10); balance in tags (+4) and content (+1)
    assert score >= 15.0

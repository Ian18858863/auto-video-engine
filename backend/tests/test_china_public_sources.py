from __future__ import annotations

import json

from app.hotspots.sources.china_public import (
    _parse_baidu_hot_search,
    _parse_bilibili_ranking,
    _parse_toutiao_hot_board,
)
from app.hotspots.sources.factory import build_hotspot_sources
from app.models import HotItem
from app.topic_ideas.event_scoring import event_score, event_title, event_title_for_hotspot


def test_default_public_bilibili_interface_is_disabled():
    source_names = [source.name for source in build_hotspot_sources("config/source_config.yaml")]

    assert "B站全站排行" not in source_names
    assert "B站科技区排行" not in source_names


def test_event_title_uses_human_summary_instead_of_truncation():
    assert event_title("曹操出行发布 Robotaxi 服务，上海自动驾驶加速") == "Robotaxi 加速"
    assert event_title("AI搜索正在改变普通人获取信息的方式") == "AI 搜索变天"
    assert event_title("The Download: a new hunt for dark matter") == "暗物质新发现"
    assert event_title("宝马停产三款国产纯电车型？为新世代车型上市让路") == "新能源洗牌"
    assert event_title("电商平台调整服务规则") == "电商调整规则"
    assert event_title("平台服务规则调整") == "平台调整规则"
    assert event_title("华为智驾功能调整") == "智驾调整功能"
    assert event_title("某电影 6 月 26 日上映") == "娱乐消息"


def test_event_title_uses_summary_when_original_title_is_too_generic():
    item = HotItem(
        source_platform="Google News",
        source_type="rss",
        title="公司发布公告",
        url="https://example.com/ai-buy",
        summary="AI 工具进入企业采购流程，平台采购规则正在变化。",
        category="AI",
        hot_score=80,
        topic_score=82,
        save_score=70,
        risk_score=0,
        freshness_score=80,
        data_origin="real",
    )

    assert event_title_for_hotspot(item) == "公司开始买AI"


def test_entertainment_without_business_context_is_demoted():
    movie = HotItem(
        source_platform="百度热搜",
        source_type="hotlist",
        title="某电影 6 月 26 日上映",
        url="https://example.com/movie",
        summary="电影档期消息。",
        category="娱乐",
        hot_score=95,
        topic_score=82,
        save_score=70,
        risk_score=0,
        freshness_score=90,
        data_origin="real",
    )
    ai_trend = HotItem(
        source_platform="36氪",
        source_type="news",
        title="AI 搜索正在改变普通人获取信息的方式",
        url="https://example.com/ai-search",
        summary="AI 搜索影响平台流量、广告模式和内容生态。",
        category="AI",
        hot_score=80,
        topic_score=82,
        save_score=70,
        risk_score=0,
        freshness_score=80,
        data_origin="real",
    )

    assert event_score(movie).total < event_score(ai_trend).total
    assert event_score(movie).fits_collection is False


def test_parse_baidu_hot_search_public_data_block():
    payload = {
        "data": {
            "cards": [
                {
                    "content": [
                        {
                            "word": "AI 搜索正在改变普通人获取信息的方式",
                            "rawUrl": "https://www.baidu.com/s?wd=AI",
                            "desc": "AI 搜索开始影响普通人找信息。",
                            "hotScore": "12345",
                        }
                    ]
                }
            ]
        }
    }
    html = f"<!--s-data:{json.dumps(payload, ensure_ascii=False)}-->"

    items = _parse_baidu_hot_search(html, limit=5)

    assert len(items) == 1
    assert items[0].source_platform == "百度热搜"
    assert items[0].source_type == "hotlist"
    assert items[0].metrics["hot_score_raw"] == 12345


def test_parse_toutiao_hot_board_json():
    payload = {
        "data": [
            {
                "Title": "车企新一轮价格战开始",
                "Url": "https://www.toutiao.com/trending/1/",
                "HotValue": "9876",
                "ClusterIdStr": "1",
                "LabelDesc": "新事件上榜",
            }
        ]
    }

    items = _parse_toutiao_hot_board(payload, limit=5)

    assert len(items) == 1
    assert items[0].source_platform == "今日头条热榜"
    assert items[0].source_type == "hotlist"
    assert items[0].metrics["hot_score_raw"] == 9876


def test_parse_bilibili_ranking_json():
    payload = {
        "code": 0,
        "data": {
            "list": [
                {
                    "title": "机器人降价带来的消费变化",
                    "bvid": "BV123",
                    "short_link_v2": "https://b23.tv/BV123",
                    "pubdate": 1710000000,
                    "desc": "机器人价格变化。",
                    "tname": "科技",
                    "stat": {
                        "view": 10000,
                        "like": 500,
                        "reply": 30,
                        "share": 20,
                        "favorite": 100,
                    },
                }
            ]
        },
    }

    items = _parse_bilibili_ranking(payload, source_name="B站科技区排行", limit=5)

    assert len(items) == 1
    assert items[0].source_platform == "B站科技区排行"
    assert items[0].source_type == "video_hotlist"
    assert items[0].metrics["views"] == 10000

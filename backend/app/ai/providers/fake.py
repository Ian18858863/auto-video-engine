from __future__ import annotations

from app.ai.schemas import AIRequest, AIResponse
from app.ai.schemas import GenerationType


class FakeProvider:
    name = "fake"

    def __init__(self, model: str = "fake-low-cost"):
        self.model = model or "fake-low-cost"
        self.call_count = 0

    def generate(self, request: AIRequest) -> AIResponse:
        self.call_count += 1
        title = request.input_content.get("title") or request.input_content.get("topic") or "未命名内容"
        if request.generation_type_value == GenerationType.TOPIC_IDEAS.value:
            return AIResponse(
                provider=self.name,
                model=self.model,
                output_content={
                    "text": f"本地测试内容：{request.generation_type_value} / {title}",
                    "topic_ideas": _fake_topic_ideas(title),
                    "generation_type": request.generation_type_value,
                    "is_fake": True,
                },
            )
        if request.generation_type_value == GenerationType.SCRIPT.value:
            return AIResponse(
                provider=self.name,
                model=self.model,
                output_content={
                    "text": f"本地测试脚本：{title}",
                    "script_asset": _fake_script_asset(title),
                    "generation_type": request.generation_type_value,
                    "is_fake": True,
                },
            )
        return AIResponse(
            provider=self.name,
            model=self.model,
            output_content={
                "text": f"本地测试内容：{request.generation_type_value} / {title}",
                "generation_type": request.generation_type_value,
                "is_fake": True,
            },
        )


def _fake_topic_ideas(title: str) -> list[dict[str, str]]:
    return [
        {
            "title": f"为什么这条热点不是新闻，而是一个新信号：{title}",
            "core_view": "这条内容适合拆背后的商业逻辑，而不是复述表面新闻。",
            "recommended_format": "数字人口播",
            "why_watch": "观众能快速知道这件事为什么值得关注。",
            "why_save": "它提供了以后判断类似趋势的框架。",
            "comment_trigger": "你觉得这是真变化，还是又一轮包装？",
            "risk_note": "只讲商业逻辑，不做投资建议，不下绝对结论。",
            "recommended_duration": "45-60 秒",
            "hook": "为什么这件事表面看是热点，背后其实是一个新信号？",
        },
        {
            "title": f"小白怎么理解这个变化：{title}",
            "core_view": "把复杂热点翻译成人话，讲清楚普通人该看哪一点。",
            "recommended_format": "动画讲解",
            "why_watch": "降低理解门槛，帮观众省时间。",
            "why_save": "观众可以复用这套理解方法看后续新闻。",
            "comment_trigger": "你平时会看这种变化，还是直接划走？",
            "risk_note": "避免夸大影响，明确不是行动建议。",
            "recommended_duration": "45-60 秒",
            "hook": "为什么这条新闻普通人不用全看，记住这一点就够了？",
        },
        {
            "title": f"判断这个风口值不值得跟，看这 3 点：{title}",
            "core_view": "把热点拆成可收藏的判断清单。",
            "recommended_format": "图文混剪",
            "why_watch": "观众能马上拿到清晰判断标准。",
            "why_save": "清单型内容方便以后反复使用。",
            "comment_trigger": "你会用这 3 点判断新风口吗？",
            "risk_note": "不鼓励跟风，不制造焦虑。",
            "recommended_duration": "50-70 秒",
            "hook": "为什么这个新风口先别急着跟，要先看这 3 点？",
        },
        {
            "title": f"别只看热闹，真正的问题是它靠什么成立：{title}",
            "core_view": "从商业模式和利益关系解释热点为什么发生。",
            "recommended_format": "录屏演示",
            "why_watch": "观众能看到更底层的原因。",
            "why_save": "这套拆解方式能用于类似产品和平台变化。",
            "comment_trigger": "你觉得它的核心价值在哪里？",
            "risk_note": "保持克制表达，不做收益承诺。",
            "recommended_duration": "45-60 秒",
            "hook": "为什么别只看热度，真正要看的是它到底靠什么成立？",
        },
    ]


def _fake_script_asset(title: str) -> dict:
    shots = [
        {
            "start": "00:00",
            "end": "00:03",
            "voiceover": "为什么这个变化值得普通人关注？关键不在热度，而在企业开始真花钱。",
            "recommended_visual": "动态标题页，突出为什么和真花钱两个关键词",
        },
        {
            "start": "00:03",
            "end": "00:10",
            "voiceover": f"表面上看，这是关于{title}的一条新闻。",
            "recommended_visual": "官方新闻截图配科技办公背景",
        },
        {
            "start": "00:10",
            "end": "00:25",
            "voiceover": "背后的商业逻辑是，工具只有进入真实预算，才算从尝鲜变成稳定需求。",
            "recommended_visual": "商业逻辑流程图，对比尝鲜和真实采购",
        },
        {
            "start": "00:25",
            "end": "00:40",
            "voiceover": "判断类似趋势，可以看三点：谁愿意付钱、能不能持续使用、是否真的省钱省人。",
            "recommended_visual": "三点判断清单信息卡片",
        },
        {
            "start": "00:40",
            "end": "00:50",
            "voiceover": "普通人不用急着跟风，但可以记住这套判断标准，之后看类似新闻还能用。",
            "recommended_visual": "总结卡片，数字人在右下角轻量收束",
        },
    ]
    return {
        "voiceover": "\n".join(shot["voiceover"] for shot in shots),
        "storyboard": shots,
        "subtitles": [
            {"start": shot["start"], "end": shot["end"], "text": shot["voiceover"]}
            for shot in shots
        ],
        "animation_prompts": [
            {
                "shot": index,
                "prompt": f"竖屏 9:16，干净克制的财经科技信息图：{shot['recommended_visual']}",
            }
            for index, shot in enumerate(shots, start=1)
        ],
        "title_options": [
            {"title": f"为什么{title}不只是一个热点", "type": "趋势解释型"},
            {"title": f"看懂{title}，先看这 3 点", "type": "收藏清单型"},
        ],
        "cover_copy": [
            {"text": "热度背后的真逻辑"},
            {"text": "判断趋势看这 3 点"},
        ],
        "publish_copy": {
            "抖音": "不复述热闹，只拆背后的商业逻辑。",
            "B站": "用大白话看懂这条变化为什么值得关注。",
        },
        "edit_checklist": [
            {"item": "核对新闻来源", "status": "待确认"},
            {"item": "检查字幕避开右下角数字人", "status": "待确认"},
        ],
    }

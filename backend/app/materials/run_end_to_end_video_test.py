from __future__ import annotations

import json

from app.materials.end_to_end_video import EndToEndVideoService


def main() -> None:
    result = EndToEndVideoService().run_test_export(
        topic="AI 搜索正在改变普通人获取信息的方式"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

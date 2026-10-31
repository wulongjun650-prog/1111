from __future__ import annotations

import json
import sys

from .app_categories import APP_CATEGORIES


def main() -> int:
    if "--self-test" in sys.argv:
        assert len(APP_CATEGORIES) == 140
        assert len({item[0] for item in APP_CATEGORIES}) == 140
        print(json.dumps({"ok": True, "app_categories": 140}))
        return 0
    if "--daily-appeal" in sys.argv:
        from .appeals import run_daily_appeal_workflow
        report = run_daily_appeal_workflow(force=False)
        return 0 if report.get("status") in {"完成", "部分完成", "未执行"} else 1
    if "--smoke-test" in sys.argv:
        from .gui import launch_smoke_test
        launch_smoke_test()
        return 0
    from .gui import launch
    launch()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

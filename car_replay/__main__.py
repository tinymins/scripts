"""`python -m car_replay` 入口；保留原顶层异常处理逻辑。"""

from __future__ import annotations

import traceback

from .cli import _pause_before_exit, main


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        code = exc.code
        is_error = isinstance(code, str) or code not in (None, 0)
        if is_error:
            if isinstance(code, str) and code:
                print(f"ERROR: {code}")
            _pause_before_exit()
        raise
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        _pause_before_exit()
        raise SystemExit(130)
    except Exception:
        print("\nFATAL ERROR: An unexpected error occurred.")
        traceback.print_exc()
        _pause_before_exit()
        raise SystemExit(1)

"""启动本机浏览器Demo；单worker复用GPU模型。"""

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / "data/models/.hub"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在1至65535之间")
    print(f"本机Demo：http://127.0.0.1:{args.port}；接口文档：/docs", flush=True)
    uvicorn.run("app.web.api:app", host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()

"""启动脚本"""

import uvicorn
import argparse
from app.config import get_settings

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start TripMind; development reload is opt-in.")
    parser.add_argument("--reload", action="store_true", help="仅开发时使用热重载")
    args = parser.parse_args()
    settings = get_settings()
    
    uvicorn.run(
        "app.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=args.reload,
        log_level=settings.log_level.lower()
    )


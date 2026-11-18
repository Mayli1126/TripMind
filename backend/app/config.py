"""配置管理模块"""

import os
from pathlib import Path
from typing import List
from pydantic_settings import BaseSettings
from pydantic import Field
from dotenv import load_dotenv

# 加载环境变量
# 首先尝试加载当前目录的.env
load_dotenv()

# 然后尝试加载HelloAgents的.env(如果存在)
helloagents_env = Path(__file__).parent.parent.parent.parent / "HelloAgents" / ".env"
if helloagents_env.exists():
    load_dotenv(helloagents_env, override=False)  # 不覆盖已有的环境变量


class Settings(BaseSettings):
    """应用配置"""

    # 应用基本配置
    app_name: str = "HelloAgents智能旅行助手"
    app_version: str = "1.0.0"
    debug: bool = False

    # 服务器配置
    host: str = "0.0.0.0"
    port: int = 8000
    app_timezone: str = "Asia/Shanghai"
    planning_job_timeout_seconds: int = 900
    auth_cookie_secure: bool = False     # HTTPS 正式部署时设 true；HTTP 本地/IP 调试使用 false

    # CORS配置 - 使用字符串,在代码中分割
    cors_origins: str = "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173,http://127.0.0.1:3000"

    # 高德地图API配置
    amap_api_key: str = ""
    # 无攻略选点使用百度智能搜索生成高性能版；其他 Agent 仍使用现有模型。
    baidu_search_enabled: bool = True
    baidu_search_api_key: str = ""
    baidu_search_timeout_seconds: float = Field(default=45, ge=5, le=90)
    baidu_search_daily_limit: int = Field(default=90, ge=0, le=90)
    # 高性能版当日上限用完后切普通版；普通版的模型推理费用需另行核实。
    baidu_search_standard_enabled: bool = True
    baidu_search_standard_daily_limit: int = Field(default=90, ge=0, le=90)
    baidu_search_standard_model: str = "deepseek-v4-flash"
    baidu_search_cache_hours: int = Field(default=24, ge=1, le=168)
    road_routes_enabled: bool = True
    road_routes_max_seconds: float = 12
    road_routes_max_calls: int = 24
    mcp_warmup_on_startup: bool = True
    mcp_pool_size: int = 4
    mcp_startup_timeout: int = 120
    mcp_package_index: str = ""          # 仅显式 prepare 时下载；留空遵循 uv 配置

    # Unsplash API配置
    unsplash_access_key: str = ""
    unsplash_secret_key: str = ""

    # LLM配置 (从环境变量读取,由HelloAgents管理)
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = ""
    llm_max_tokens: int = 8192
    planning_tips_mode: str = 'rules'  # rules省去收尾模型调用；llm恢复模型润色
    skeleton_direct_enabled: bool = True
    advisory_cache_hours: int = 6
    advisory_provider_url: str = ''  # 部署者配置的官方政策JSON适配服务，非任意网页抓取
    advisory_official_hosts: str = ''  # 允许声明官方政策的精确来源域名，逗号分隔

    # 按模块模型覆盖（阶段5预留：不同任务用不同模型，均走同一 OpenAI 兼容接口）
    # 留空 = 全部回退 LLM_MODEL_ID。见 .env 中「按模块模型配置」备注块。
    vision_model: str = ""               # 截图攻略解析（需多模态/视觉模型，如 qwen-vl 系列）

    # Redis 配置（阶段 D：任务队列/状态/缓存/配额）
    redis_enabled: bool = False          # 总开关：false 时沿用内存任务队列
    redis_url: str = "redis://localhost:6379/0"
    # 独立的公共事实缓存，供地点检索和道路路线共用。
    cache_enabled: bool = False
    cache_redis_url: str = ''
    cache_max_connections: int = Field(default=16, ge=1, le=512)
    cache_connect_timeout_seconds: float = Field(default=.3, gt=0, le=2)
    cache_read_timeout_seconds: float = Field(default=.3, gt=0, le=2)
    cache_failure_cooldown_seconds: float = Field(default=5, ge=0, le=60)
    cache_l1_ttl_seconds: float = Field(default=30, ge=0, le=300)
    cache_l1_max_entries: int = Field(default=512, ge=0, le=100000)
    cache_l1_max_bytes: int = Field(default=8 * 1024 * 1024, ge=0)
    cache_max_value_bytes: int = Field(default=256 * 1024, gt=0, le=1024 * 1024)
    cache_rebuild_lease_seconds: int = Field(default=45, ge=30, le=120)
    cache_rebuild_max_seconds: int = Field(default=90, ge=1, le=300)
    cache_rebuild_wait_seconds: float = Field(default=3, ge=0, le=10)
    cache_rebuild_max_local: int = Field(default=128, ge=1, le=2048)
    provider_map_concurrency: int = Field(default=3, ge=1, le=128)
    provider_llm_concurrency: int = Field(default=4, ge=1, le=128)
    provider_map_interval_seconds: float = Field(default=.35, ge=0, le=60)
    provider_llm_interval_seconds: float = Field(default=.1, ge=0, le=60)
    provider_wait_seconds: float = Field(default=3, ge=0, le=30)
    job_user_active_limit: int = Field(default=2, ge=1, le=100)
    job_global_active_limit: int = Field(default=32, ge=1, le=10000)
    job_queue_capacity: int = Field(default=32, ge=1, le=10000)
    job_admission_max_age_seconds: int = Field(default=3600, ge=1800, le=86400)
    job_max_queue_wait_seconds: int = Field(default=120, ge=10, le=3600)
    plan_quota_per_day: int = 20         # 每客户端每日提交上限，0 表示不限
    guide_quota_per_day: int = 20        # 每客户端每日攻略解析上限（每次=1次LLM+多次高德查询）
    chat_quota_per_day: int = 100        # 每客户端每日需求对话轮数，与攻略解析配额分开

    # 日志配置
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # 忽略额外的环境变量

    def get_cors_origins_list(self) -> List[str]:
        """获取CORS origins列表"""
        return [origin.strip() for origin in self.cors_origins.split(',')] #把用逗号分隔的 CORS 地址字符串，转换为 Python 列表，并删除每个地址前后的空格



# 创建全局配置实例
settings = Settings()


def get_settings() -> Settings:
    """获取配置实例"""
    return settings


# 验证必要的配置
def validate_config():
    """验证配置是否完整"""
    errors = []
    warnings = []

    if not settings.amap_api_key:
        errors.append("AMAP_API_KEY未配置")

    # HelloAgentsLLM会自动从LLM_API_KEY读取,不强制要求OPENAI_API_KEY
    llm_api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not llm_api_key:
        warnings.append("LLM_API_KEY或OPENAI_API_KEY未配置,LLM功能可能无法使用")

    if errors:
        error_msg = "配置错误:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ValueError(error_msg)

    if warnings:
        print("\n⚠️  配置警告:")
        for w in warnings:
            print(f"  - {w}")

    return True


# 打印配置信息(用于调试)
def print_config():
    """打印当前配置(隐藏敏感信息)"""
    print(f"应用名称: {settings.app_name}")
    print(f"版本: {settings.app_version}")
    print(f"服务器: {settings.host}:{settings.port}")
    print(f"高德地图API Key: {'已配置' if settings.amap_api_key else '未配置'}")

    # 检查LLM配置
    llm_api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    llm_base_url = os.getenv("LLM_BASE_URL") or settings.openai_base_url
    llm_model = os.getenv("LLM_MODEL_ID") or settings.openai_model

    print(f"LLM API Key: {'已配置' if llm_api_key else '未配置'}")
    print(f"LLM Base URL: {llm_base_url}")
    print(f"LLM Model: {llm_model}")
    print(f"LLM最大输出: {settings.llm_max_tokens} tokens")
    print(f"日志级别: {settings.log_level}")

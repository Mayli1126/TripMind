"""SQLite 公共连接层：统一连接参数、事务提交和句柄释放。"""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


DEFAULT_DATABASE_PATH = Path(__file__).resolve().parents[2] / "data" / "tripmind.db"


def connect(database_path: str | Path = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    """每次操作使用独立连接；业务层自行决定事务边界。"""
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """成功提交、异常回滚，无论结果如何都关闭连接。"""
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def connection(database_path: str | Path = DEFAULT_DATABASE_PATH):
    """提供短事务上下文，不在外部接口调用期间持有写锁。"""
    return transaction(connect(database_path))


class Database:
    """数据库入口可注入路径；测试和独立工具不必改业务服务内部属性。"""

    def __init__(self, path: str | Path = DEFAULT_DATABASE_PATH):
        self.path = Path(path)

    def connection(self):
        return connection(self.path)


# 账号、会话、摘要和提交记录共用同一数据库配置，不相互借用业务连接。
application_database = Database()

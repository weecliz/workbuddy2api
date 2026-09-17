"""pytest 全局夹具：把测试强制隔离到一次性 SQLite，绝不触碰真实数据库。

**为什么必须这么做（真实事故）**：
`admin/db_config.py` 在模块导入时就解析 `.env` 并实例化全局单例 `db_config`。
因此 `from admin.server import app` + `TestClient(app)` 会直接连上 **`.env` 里配置的
真实库**（本项目是 MySQL `workbuddy_admin`），任何写接口都会真落库、真消耗额度。
曾因此把测试账号写进生产库、并产生真实用量记录。

**隔离策略（三层，缺一不可）**：

  1. **最早时机**：在本文件顶部、导入任何 `admin.*` 之前设置 `ADMIN_DATABASE_URL`。
     `load_dotenv()` 不覆盖已存在的环境变量，所以在这里设的值必定胜出。
  2. **真文件库**：用 `.tmp/test.db` 而不是 `:memory:`。内存库在多连接
     （FastAPI 线程 + 后台调度线程）下会各自拿到独立的空库，导致"表不存在"的假失败。
  3. **失败即停**：会话级夹具断言实际生效的库确实是那个临时文件；一旦不是就
     立即报错终止，而不是让测试继续跑在真实库上。

用完删除临时库文件；`.tmp/` 已被 `.gitignore` 忽略。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# 关键：必须在任何 admin.* 导入之前设置（此时 .env 尚未被读取）
# ---------------------------------------------------------------------------
_TMP_DIR = ROOT / ".tmp"
_TMP_DIR.mkdir(exist_ok=True)
TEST_DB = _TMP_DIR / "test.db"

os.environ["ADMIN_DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
# 必须清掉可能干扰方言判定的变量：resolve() 的逻辑是「同时给出 URL 与
# ADMIN_DB_TYPE 时，方言以 DB_TYPE 为准」，这会拿 mysql 方言去解析 sqlite URL
# （url 看着对，dialect 却是 mysql）→ create_engine 加载错驱动。
# 当前 .env 恰好没设 ADMIN_DB_TYPE，但不能依赖这个巧合。
for _k in ("ADMIN_DB_TYPE", "ADMIN_DB_OPTIONS", "ADMIN_DB_SCHEMA"):
    os.environ.pop(_k, None)
# 测试不需要连 Redis，避免限流模块去连真实实例
os.environ.pop("ADMIN_REDIS_URL", None)

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _assert_isolated_database():
    """守门夹具：确认测试跑在临时 sqlite 上，否则立即中止。

    同时校验 dialect 而不只看 URL：resolve() 在 DB_TYPE 与 URL 同时存在时
    以 DB_TYPE 定方言，只看 url 会漏掉「url 是 sqlite、dialect 是 mysql」这种半错状态。
    """
    from admin.config import settings

    url = settings.DATABASE_URL or ""
    db_type = (settings.DB_TYPE or "").lower()
    if "test.db" not in url or db_type != "sqlite":
        raise RuntimeError(
            "测试未被隔离，拒绝继续运行以免污染真实数据库！\n"
            f"  期望: sqlite @ .../.tmp/test.db\n"
            f"  实际: {db_type} @ {url}\n"
            "请检查 tests/conftest.py 顶部的环境变量是否被覆盖。"
        )
    yield
    # 会话结束：清掉临时库，不留残留
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = Path(str(TEST_DB) + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

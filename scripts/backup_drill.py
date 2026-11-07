"""备份恢复演练（实现文档 §12.3）：pg_dump 往返 + 向量列校验。

流程：
  1. pg_dump 导出源库 nlp_rag；
  2. 创建演练库 nlp_rag_drill 并导入；
  3. 核对核心表行数一致；
  4. 逐块核对 vector 列的文本表示（源库 vs 演练库）完全一致；
  5. 删除演练库。

用法（数据库运行在 WSL docker 容器 pgvector 时）：
  uv run python -m scripts.backup_drill
环境变量：DRILL_CONTAINER（默认 pgvector）、DRILL_DB_USER（默认 postgres）、
DRILL_SOURCE_DB（默认 nlp_rag）、DRILL_TARGET_DB（默认 nlp_rag_drill）、
DRILL_KEEP（设为 1 保留演练库）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal, engine  # noqa: E402

CONTAINER = os.getenv("DRILL_CONTAINER", "pgvector")
DB_USER = os.getenv("DRILL_DB_USER", "postgres")
SOURCE_DB = os.getenv("DRILL_SOURCE_DB", "nlp_rag")
TARGET_DB = os.getenv("DRILL_TARGET_DB", "nlp_rag_drill")
KEEP = os.getenv("DRILL_KEEP") == "1"

VERIFY_TABLES = (
    "kb_collection",
    "kb_document",
    "kb_document_version",
    "kb_index",
    "kb_chunk",
    "kb_embedding",
    "ingest_job",
)


def _run(args: list[str], *, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["wsl", *args],
        input=input_bytes,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[:500])
    return result.stdout


def _psql_exec(sql: str, *, database: str | None = None) -> bytes:
    # -t：tuples-only，去掉 (1 row) 页脚便于解析
    args = ["docker", "exec", CONTAINER, "psql", "-U", DB_USER, "-t", "-v", "ON_ERROR_STOP=1", "-q"]
    if database:
        args += ["-d", database]
    args += ["-c", sql]
    return _run(args)


def _wsl_path(win_path: Path) -> str:
    """Windows 路径转 WSL 路径（D:\\a\\b -> /mnt/d/a/b）。"""
    resolved = str(win_path.resolve())
    drive, rest = resolved[:2], resolved[2:]
    return f"/mnt/{drive[0].lower()}{rest.replace(chr(92), '/')}"


def _dump_via_file() -> Path:
    """dump 经临时文件中转：wsl.exe 管道会损坏二进制 stdin/stdout，不可直接传。"""
    dump_path = Path("data/_drill_dump.sql").resolve()
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    wsl_dump = _wsl_path(dump_path)
    _run(
        [
            "bash", "-c",
            f"docker exec {CONTAINER} pg_dump -U {DB_USER} {SOURCE_DB} > '{wsl_dump}'",
        ]
    )
    return dump_path


def _restore_from_file(dump_path: Path) -> None:
    wsl_dump = _wsl_path(dump_path)
    _run(
        [
            "bash", "-c",
            f"docker exec -i {CONTAINER} psql -U {DB_USER} "
            f"-v ON_ERROR_STOP=1 -q -d {TARGET_DB} < '{wsl_dump}'",
        ]
    )


def row_counts(database: str) -> dict[str, int]:
    """从演练库读行数：通过 psql 输出解析（演练库不走应用 DSN，避免改连接池）。"""
    counts: dict[str, int] = {}
    for table in VERIFY_TABLES:
        out = _psql_exec(f"SELECT count(*) FROM {table};", database=database)
        counts[table] = int(out.decode().strip().splitlines()[-1])
    return counts


def source_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    with SessionLocal() as session:
        for table in VERIFY_TABLES:
            counts[table] = session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
    return counts


def main() -> int:
    print(f"备份恢复演练：{SOURCE_DB} -> {TARGET_DB}（容器 {CONTAINER}）")

    print("[1/5] pg_dump 导出…")
    dump_path = _dump_via_file()
    print(f"      导出文件 {dump_path.name}（{dump_path.stat().st_size} 字节）")

    print("[2/5] 重建演练库并导入…")
    _psql_exec(f"DROP DATABASE IF EXISTS {TARGET_DB};")
    _psql_exec(f"CREATE DATABASE {TARGET_DB} ENCODING 'UTF8' TEMPLATE template0;")
    _restore_from_file(dump_path)

    print("[3/5] 核对行数…")
    src, dst = source_counts(), row_counts(TARGET_DB)
    for table in VERIFY_TABLES:
        status = "OK" if src[table] == dst[table] else "MISMATCH"
        print(f"      {table}: 源 {src[table]} / 演练 {dst[table]} [{status}]")
        if src[table] != dst[table]:
            print("行数不一致，演练失败")
            return 1

    print("[4/5] 核对 vector 列往返…")
    active_snapshot_sql = (
        "SELECT active_index_id FROM kb_collection "
        "WHERE active_index_id IS NOT NULL ORDER BY id LIMIT 1"
    )
    with engine.connect() as conn:
        source_vectors = dict(
            conn.execute(
                text(
                    "SELECT e.chunk_id, e.embedding::text FROM kb_embedding e "
                    "JOIN kb_chunk c ON c.id = e.chunk_id "
                    f"WHERE c.index_id = ({active_snapshot_sql}) "
                    "ORDER BY e.chunk_id LIMIT 50"
                )
            ).all()
        )
    if not source_vectors:
        print("      源库无活跃快照向量，跳过逐块核对（先上传并构建索引）")
    else:
        chunk_ids = ",".join(str(cid) for cid in source_vectors)
        out = _psql_exec(
            "SELECT chunk_id, embedding::text FROM kb_embedding "
            f"WHERE chunk_id IN ({chunk_ids}) ORDER BY chunk_id;",
            database=TARGET_DB,
        )
        restored: dict[int, str] = {}
        for line in out.decode().strip().splitlines():
            chunk_id, _, vec = line.partition("|")
            restored[int(chunk_id)] = vec.strip()
        mismatch = [cid for cid, vec in source_vectors.items() if restored.get(cid) != vec]
        print(f"      核对 {len(source_vectors)} 条向量，不一致 {len(mismatch)} 条")
        if mismatch:
            print("向量往返不一致，演练失败")
            return 1

    print("[5/5] 清理演练库…")
    if KEEP:
        print(f"      DRILL_KEEP=1，保留 {TARGET_DB}")
    else:
        _psql_exec(f"DROP DATABASE IF EXISTS {TARGET_DB};")
    dump_path.unlink(missing_ok=True)

    print("演练通过：RTO 观测以实际耗时为准；建议 RPO 24h / RTO 4h（实现文档 §12.3）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

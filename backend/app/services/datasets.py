from __future__ import annotations

import io
import json
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Full, LifoQueue
from typing import Any

import pandas as pd
from fastapi import UploadFile
import pymysql
from pymysql.cursors import DictCursor

from ..config import get_settings
from ..db import connect, using_mysql
from .security import safe_identifier
from .vector_store import VectorStoreError, delete_knowledge_vectors


NUMERIC_TYPE_TOKENS = ("int", "float", "double", "decimal", "real", "numeric", "number")
UPLOAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
MYSQL_SYSTEM_DATABASES = {"information_schema", "mysql", "performance_schema", "sys"}
MYSQL_POOL_SIZE = 5
_MYSQL_POOLS: dict[tuple[Any, ...], LifoQueue] = {}
_MYSQL_POOL_LOCK = threading.Lock()


def _quote_identifier(name: str) -> str:
    if using_mysql():
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def _sql_type(dtype: Any) -> str:
    text = str(dtype).lower()
    if "int" in text:
        return "BIGINT" if using_mysql() else "INTEGER"
    if any(token in text for token in ("float", "double", "decimal")):
        return "DOUBLE" if using_mysql() else "REAL"
    if "bool" in text:
        return "TINYINT" if using_mysql() else "INTEGER"
    if "datetime" in text or "date" in text:
        return "DATETIME" if using_mysql() else "TEXT"
    return "TEXT"


def _clean_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value.item() if hasattr(value, "item") else value


def _read_file_frame(filename: str | None, content: bytes) -> pd.DataFrame:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in {".csv", ".xlsx", ".xls"}:
        raise ValueError("仅支持 CSV、XLS、XLSX 文件")
    if suffix == ".csv":
        try:
            return pd.read_csv(io.BytesIO(content), encoding="utf-8-sig")
        except UnicodeDecodeError:
            return pd.read_csv(io.BytesIO(content), encoding="gb18030")
    return pd.read_excel(io.BytesIO(content))


def _normalize_frame(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    if frame.empty or not len(frame.columns):
        raise ValueError("文件中没有可用数据")
    if len(frame.columns) > 200:
        raise ValueError("单个数据集最多支持 200 个字段")
    original_columns = [str(column) for column in frame.columns]
    sanitized: list[str] = []
    used: set[str] = set()
    for index, column in enumerate(original_columns):
        candidate = safe_identifier(column, f"column_{index + 1}")
        base = candidate
        counter = 2
        while candidate in used:
            candidate = f"{base}_{counter}"
            counter += 1
        used.add(candidate)
        sanitized.append(candidate)
    frame = frame.copy()
    frame.columns = sanitized
    return frame, original_columns


def _store_frame(
    frame: pd.DataFrame,
    original_columns: list[str],
    *,
    source_type: str,
    name: str,
    description: str,
) -> dict:
    table_name = f"data_{uuid.uuid4().hex[:12]}"
    with connect() as conn:
        column_defs = ", ".join(f"{_quote_identifier(column)} {_sql_type(frame[column].dtype)}" for column in frame.columns)
        engine = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci" if using_mysql() else ""
        conn.execute(f"CREATE TABLE {_quote_identifier(table_name)} ({column_defs}){engine}")
        if len(frame):
            columns_sql = ", ".join(_quote_identifier(column) for column in frame.columns)
            placeholders = ", ".join("?" for _ in frame.columns)
            values = [tuple(_clean_value(value) for value in row) for row in frame.itertuples(index=False, name=None)]
            conn.executemany(f"INSERT INTO {_quote_identifier(table_name)} ({columns_sql}) VALUES ({placeholders})", values)
        cursor = conn.execute(
            """INSERT INTO datasets(name, description, source_type, table_name, row_count, column_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, description, source_type, table_name, len(frame), len(frame.columns)),
        )
        dataset_id = int(cursor.lastrowid)
        for original, column in zip(original_columns, frame.columns):
            series = frame[column]
            sample = next((str(value) for value in series.head(20) if pd.notna(value)), None)
            null_rate = round(float(series.isna().mean()), 4)
            conn.execute(
                """INSERT INTO dataset_columns(dataset_id, name, data_type, description, sample_value, null_rate)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (dataset_id, str(column), str(series.dtype), original, sample, null_rate),
            )
    return get_dataset(dataset_id)


async def import_dataset(file: UploadFile, name: str | None = None, description: str = "") -> dict:
    content = await file.read()
    return import_dataset_content(file.filename, content, name, description)


def import_dataset_content(filename: str | None, content: bytes, name: str | None = None, description: str = "") -> dict:
    settings = get_settings()
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise ValueError(f"文件不能超过 {settings.max_upload_mb}MB")
    frame = _read_file_frame(filename, content)
    frame, original_columns = _normalize_frame(frame)
    display_name = name or Path(filename or "数据集").stem
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    return _store_frame(frame, original_columns, source_type=suffix or "file", name=display_name, description=description)


def _chunk_root() -> Path:
    root = get_settings().database_file.parent / "upload_chunks"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _upload_dir(upload_id: str) -> Path:
    if not UPLOAD_ID_PATTERN.fullmatch(upload_id):
        raise ValueError("上传任务 ID 不合法")
    return _chunk_root() / upload_id


def _chunk_path(upload_id: str, chunk_index: int) -> Path:
    return _upload_dir(upload_id) / f"{chunk_index:06d}.part"


def _received_chunks(upload_id: str) -> list[int]:
    upload_dir = _upload_dir(upload_id)
    if not upload_dir.exists():
        return []
    received: list[int] = []
    for path in upload_dir.glob("*.part"):
        try:
            received.append(int(path.stem))
        except ValueError:
            continue
    return sorted(received)


def chunk_upload_status(upload_id: str) -> dict[str, Any]:
    upload_dir = _upload_dir(upload_id)
    received = _received_chunks(upload_id)
    status: dict[str, Any] = {
        "upload_id": upload_id,
        "received_chunks": received,
        "received_count": len(received),
        "total_chunks": None,
        "missing_chunks": [],
        "complete": False,
        "progress_percent": 0,
    }
    metadata_path = upload_dir / "metadata.json"
    if not metadata_path.exists():
        return status
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return status
    total_chunks = max(0, int(metadata.get("total_chunks") or 0))
    missing = [index for index in range(total_chunks) if index not in set(received)]
    status.update(
        {
            "filename": metadata.get("filename"),
            "total_chunks": total_chunks,
            "total_size": int(metadata.get("total_size") or 0),
            "missing_chunks": missing,
            "complete": bool(total_chunks and not missing),
            "progress_percent": round((len(received) / total_chunks) * 100, 2) if total_chunks else 0,
        }
    )
    return status


def save_upload_chunk(
    *,
    upload_id: str,
    chunk_index: int,
    total_chunks: int,
    total_size: int,
    filename: str,
    content: bytes,
) -> dict[str, Any]:
    settings = get_settings()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if total_size > max_bytes:
        raise ValueError(f"文件不能超过 {settings.max_upload_mb}MB")
    if total_chunks < 1 or chunk_index < 0 or chunk_index >= total_chunks:
        raise ValueError("分片序号不合法")
    if Path(filename or "").suffix.lower() not in {".csv", ".xlsx", ".xls"}:
        raise ValueError("仅支持 CSV、XLS、XLSX 文件")

    upload_dir = _upload_dir(upload_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "filename": filename,
        "total_chunks": total_chunks,
        "total_size": total_size,
    }
    (upload_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    _chunk_path(upload_id, chunk_index).write_bytes(content)
    received = _received_chunks(upload_id)
    return {
        "upload_id": upload_id,
        "received_chunks": received,
        "received_count": len(received),
        "total_chunks": total_chunks,
        "complete": len(received) == total_chunks,
    }


def complete_chunk_upload(
    *,
    upload_id: str,
    filename: str,
    total_chunks: int,
    total_size: int,
    name: str | None = None,
    description: str = "",
) -> dict:
    upload_dir = _upload_dir(upload_id)
    if not upload_dir.exists():
        raise ValueError("上传任务不存在，请重新上传")
    metadata_path = upload_dir / "metadata.json"
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        filename = filename or metadata.get("filename") or "数据集.csv"
        total_chunks = int(metadata.get("total_chunks") or total_chunks)
        total_size = int(metadata.get("total_size") or total_size)
    missing = [index for index in range(total_chunks) if not _chunk_path(upload_id, index).exists()]
    if missing:
        raise ValueError(f"还有 {len(missing)} 个分片未上传完成")

    content = bytearray()
    for index in range(total_chunks):
        content.extend(_chunk_path(upload_id, index).read_bytes())
    if len(content) != total_size:
        raise ValueError("合并后的文件大小与原文件不一致，请重新上传")

    try:
        return import_dataset_content(filename, bytes(content), name, description)
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)


def _quote_mysql_identifier(name: str) -> str:
    if not name or "\x00" in name:
        raise ValueError("MySQL 标识符不合法")
    return "`" + name.replace("`", "``") + "`"


def _payload_value(payload: Any, key: str, default: Any = None) -> Any:
    return getattr(payload, key, default)


def _mysql_ssl_config(payload: Any) -> dict[str, str] | None:
    if not _payload_value(payload, "ssl_enabled", False):
        return None
    ssl_config: dict[str, str] = {}
    if _payload_value(payload, "ssl_ca"):
        ssl_config["ca"] = _payload_value(payload, "ssl_ca")
    if _payload_value(payload, "ssl_cert"):
        ssl_config["cert"] = _payload_value(payload, "ssl_cert")
    if _payload_value(payload, "ssl_key"):
        ssl_config["key"] = _payload_value(payload, "ssl_key")
    return ssl_config or {}


def _mysql_pool_key(payload: Any, database: str | None) -> tuple[Any, ...]:
    return (
        _payload_value(payload, "host"),
        int(_payload_value(payload, "port", 3306)),
        _payload_value(payload, "username"),
        _payload_value(payload, "password", ""),
        database or "",
        bool(_payload_value(payload, "ssl_enabled", False)),
        _payload_value(payload, "ssl_ca"),
        _payload_value(payload, "ssl_cert"),
        _payload_value(payload, "ssl_key"),
        bool(_payload_value(payload, "ssh_enabled", False)),
        _payload_value(payload, "ssh_host"),
        int(_payload_value(payload, "ssh_port", 22)),
        _payload_value(payload, "ssh_username"),
        _payload_value(payload, "ssh_password"),
        _payload_value(payload, "ssh_pkey"),
        _payload_value(payload, "ssh_private_key_passphrase"),
    )


def _mysql_pool(key: tuple[Any, ...]) -> LifoQueue:
    with _MYSQL_POOL_LOCK:
        pool = _MYSQL_POOLS.get(key)
        if pool is None:
            pool = LifoQueue(maxsize=MYSQL_POOL_SIZE)
            _MYSQL_POOLS[key] = pool
        return pool


def _ssh_tunnel(payload: Any):
    """Establish SSH tunnel for connecting to MySQL behind a bastion host."""
    ssh_host = _payload_value(payload, "ssh_host")
    ssh_username = _payload_value(payload, "ssh_username")
    if not ssh_host or not ssh_username:
        raise ValueError("启用 SSH 隧道时必须填写 SSH 主机和 SSH 用户名")
    if not _payload_value(payload, "ssh_password") and not _payload_value(payload, "ssh_pkey"):
        raise ValueError("启用 SSH 隧道时必须填写 SSH 密码或私钥路径")
    try:
        from sshtunnel import SSHTunnelForwarder
    except ImportError as exc:
        raise ValueError("当前环境缺少 sshtunnel 依赖，请先执行 pip install -r backend/requirements.txt") from exc

    tunnel = SSHTunnelForwarder(
        ssh_address_or_host=(ssh_host, int(_payload_value(payload, "ssh_port", 22))),
        ssh_username=ssh_username,
        ssh_password=_payload_value(payload, "ssh_password") or None,
        ssh_pkey=_payload_value(payload, "ssh_pkey") or None,
        ssh_private_key_password=_payload_value(payload, "ssh_private_key_passphrase") or None,
        remote_bind_address=(_payload_value(payload, "host"), int(_payload_value(payload, "port", 3306))),
        local_bind_address=("127.0.0.1", 0),
        set_keepalive=30,
    )
    try:
        tunnel.start()
    except Exception as exc:
        tunnel.stop()
        raise ValueError(f"SSH 隧道建立失败：{exc}") from exc
    return tunnel


def _new_mysql_connection(payload: Any, database: str | None = None):
    tunnel = _ssh_tunnel(payload) if _payload_value(payload, "ssh_enabled", False) else None
    connect_host = "127.0.0.1" if tunnel is not None else _payload_value(payload, "host")
    connect_port = int(tunnel.local_bind_port) if tunnel is not None else int(_payload_value(payload, "port", 3306))
    kwargs = {
        "host": connect_host,
        "port": connect_port,
        "user": _payload_value(payload, "username"),
        "password": _payload_value(payload, "password", ""),
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "autocommit": True,
        "connect_timeout": int(_payload_value(payload, "connect_timeout", 5)),
        "read_timeout": int(_payload_value(payload, "read_timeout", 20)),
        "write_timeout": int(_payload_value(payload, "read_timeout", 20)),
    }
    if database:
        kwargs["database"] = database
    ssl_config = _mysql_ssl_config(payload)
    if ssl_config is not None:
        kwargs["ssl"] = ssl_config
    try:
        conn = pymysql.connect(**kwargs)
    except Exception:
        if tunnel is not None:
            tunnel.stop()
        raise
    return _MySQLLease(conn, tunnel)


class _MySQLLease:
    """Holds a MySQL connection and optional SSH tunnel together."""
    def __init__(self, conn, tunnel=None):
        self.conn = conn
        self.tunnel = tunnel

    def ping(self):
        self.conn.ping(reconnect=True)

    def close(self):
        try:
            self.conn.close()
        finally:
            if self.tunnel is not None:
                self.tunnel.stop()


@contextmanager
def external_mysql_connection(payload: Any, database: str | None = None):
    key = _mysql_pool_key(payload, database)
    pool = _mysql_pool(key)
    lease = None
    try:
        lease = pool.get_nowait()
    except Empty:
        lease = _new_mysql_connection(payload, database)
    try:
        lease.ping()
        yield lease.conn
    except Exception:
        lease.close()
        raise
    else:
        try:
            pool.put_nowait(lease)
        except Full:
            lease.close()


def test_mysql_connection(payload: Any) -> dict[str, Any]:
    try:
        with external_mysql_connection(payload, _payload_value(payload, "database")) as mysql_conn:
            with mysql_conn.cursor() as cursor:
                cursor.execute("SELECT VERSION() AS version, CURRENT_USER() AS current_user, DATABASE() AS current_database")
                row = cursor.fetchone()
        return {
            "ok": True,
            "version": row["version"],
            "current_user": row["current_user"],
            "current_database": row["current_database"],
            "pool_size": MYSQL_POOL_SIZE,
            "ssl_enabled": bool(_payload_value(payload, "ssl_enabled", False)),
        }
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"MySQL 连接失败：{exc}") from exc


def browse_mysql_schema(payload: Any) -> dict[str, Any]:
    database = _payload_value(payload, "database")
    table = _payload_value(payload, "table")
    try:
        with external_mysql_connection(payload, database) as mysql_conn:
            with mysql_conn.cursor() as cursor:
                cursor.execute("SHOW DATABASES")
                databases = [
                    next(iter(row.values()))
                    for row in cursor.fetchall()
                    if next(iter(row.values())) not in MYSQL_SYSTEM_DATABASES
                ]

                tables: list[dict[str, Any]] = []
                columns: list[dict[str, Any]] = []
                if database:
                    cursor.execute(
                        """SELECT table_name, table_type, table_rows
                           FROM information_schema.tables
                           WHERE table_schema = %s
                           ORDER BY table_name""",
                        (database,),
                    )
                    tables = [
                        {
                            "name": row["table_name"],
                            "type": row["table_type"],
                            "rows": row["table_rows"] or 0,
                        }
                        for row in cursor.fetchall()
                    ]
                if database and table:
                    cursor.execute(
                        """SELECT column_name, data_type, column_type, is_nullable, column_key, column_comment
                           FROM information_schema.columns
                           WHERE table_schema = %s AND table_name = %s
                           ORDER BY ordinal_position""",
                        (database, table),
                    )
                    columns = [
                        {
                            "name": row["column_name"],
                            "data_type": row["data_type"],
                            "column_type": row["column_type"],
                            "nullable": row["is_nullable"] == "YES",
                            "key": row["column_key"],
                            "comment": row["column_comment"],
                        }
                        for row in cursor.fetchall()
                    ]
        return {"databases": databases, "tables": tables, "columns": columns}
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"MySQL schema 读取失败：{exc}") from exc


def import_mysql_dataset(payload: Any) -> dict:
    try:
        database = _payload_value(payload, "database")
        table = _payload_value(payload, "table")
        with external_mysql_connection(payload, database) as mysql_conn:
            query = f"SELECT * FROM {_quote_mysql_identifier(table)} LIMIT {int(_payload_value(payload, 'limit', 100000))}"
            frame = pd.read_sql_query(query, mysql_conn)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"MySQL 连接或读取失败：{exc}") from exc

    frame, original_columns = _normalize_frame(frame)
    display_name = payload.name or f"{payload.database}.{payload.table}"
    return _store_frame(
        frame,
        original_columns,
        source_type="mysql",
        name=display_name,
        description=payload.description,
    )


def list_datasets(allowed_ids: list[int] | None = None) -> list[dict]:
    with connect() as conn:
        if allowed_ids is None:
            rows = conn.execute("SELECT * FROM datasets ORDER BY id DESC").fetchall()
        elif allowed_ids:
            placeholders = ",".join("?" for _ in allowed_ids)
            rows = conn.execute(
                f"SELECT * FROM datasets WHERE id IN ({placeholders}) ORDER BY id DESC",
                allowed_ids,
            ).fetchall()
        else:
            rows = []
    return [dict(row) for row in rows]


def get_dataset(dataset_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        if not row:
            raise ValueError("数据集不存在")
        result = dict(row)
        result["columns"] = [
            dict(item)
            for item in conn.execute(
                "SELECT name, data_type, description, sample_value, null_rate FROM dataset_columns WHERE dataset_id = ? ORDER BY id",
                (dataset_id,),
            ).fetchall()
        ]
        preview = conn.execute(f"SELECT * FROM {_quote_identifier(result['table_name'])} LIMIT 100").fetchall()
        result["preview"] = [dict(item) for item in preview]
        return result


def update_dataset(dataset_id: int, name: str, description: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT id FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        if not row:
            raise ValueError("数据集不存在")
        conn.execute(
            "UPDATE datasets SET name = ?, description = ? WHERE id = ?",
            (name.strip(), description, dataset_id),
        )
    return get_dataset(dataset_id)


def update_column_description(dataset_id: int, column_name: str, description: str) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM dataset_columns WHERE dataset_id = ? AND name = ?",
            (dataset_id, column_name),
        ).fetchone()
        if not row:
            raise ValueError("字段不存在")
        conn.execute(
            "UPDATE dataset_columns SET description = ? WHERE dataset_id = ? AND name = ?",
            (description, dataset_id, column_name),
        )
    return get_dataset(dataset_id)


def delete_dataset(dataset_id: int) -> None:
    knowledge_ids: list[int] = []
    with connect() as conn:
        row = conn.execute("SELECT table_name FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        if not row:
            raise ValueError("数据集不存在")
        table_name = row["table_name"]
        knowledge_ids = [
            int(item["id"])
            for item in conn.execute("SELECT id FROM knowledge_chunks WHERE dataset_id = ?", (dataset_id,)).fetchall()
        ]
        conn.execute(f"DROP TABLE IF EXISTS {_quote_identifier(table_name)}")
        conn.execute("DELETE FROM knowledge_chunks WHERE dataset_id = ?", (dataset_id,))
        conn.execute("DELETE FROM dataset_columns WHERE dataset_id = ?", (dataset_id,))
        conn.execute("DELETE FROM data_lineage WHERE dataset_id = ?", (dataset_id,))
        conn.execute("DELETE FROM user_dataset_permissions WHERE dataset_id = ?", (dataset_id,))
        conn.execute("UPDATE sessions SET dataset_id = NULL WHERE dataset_id = ?", (dataset_id,))
        conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
    if knowledge_ids:
        try:
            delete_knowledge_vectors(knowledge_ids)
        except VectorStoreError:
            # 数据集删除不应因为外部向量服务暂时不可用而失败；下次重建索引会自动修正。
            pass


def _quality_level(score: int) -> str:
    if score >= 90:
        return "优秀"
    if score >= 75:
        return "良好"
    if score >= 60:
        return "一般"
    return "较差"


def _quality_json_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value.item() if hasattr(value, "item") else value


def _quality_json_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _quality_json_value(value) for key, value in row.items()}


def _duplicate_value_report(frame: pd.DataFrame, columns: list[dict], row_count: int) -> list[dict[str, Any]]:
    duplicate_values: list[dict[str, Any]] = []
    if row_count <= 1 or frame.empty:
        return duplicate_values
    for column in columns:
        name = column["name"]
        if name not in frame:
            continue
        series = frame[name].dropna().map(str).map(str.strip)
        series = series[series != ""]
        if series.empty:
            continue
        counts = series.value_counts()
        duplicated = counts[counts > 1]
        if duplicated.empty:
            continue
        duplicate_cells = int(duplicated.sum() - len(duplicated))
        duplicate_values.append(
            {
                "column": name,
                "duplicate_value_count": duplicate_cells,
                "duplicate_rate": round(duplicate_cells / max(int(series.count()), 1), 4),
                "top_values": [
                    {"value": str(value), "count": int(count)}
                    for value, count in duplicated.head(5).items()
                ],
            }
        )
    return sorted(duplicate_values, key=lambda item: item["duplicate_value_count"], reverse=True)


def dataset_quality(dataset_id: int) -> dict[str, Any]:
    dataset = get_dataset(dataset_id)
    columns = dataset.get("columns", [])
    table_name = dataset["table_name"]
    with connect() as conn:
        row_count = int(conn.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}").fetchone()[0])
        missing = []
        for column in columns:
            name = column["name"]
            quoted_name = _quote_identifier(name)
            missing_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table_name)} WHERE {quoted_name} IS NULL OR TRIM(CAST({quoted_name} AS CHAR)) = ''"
                ).fetchone()[0]
            )
            missing.append(
                {
                    "column": name,
                    "missing_count": missing_count,
                    "missing_rate": round(missing_count / row_count, 4) if row_count else 0,
                }
            )
        if columns:
            group_columns = ", ".join(_quote_identifier(column["name"]) for column in columns)
            duplicate_row = conn.execute(
                f"SELECT COALESCE(SUM(cnt - 1), 0) FROM (SELECT COUNT(*) AS cnt FROM {_quote_identifier(table_name)} GROUP BY {group_columns} HAVING cnt > 1) t"
            ).fetchone()
            duplicate_rows = int(duplicate_row[0] or 0)
        else:
            duplicate_rows = 0
        rows = conn.execute(f"SELECT * FROM {_quote_identifier(table_name)}").fetchall()
        frame = pd.DataFrame([dict(row) for row in rows])

    duplicate_rate = round(duplicate_rows / row_count, 4) if row_count else 0
    duplicate_samples = []
    if not frame.empty:
        duplicate_samples = [_quality_json_row(row) for row in frame[frame.duplicated(keep=False)].head(5).to_dict(orient="records")]
    duplicate_values = _duplicate_value_report(frame, columns, row_count)

    outliers = []
    numeric_cells = 0
    for column in columns:
        if not any(token in column["data_type"].lower() for token in NUMERIC_TYPE_TOKENS):
            continue
        if column["name"] not in frame:
            continue
        series = pd.to_numeric(frame[column["name"]], errors="coerce").dropna()
        numeric_cells += int(series.count())
        if series.empty:
            continue
        q1 = float(series.quantile(0.25))
        q3 = float(series.quantile(0.75))
        iqr = q3 - q1
        if iqr <= 0:
            count = 0
            lower = upper = None
            sample_values = []
        else:
            lower = q1 - 1.5 * iqr
            upper = q3 + 1.5 * iqr
            outlier_series = series[(series < lower) | (series > upper)]
            count = int(outlier_series.count())
            sample_values = [_quality_json_value(value) for value in outlier_series.head(5).tolist()]
        outliers.append(
            {
                "column": column["name"],
                "outlier_count": count,
                "outlier_rate": round(count / max(int(series.count()), 1), 4),
                "lower_bound": round(lower, 4) if lower is not None else None,
                "upper_bound": round(upper, 4) if upper is not None else None,
                "sample_values": sample_values,
            }
        )

    total_cells = max(row_count * len(columns), 1)
    missing_cells = sum(item["missing_count"] for item in missing)
    missing_rate = round(missing_cells / total_cells, 4) if row_count and columns else 0
    outlier_count = sum(item["outlier_count"] for item in outliers)
    outlier_rate = round(outlier_count / max(numeric_cells, 1), 4) if numeric_cells else 0
    completeness_score = max(0, round(100 * (1 - missing_rate)))
    uniqueness_score = max(0, round(100 * (1 - duplicate_rate)))
    validity_score = max(0, round(100 * (1 - outlier_rate)))
    quality_score = max(
        0,
        min(
            100,
            round(
                completeness_score * 0.4
                + uniqueness_score * 0.3
                + validity_score * 0.2
                + 10
            ),
        ),
    )
    quality_level = _quality_level(quality_score)

    return {
        "dataset_id": dataset_id,
        "row_count": row_count,
        "column_count": len(columns),
        "quality_score": quality_score,
        "quality_level": quality_level,
        "score_detail": {
            "completeness_score": completeness_score,
            "uniqueness_score": uniqueness_score,
            "validity_score": validity_score,
            "missing_rate": missing_rate,
            "duplicate_rate": duplicate_rate,
            "outlier_rate": outlier_rate,
        },
        "duplicate_rows": duplicate_rows,
        "duplicate_rate": duplicate_rate,
        "duplicate_samples": duplicate_samples,
        "duplicate_values": duplicate_values,
        "missing": missing,
        "outliers": outliers,
        "summary": [
            f"质量评分 {quality_score}/100（{quality_level}）",
            f"共 {row_count} 行、{len(columns)} 个字段",
            f"缺失单元格 {missing_cells} 个，缺失率 {missing_rate * 100:.2f}%",
            f"重复行 {duplicate_rows} 行，重复率 {duplicate_rate * 100:.2f}%",
            f"疑似异常值 {outlier_count} 个，异常率 {outlier_rate * 100:.2f}%",
        ],
    }


def save_column_lineage(dataset_id: int, columns_lineage: list[dict[str, Any]]) -> list[dict]:
    """Upsert data lineage records for dataset columns."""
    with connect() as conn:
        for item in columns_lineage:
            conn.execute(
                """INSERT INTO data_lineage(dataset_id, column_name, source_table, source_column, transformation)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(dataset_id, column_name)
                   DO UPDATE SET source_table=excluded.source_table, source_column=excluded.source_column,
                                 transformation=excluded.transformation, updated_at=CURRENT_TIMESTAMP""",
                (
                    dataset_id,
                    item["column_name"],
                    item.get("source_table"),
                    item.get("source_column"),
                    item.get("transformation"),
                ),
            )
    return get_column_lineage(dataset_id)


def get_column_lineage(dataset_id: int) -> list[dict]:
    """Get all lineage records for a dataset."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM data_lineage WHERE dataset_id = ? ORDER BY column_name",
            (dataset_id,),
        ).fetchall()
    return [dict(row) for row in rows]

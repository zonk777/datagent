from __future__ import annotations

import math
import random
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from urllib.parse import quote_plus
from typing import Any, Iterator

import queue
import threading

import pymysql
from pymysql.cursors import DictCursor

from .config import get_settings


SCHEMA_SQLITE = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL,
    table_name TEXT NOT NULL UNIQUE,
    row_count INTEGER NOT NULL DEFAULT 0,
    column_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ready',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dataset_columns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    data_type TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    sample_value TEXT,
    null_rate REAL NOT NULL DEFAULT 0,
    UNIQUE(dataset_id, name)
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'business_rule',
    dataset_id INTEGER REFERENCES datasets(id) ON DELETE CASCADE,
    created_by INTEGER REFERENCES admin_users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    dataset_id INTEGER REFERENCES datasets(id) ON DELETE SET NULL,
    user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS session_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    file_type TEXT NOT NULL DEFAULT '',
    file_size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL,
    storage_path TEXT NOT NULL DEFAULT '',
    extracted_text TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session_id, sha256)
);

CREATE TABLE IF NOT EXISTS session_document_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES session_documents(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_session_documents_session_id ON session_documents(session_id);
CREATE INDEX IF NOT EXISTS idx_session_document_chunks_session_id ON session_document_chunks(session_id);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'success',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin',
    is_initial_admin INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES admin_users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_dataset_permissions (
    user_id INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    column_mask TEXT,
    PRIMARY KEY(user_id, dataset_id)
);

CREATE TABLE IF NOT EXISTS data_lineage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    column_name TEXT NOT NULL,
    source_table TEXT,
    source_column TEXT,
    transformation TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(dataset_id, column_name)
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token TEXT PRIMARY KEY,
    admin_id INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL
);
"""


SCHEMA_MYSQL = [
    """
    CREATE TABLE IF NOT EXISTS datasets (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        description TEXT NOT NULL,
        source_type VARCHAR(64) NOT NULL,
        table_name VARCHAR(128) NOT NULL UNIQUE,
        row_count INT NOT NULL DEFAULT 0,
        column_count INT NOT NULL DEFAULT 0,
        status VARCHAR(32) NOT NULL DEFAULT 'ready',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS dataset_columns (
        id INT AUTO_INCREMENT PRIMARY KEY,
        dataset_id INT NOT NULL,
        name VARCHAR(255) NOT NULL,
        data_type VARCHAR(128) NOT NULL,
        description TEXT NOT NULL,
        sample_value TEXT,
        null_rate DOUBLE NOT NULL DEFAULT 0,
        UNIQUE KEY uq_dataset_column (dataset_id, name),
        CONSTRAINT fk_dataset_columns_dataset FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_chunks (
        id INT AUTO_INCREMENT PRIMARY KEY,
        title VARCHAR(255) NOT NULL,
        content LONGTEXT NOT NULL,
        category VARCHAR(64) NOT NULL DEFAULT 'business_rule',
        dataset_id INT NULL,
        created_by INT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT fk_knowledge_dataset FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id VARCHAR(64) PRIMARY KEY,
        title VARCHAR(255) NOT NULL,
        dataset_id INT NULL,
        user_id INT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT fk_sessions_dataset FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INT AUTO_INCREMENT PRIMARY KEY,
        session_id VARCHAR(64) NOT NULL,
        role VARCHAR(16) NOT NULL,
        content LONGTEXT NOT NULL,
        payload LONGTEXT,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_messages_session_id (session_id),
        CONSTRAINT fk_messages_session FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS session_documents (
        id INT AUTO_INCREMENT PRIMARY KEY,
        session_id VARCHAR(64) NOT NULL,
        filename VARCHAR(512) NOT NULL,
        file_type VARCHAR(64) NOT NULL DEFAULT '',
        file_size BIGINT NOT NULL DEFAULT 0,
        sha256 VARCHAR(64) NOT NULL,
        storage_path VARCHAR(1024) NOT NULL DEFAULT '',
        extracted_text LONGTEXT NOT NULL,
        summary TEXT NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_session_document_hash (session_id, sha256),
        INDEX idx_session_documents_session_id (session_id),
        CONSTRAINT fk_session_documents_session FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS session_document_chunks (
        id INT AUTO_INCREMENT PRIMARY KEY,
        document_id INT NOT NULL,
        session_id VARCHAR(64) NOT NULL,
        chunk_index INT NOT NULL,
        content LONGTEXT NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_session_document_chunk (document_id, chunk_index),
        INDEX idx_session_document_chunks_session_id (session_id),
        CONSTRAINT fk_session_document_chunks_document FOREIGN KEY (document_id) REFERENCES session_documents(id) ON DELETE CASCADE,
        CONSTRAINT fk_session_document_chunks_session FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NULL,
        username VARCHAR(255),
        action VARCHAR(128) NOT NULL,
        resource_type VARCHAR(128) NOT NULL,
        resource_id VARCHAR(255),
        detail TEXT NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'success',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_audit_action (action),
        INDEX idx_audit_username (username),
        INDEX idx_audit_created_at (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255) NOT NULL UNIQUE,
        password_hash VARCHAR(512) NOT NULL,
        role VARCHAR(64) NOT NULL DEFAULT 'admin',
        is_initial_admin TINYINT(1) NOT NULL DEFAULT 0,
        created_by INT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT fk_admin_created_by FOREIGN KEY (created_by) REFERENCES admin_users(id) ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS user_dataset_permissions (
        user_id INT NOT NULL,
        dataset_id INT NOT NULL,
        column_mask JSON,
        PRIMARY KEY(user_id, dataset_id),
        CONSTRAINT fk_permission_user FOREIGN KEY (user_id) REFERENCES admin_users(id) ON DELETE CASCADE,
        CONSTRAINT fk_permission_dataset FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS data_lineage (
        id INT AUTO_INCREMENT PRIMARY KEY,
        dataset_id INT NOT NULL,
        column_name VARCHAR(128) NOT NULL,
        source_table VARCHAR(255),
        source_column VARCHAR(255),
        transformation TEXT,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_lineage (dataset_id, column_name),
        CONSTRAINT fk_lineage_dataset FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_sessions (
        token VARCHAR(128) PRIMARY KEY,
        admin_id INT NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at VARCHAR(64) NOT NULL,
        CONSTRAINT fk_admin_sessions_user FOREIGN KEY (admin_id) REFERENCES admin_users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]


class Row(dict):
    """Dict row that also supports integer indexes like sqlite3.Row."""

    def __init__(self, data: dict[str, Any], order: list[str] | None = None) -> None:
        super().__init__(data)
        self._order = order or list(data.keys())

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return super().__getitem__(self._order[key])
        return super().__getitem__(key)


class MySQLCursor:
    def __init__(self, cursor) -> None:
        self._cursor = cursor
        self._order: list[str] = []

    @property
    def lastrowid(self) -> int:
        return int(self._cursor.lastrowid or 0)

    def fetchone(self) -> Row | None:
        row = self._cursor.fetchone()
        return Row(row, self._order) if row is not None else None

    def fetchall(self) -> list[Row]:
        return [Row(row, self._order) for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class MySQLConnection:
    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple | list | None = None) -> MySQLCursor:
        sql = _mysql_sql(sql)
        cursor = self._conn.cursor()
        cursor.execute(sql, params if params else None)
        wrapped = MySQLCursor(cursor)
        wrapped._order = [item[0] for item in (cursor.description or [])]
        return wrapped

    def executemany(self, sql: str, params: list[tuple] | tuple[tuple, ...]) -> MySQLCursor:
        sql = _mysql_sql(sql)
        cursor = self._conn.cursor()
        cursor.executemany(sql, params)
        wrapped = MySQLCursor(cursor)
        wrapped._order = [item[0] for item in (cursor.description or [])]
        return wrapped

    def executescript(self, script: str) -> None:
        for statement in _split_sql_script(script):
            self.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


class SQLiteConnection:
    """SQLite wrapper that accepts the same placeholder style used by MySQL code."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple | list | None = None) -> sqlite3.Cursor:
        return self._conn.execute(_sqlite_sql(sql), params or ())

    def executemany(self, sql: str, params: list[tuple] | tuple[tuple, ...]) -> sqlite3.Cursor:
        return self._conn.executemany(_sqlite_sql(sql), params)

    def executescript(self, script: str) -> sqlite3.Cursor:
        return self._conn.executescript(_sqlite_sql(script))

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


def _split_sql_script(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";") if statement.strip() and not statement.strip().startswith("PRAGMA")]


def _mysql_sql(sql: str) -> str:
    sql = sql.strip()
    sql = sql.replace("INSERT OR IGNORE INTO", "INSERT IGNORE INTO")
    return sql.replace("?", "%s")


def _sqlite_sql(sql: str) -> str:
    sql = sql.strip()
    sql = sql.replace("INSERT IGNORE INTO", "INSERT OR IGNORE INTO")
    return sql.replace("%s", "?")


def using_mysql() -> bool:
    return get_settings().database_backend.lower() == "mysql"


def _engine():
    """SQLAlchemy engine for pandas to_sql/read_sql helpers."""
    from sqlalchemy import create_engine

    settings = get_settings()
    if settings.database_backend.lower() == "mysql":
        user = quote_plus(settings.mysql_user)
        password = quote_plus(settings.mysql_password)
        database = quote_plus(settings.mysql_database)
        url = f"mysql+pymysql://{user}:{password}@{settings.mysql_host}:{settings.mysql_port}/{database}?charset=utf8mb4"
    else:
        settings.database_file.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{settings.database_file.as_posix()}"
    return create_engine(url)


_mysql_pool: queue.Queue | None = None
_pool_lock = threading.Lock()
_POOL_SIZE = 10


def _get_mysql_connection():
    """Get a MySQL connection from the pool, or create a new one."""
    global _mysql_pool
    with _pool_lock:
        if _mysql_pool is None:
            _mysql_pool = queue.Queue(maxsize=_POOL_SIZE)
    settings = get_settings()
    try:
        conn = _mysql_pool.get_nowait()
        try:
            conn.ping(reconnect=False)
        except Exception:
            conn.close()
            conn = _create_mysql_conn(settings)
        return conn
    except queue.Empty:
        return _create_mysql_conn(settings)


def _return_mysql_connection(conn):
    """Return a healthy connection to the pool."""
    global _mysql_pool
    try:
        conn.ping(reconnect=False)
        _mysql_pool.put_nowait(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        try:
            replacement = _create_mysql_conn(get_settings())
            _mysql_pool.put_nowait(replacement)
        except Exception:
            pass  # silently drop dead connections; new ones created on demand


def _create_mysql_conn(settings):
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
        connect_timeout=settings.sql_timeout_seconds,
        read_timeout=settings.sql_timeout_seconds,
        write_timeout=settings.sql_timeout_seconds,
    )


@contextmanager
def connect() -> Iterator[Any]:
    settings = get_settings()
    mysql_conn = None
    if settings.database_backend.lower() == "mysql":
        conn = _get_mysql_connection()
        db = MySQLConnection(conn)
        mysql_conn = conn
    else:
        settings.database_file.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(settings.database_file, timeout=settings.sql_timeout_seconds)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys=ON")
        db = SQLiteConnection(raw)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        if mysql_conn is not None:
            _return_mysql_connection(mysql_conn)
        else:
            db.close()


def _migrate_mysql_schema(conn: Any) -> None:
    """Apply idempotent MySQL schema migrations for columns added after initial release."""
    migrations = [
        "ALTER TABLE knowledge_chunks ADD COLUMN created_by INT NULL",
    ]
    for stmt in migrations:
        try:
            conn.execute(stmt)
        except Exception:
            pass  # column already exists


def initialize_database() -> None:
    with connect() as conn:
        if using_mysql():
            for statement in SCHEMA_MYSQL:
                conn.execute(statement)
            _migrate_mysql_schema(conn)
        else:
            conn.executescript(SCHEMA_SQLITE)
            _migrate_sqlite_schema(conn)
        _seed_initial_admin(conn)
        exists = conn.execute("SELECT id FROM datasets LIMIT 1").fetchone()
        if not exists:
            _seed_demo_dataset(conn)


def _has_sqlite_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _migrate_sqlite_schema(conn: sqlite3.Connection) -> None:
    if not _has_sqlite_column(conn, "admin_users", "role"):
        conn.execute("ALTER TABLE admin_users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
    if not _has_sqlite_column(conn, "audit_logs", "user_id"):
        conn.execute("ALTER TABLE audit_logs ADD COLUMN user_id INTEGER")
    if not _has_sqlite_column(conn, "audit_logs", "username"):
        conn.execute("ALTER TABLE audit_logs ADD COLUMN username TEXT")
    if not _has_sqlite_column(conn, "sessions", "user_id"):
        conn.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER")
    if not _has_sqlite_column(conn, "user_dataset_permissions", "column_mask"):
        conn.execute("ALTER TABLE user_dataset_permissions ADD COLUMN column_mask TEXT")
    if not _has_sqlite_column(conn, "knowledge_chunks", "created_by"):
        conn.execute("ALTER TABLE knowledge_chunks ADD COLUMN created_by INTEGER REFERENCES admin_users(id) ON DELETE SET NULL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_dataset_permissions (
            user_id INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
            dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
            column_mask TEXT,
            PRIMARY KEY(user_id, dataset_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS data_lineage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
            column_name TEXT NOT NULL,
            source_table TEXT,
            source_column TEXT,
            transformation TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(dataset_id, column_name)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS session_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            file_type TEXT NOT NULL DEFAULT '',
            file_size INTEGER NOT NULL DEFAULT 0,
            sha256 TEXT NOT NULL,
            storage_path TEXT NOT NULL DEFAULT '',
            extracted_text TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id, sha256)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS session_document_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES session_documents(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(document_id, chunk_index)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_documents_session_id ON session_documents(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_document_chunks_session_id ON session_document_chunks(session_id)")
    conn.execute("UPDATE admin_users SET role = 'initial_admin' WHERE is_initial_admin = 1")
    conn.execute("UPDATE admin_users SET role = 'admin' WHERE role IS NULL OR role = ''")


def _seed_initial_admin(conn: Any) -> None:
    exists = conn.execute("SELECT id FROM admin_users WHERE username = ?", ("liuze",)).fetchone()
    if exists:
        return
    from .services.auth import hash_password

    conn.execute(
        "INSERT INTO admin_users(username, password_hash, role, is_initial_admin) VALUES (?, ?, 'initial_admin', 1)",
        ("liuze", hash_password("18437431")),
    )


def _seed_demo_dataset(conn: Any) -> None:
    table_name = "data_demo_sales"
    if using_mysql():
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {table_name} (
                order_date VARCHAR(32) NOT NULL,
                region VARCHAR(64) NOT NULL,
                product_category VARCHAR(128) NOT NULL,
                channel VARCHAR(128) NOT NULL,
                sales_amount DOUBLE NOT NULL,
                order_count INT NOT NULL,
                profit DOUBLE NOT NULL,
                complaint_count INT NOT NULL,
                visits INT NOT NULL,
                conversions INT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""
        )
    else:
        conn.execute(
            f"""CREATE TABLE {table_name} (
                order_date TEXT NOT NULL,
                region TEXT NOT NULL,
                product_category TEXT NOT NULL,
                channel TEXT NOT NULL,
                sales_amount REAL NOT NULL,
                order_count INTEGER NOT NULL,
                profit REAL NOT NULL,
                complaint_count INTEGER NOT NULL,
                visits INTEGER NOT NULL,
                conversions INTEGER NOT NULL
            )"""
        )

    rng = random.Random(20250622)
    regions = ["华东", "华南", "华北", "西南"]
    categories = ["智能设备", "办公用品", "家居生活", "企业服务"]
    channels = ["线上商城", "直营网点", "渠道合作"]
    start = date.today() - timedelta(days=119)
    rows = []
    for day_index in range(120):
        current = start + timedelta(days=day_index)
        trend = 1 + day_index / 900
        weekly = 1 + 0.09 * math.sin(day_index / 7 * math.pi * 2)
        for region_index, region in enumerate(regions):
            category = categories[(day_index + region_index) % len(categories)]
            channel = channels[(day_index + region_index * 2) % len(channels)]
            region_factor = [1.22, 1.08, 0.94, 0.82][region_index]
            decline = 0.72 if region == "华东" and 73 <= day_index <= 86 else 1.0
            orders = max(18, int((42 + rng.randint(-8, 11)) * region_factor * weekly * decline))
            unit_price = 168 + categories.index(category) * 38 + rng.randint(-12, 20)
            sales = round(orders * unit_price * trend, 2)
            profit = round(sales * (0.17 + rng.random() * 0.08), 2)
            complaints = max(0, int(orders * (0.012 + rng.random() * 0.026)))
            visits = orders * rng.randint(14, 22)
            conversions = orders + rng.randint(0, 5)
            rows.append((current.isoformat(), region, category, channel, sales, orders, profit, complaints, visits, conversions))

    conn.executemany(
        f"INSERT INTO {table_name} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    cursor = conn.execute(
        """INSERT INTO datasets(name, description, source_type, table_name, row_count, column_count)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("企业经营演示数据", "近 120 天区域、品类、渠道经营指标", "demo", table_name, len(rows), 10),
    )
    dataset_id = cursor.lastrowid
    columns = [
        ("order_date", "DATE", "订单日期", rows[0][0]),
        ("region", "TEXT", "销售区域", rows[0][1]),
        ("product_category", "TEXT", "产品类别", rows[0][2]),
        ("channel", "TEXT", "销售渠道", rows[0][3]),
        ("sales_amount", "DECIMAL", "销售额，单位为元", str(rows[0][4])),
        ("order_count", "INTEGER", "成交订单数", str(rows[0][5])),
        ("profit", "DECIMAL", "毛利润，单位为元", str(rows[0][6])),
        ("complaint_count", "INTEGER", "客户投诉数量", str(rows[0][7])),
        ("visits", "INTEGER", "访问人数", str(rows[0][8])),
        ("conversions", "INTEGER", "完成转化人数", str(rows[0][9])),
    ]
    conn.executemany(
        """INSERT INTO dataset_columns(dataset_id, name, data_type, description, sample_value)
           VALUES (?, ?, ?, ?, ?)""",
        [(dataset_id, *column) for column in columns],
    )
    knowledge = [
        ("销售额口径", "销售额为已完成交易的含税成交金额之和，退款订单不计入。", "metric", dataset_id),
        ("投诉率口径", "投诉率 = 投诉数量 / 成交订单数，分析时至少聚合到日级。", "metric", dataset_id),
        ("转化率口径", "转化率 = 完成转化人数 / 访问人数。", "metric", dataset_id),
        ("区域说明", "企业当前按华东、华南、华北、西南四个大区进行经营管理。", "data_dictionary", dataset_id),
        ("异常判断规则", "指标较前一可比周期偏离 15% 以上时，应提示业务人员进一步核查。", "business_rule", dataset_id),
    ]
    conn.executemany(
        "INSERT INTO knowledge_chunks(title, content, category, dataset_id) VALUES (?, ?, ?, ?)",
        knowledge,
    )

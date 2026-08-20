import os
import socket

# Monkeypatch socket.getaddrinfo to bypass local DNS resolution timeouts if requested
if os.getenv("BYPASS_DNS_TIMEOUTS", "false").lower() == "true":
    _orig_getaddrinfo = socket.getaddrinfo

    def custom_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        """
        Custom DNS resolution function used to monkeypatch socket.getaddrinfo.
        This forces specific IP addresses for the Gemini API and Neon PostgreSQL pooler
        to bypass potentially slow or failing local DNS lookups, improving startup and request speed.
        """
        if host == "generativelanguage.googleapis.com":
            return _orig_getaddrinfo("172.217.117.4", port, family, type, proto, flags)
        elif host == "ep-wispy-sun-axuj92z8-pooler.c-4.us-east-2.aws.neon.tech":
            return _orig_getaddrinfo("18.226.241.3", port, family, type, proto, flags)
        return _orig_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = custom_getaddrinfo

import time

from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import cache_validator
import db_connections
import schema_hasher
from analytics import router as analytics_router
from database import get_db_session, get_engine
from db_connections import InvalidDatabaseFileError
from db_models import Base
from models import ExecuteSQLRequest, QueryRequest, QueryResponse, UploadDatabaseResponse
from question_validator import validate_question
from schema_introspection import format_schema_for_context, get_database_schema
from sql_generator import generate_sql_from_question
from sql_templates import try_template_match
from sql_validator import is_write_query, validate_sql

app = FastAPI(title="Text-to-SQL Analytics API")
app.include_router(analytics_router)

# Create the query cache tables on startup if they don't already exist.
Base.metadata.create_all(bind=get_engine())
# Safely ensure all columns and tables exist in the database
_startup_statements = [
    "ALTER TABLE cache_audit_log ADD COLUMN IF NOT EXISTS connection_id VARCHAR(64)",
    "ALTER TABLE dynamic_query_cache ADD COLUMN IF NOT EXISTS connection_id VARCHAR(64)",
    "ALTER TABLE dynamic_query_cache ADD COLUMN IF NOT EXISTS question TEXT",
    "ALTER TABLE dynamic_query_cache ADD COLUMN IF NOT EXISTS question_hash VARCHAR(64)",
    "ALTER TABLE dynamic_query_cache ADD COLUMN IF NOT EXISTS schema_hash_at_cache_time VARCHAR(64)",
    "ALTER TABLE dynamic_query_cache ADD COLUMN IF NOT EXISTS api_tokens_used INTEGER DEFAULT 0",
    "ALTER TABLE dynamic_query_cache ADD COLUMN IF NOT EXISTS api_cost DOUBLE PRECISION DEFAULT 0.0",
    "ALTER TABLE dynamic_query_cache ADD COLUMN IF NOT EXISTS hit_count INTEGER DEFAULT 0",
    "ALTER TABLE dynamic_query_cache ADD COLUMN IF NOT EXISTS cache_status VARCHAR(32) DEFAULT 'miss'",
    "ALTER TABLE dynamic_query_cache ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
]
for _stmt in _startup_statements:
    try:
        with get_engine().begin() as _conn:
            _conn.execute(text(_stmt))
    except Exception:
        pass

ENABLE_QUERY_CACHE = os.getenv("ENABLE_QUERY_CACHE", "true").lower() == "true"
CACHE_INVALIDATION_ON_SCHEMA_CHANGE = (
    os.getenv("CACHE_INVALIDATION_ON_SCHEMA_CHANGE", "true").lower() == "true"
)
HAIKU_PRICE_PER_TOKEN = float(os.getenv("HAIKU_PRICE_PER_TOKEN", "0.0000015"))

allowed_origins_str = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,https://main.d3dz7qujv68w6q.amplifyapp.com")
allowed_origins = [origin.strip().rstrip("/") for origin in allowed_origins_str.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _run_select(sql: str, engine: Engine) -> tuple[list[str], list[dict]]:
    """
    Executes a read-only SELECT query against the provided database engine.
    
    Returns:
        A tuple containing:
        - A list of column names (strings).
        - A list of rows (each row is a dictionary mapping column names to values).
    """
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        columns = list(result.keys())
        rows = [dict(zip(columns, row)) for row in result.fetchall()]
    return columns, rows


def _run_write(sql: str, engine: Engine) -> int:
    """
    Executes a write or DDL statement (e.g., INSERT, UPDATE, DELETE, CREATE) against the database.
    This runs in a transaction context (engine.begin()) to ensure safety.
    
    Returns:
        The number of rows affected by the query. Returns 0 if the query does not affect rows.
    """
    with engine.begin() as conn:
        result = conn.execute(text(sql))
        return result.rowcount if result.rowcount and result.rowcount >= 0 else 0


def _resolve_engine(connection_id: str | None) -> Engine:
    """
    Determines which SQLAlchemy Engine to use for the incoming request.
    
    If a connection_id is provided, it retrieves the engine for the corresponding
    user-uploaded SQLite database. If connection_id is None, it defaults to the
    main PostgreSQL engine (configured via DATABASE_URL).
    """
    try:
        return db_connections.get_engine_for_connection(connection_id, get_engine())
    except KeyError:
        raise HTTPException(status_code=404, detail="Uploaded database session not found. Please re-upload your file.")


@app.get("/health")
def health():
    """
    Basic health check endpoint. Used by deployment platforms (like AWS, Render, etc.)
    and load balancers to verify that the API is running and responsive.
    """
    return {"status": "ok"}


@app.get("/schema")
def schema(connection_id: str | None = None):
    """
    Retrieves the database schema (tables and columns) for the active connection.
    This is called by the frontend to display the available schema to the user
    and is also used internally to provide context to the LLM when generating SQL.
    """
    try:
        engine = _resolve_engine(connection_id)
        schema_data = get_database_schema(engine)
        return schema_data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read the database schema: {str(e)}")


@app.post("/database/upload", response_model=UploadDatabaseResponse)
async def upload_database(file: UploadFile):
    """
    Accepts a user-uploaded SQLite database file (.db).
    The file is saved locally and registered as an active, read-only ad-hoc connection.
    This allows users to immediately query their own custom datasets without
    overwriting or affecting the main PostgreSQL database.
    """
    file_bytes = await file.read()
    try:
        connection = db_connections.register_uploaded_db(file_bytes, file.filename or "uploaded.db")
    except InvalidDatabaseFileError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return UploadDatabaseResponse(connection_id=connection.connection_id, filename=connection.filename)


@app.delete("/database/{connection_id}")
def remove_uploaded_database(connection_id: str):
    """
    Cleans up and removes a previously uploaded ad-hoc SQLite database connection.
    This frees up resources and deletes the file from the server's local storage.
    """
    db_connections.remove_connection(connection_id)
    return {"status": "removed"}


@app.post("/query", response_model=QueryResponse)
def query(request: QueryRequest, db: Session = Depends(get_db_session)):
    """
    The main text-to-SQL endpoint. It takes a natural language question and returns
    both the generated SQL and the resulting data.
    
    Workflow:
    1. Check Cache: Looks for an existing, valid generated query for this exact question.
    2. Fallback to Templates: If no cache exists, checks if the question matches a known, simple regex pattern (fast-path).
    3. Generate via LLM: If templates fail, calls Gemini/Claude to generate the SQL.
    4. Validation & Execution: Ensures the query is safe, runs it against the selected database, and records metrics (cost, time).
    """
    engine = _resolve_engine(request.connection_id)
    start_time = time.perf_counter()

    from_cache = False
    cache_status = "n/a"
    schema_hash = None
    tokens_used = 0
    api_cost = 0.0
    api_cost_saved = 0.0
    source = "template"
    sql = None
    cached_entry = None
    schema_changed_invalidation = False
    question_hash = cache_validator.compute_question_hash(request.question, request.connection_id)

    # 1. Try the Redis cache first (only ever populated by LLM-generated queries).
    if ENABLE_QUERY_CACHE:
        cached_entry = cache_validator.check_redis_cache(
            request.question, request.connection_id, engine
        )
        if cached_entry is not None:
            sql = cached_entry["sql"]
            source = "llm"
            from_cache = True
            cache_status = "hit"
            schema_hash = cached_entry.get("schema_hash")
            api_cost_saved = cached_entry.get("cost", 0.0)
        else:
            cache_status = "miss"

    # 2. No valid cache entry: try the deterministic template match, then the LLM.
    if sql is None:
        try:
            schema_data = get_database_schema(engine)
            tables = schema_data.get("tables", {})
            schema_context = format_schema_for_context(schema_data)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read the database schema: {str(e)}")

        # Validate the question before hitting templates or the LLM.
        try:
            validate_question(request.question, tables)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        sql = try_template_match(request.question, tables)
        source = "template"

        if sql is None:
            try:
                sql, tokens_used = generate_sql_from_question(request.question, schema_context)
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))
            source = "llm"

            try:
                schema_hash = schema_hasher.get_schema_hash_for_query(sql, engine)
            except Exception:
                schema_hash = None

            api_cost = round(tokens_used * HAIKU_PRICE_PER_TOKEN, 6)

            if ENABLE_QUERY_CACHE and schema_hash is not None:
                cache_validator.update_redis_cache(
                    question=request.question,
                    connection_id=request.connection_id,
                    sql=sql,
                    engine=engine,
                    tokens_used=tokens_used,
                    api_cost=api_cost,
                )

    # If the generated query is a write / DDL statement, either execute it
    # (when allow_write is on) or return a preview.
    if is_write_query(sql):
        if not request.allow_write:
            return QueryResponse(
                question=request.question,
                sql=sql,
                columns=[],
                rows=[],
                row_count=0,
                source=source,
                is_preview=True,
                from_cache=False,
                is_cached=False,
                cache_status="n/a",
                execution_time_ms=0,
                generation_time_ms=0,
                api_tokens_used=tokens_used,
                api_cost=api_cost,
                api_cost_saved=0.0,
                cost_saved=0.0,
            )

        # Write mode is ON — execute the statement.
        try:
            affected = _run_write(sql, engine)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Write query failed to execute: {str(e)}")

        execution_time_ms = int((time.perf_counter() - start_time) * 1000)

        return QueryResponse(
            question=request.question,
            sql=sql,
            columns=["affected_rows"],
            rows=[{"affected_rows": affected}],
            row_count=1,
            source=source,
            is_preview=False,
            from_cache=False,
            is_cached=False,
            cache_status="n/a",
            execution_time_ms=execution_time_ms,
            generation_time_ms=execution_time_ms,
            api_tokens_used=tokens_used,
            api_cost=api_cost,
            api_cost_saved=0.0,
            cost_saved=0.0,
        )

    try:
        validate_sql(sql)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        columns, rows = _run_select(sql, engine)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Query failed to run: {str(e)}")

    execution_time_ms = int((time.perf_counter() - start_time) * 1000)

    if from_cache and cached_entry is not None:
        cache_validator.record_cache_hit(
            question=request.question,
            cached_data=cached_entry,
            execution_time_ms=execution_time_ms,
            connection_id=request.connection_id,
        )
    elif source == "llm":
        cache_validator.record_cache_miss(
            question=request.question,
            execution_time_ms=execution_time_ms,
            tokens_used=tokens_used,
            schema_hash=schema_hash,
            cache_status=cache_status,
            connection_id=request.connection_id,
        )

    return QueryResponse(
        question=request.question,
        sql=sql,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        source=source,
        from_cache=from_cache,
        is_cached=from_cache,
        cache_status=cache_status,
        schema_hash=schema_hash,
        execution_time_ms=execution_time_ms,
        generation_time_ms=execution_time_ms,
        api_tokens_used=tokens_used,
        api_cost=api_cost,
        api_cost_saved=api_cost_saved,
        cost_saved=api_cost_saved,
    )


@app.post("/execute-sql")
def execute_sql(request: ExecuteSQLRequest):
    """
    Executes a raw SQL statement provided directly by the client.
    This bypasses natural language generation, but the query is still strictly validated
    to ensure it is a safe, read-only SELECT statement.
    """
    try:
        validate_sql(request.sql)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    engine = _resolve_engine(request.connection_id)

    try:
        columns, rows = _run_select(request.sql, engine)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Query failed to run. Please check your SQL and try again: {str(e)}")

    return {"sql": request.sql, "columns": columns, "rows": rows, "row_count": len(rows)}

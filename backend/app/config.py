from __future__ import annotations

from functools import lru_cache
from datetime import date
from pydantic import BaseModel
from dotenv import load_dotenv
import os
from pathlib import Path


class Settings(BaseModel):
    process_role: str
    environment: str
    app_timezone: str
    cors_allow_origins: list[str]
    phcenter_token: str | None
    phcenter_base_url: str
    provisor_base_url: str
    provisor_login: str | None
    provisor_password: str | None
    provisor_price_total_timeout_seconds: int
    provisor_price_read_timeout_seconds: int
    provisor_price_connect_timeout_seconds: int
    provisor_goods_connect_timeout_seconds: float
    provisor_goods_read_timeout_seconds: float
    provisor_goods_total_timeout_seconds: float
    provisor_goods_search_max_results: int
    provisor_auto_refresh_enabled: bool
    provisor_auto_refresh_cron: str
    provisor_auto_refresh_mode: str
    provisor_auto_refresh_max_parallel_accounts: int
    provisor_auto_refresh_max_parallel_plk: int
    provisor_auto_refresh_keep_last_success: bool
    provisor_auto_refresh_timezone: str
    emit_worker_enabled: bool
    emit_filial_ids: list[int]
    emit_temp_dir: str
    emit_download_timeout_seconds: int
    emit_max_file_size_gb: float
    emit_batch_insert_size: int
    emit_delete_temp_after_success: bool
    emit_cleanup_temp_hours: int
    emit_min_free_disk_gb: float
    emit_max_concurrent_filials: int
    emit_min_final_rows: int
    emit_min_row_ratio: float
    emit_refresh_stale_timeout_seconds: int
    emit_heartbeat_interval_seconds: int
    emit_max_memory_mb: int
    emit_cron: str
    emit_timezone: str
    parser_worker_poll_interval_seconds: float
    parser_worker_concurrency: int
    parser_worker_stale_heartbeat_seconds: int
    parser_daily_schedule_enabled: bool
    parser_daily_schedule_cron: str
    parser_daily_catchup_max_hours: float
    parser_daily_schedule_until_date: date | None
    parser_daily_baseline_window: int
    parser_daily_baseline_min_samples: int
    parser_daily_baseline_ratio: float
    vidman_base_url: str
    vidman_login_path: str
    vidman_price_base_url: str
    price_source_secret: str | None
    redis_url: str | None
    sap_balance_enabled: bool
    sap_balance_schedule_enabled: bool
    sap_balance_base_url: str
    sap_balance_username: str | None
    sap_balance_password: str | None
    sap_balance_company_code: str | None
    sap_balance_cron: str
    sap_balance_timeout_seconds: int
    sap_balance_branch_map_json: str
    sap_manual_precedence_hours: int


@lru_cache
def get_settings() -> Settings:
    root_env = Path(__file__).resolve().parents[2] / ".env"
    load_dotenv(dotenv_path=root_env if root_env.exists() else None)
    environment = os.getenv("ENVIRONMENT", "dev")
    process_role = os.getenv("PROCESS_ROLE", "all").strip().lower() or "all"
    if process_role not in {"all", "web", "worker"}:
        raise ValueError("PROCESS_ROLE must be one of: all, web, worker")

    cors_allow_origins_raw = os.getenv("CORS_ALLOW_ORIGINS")
    if cors_allow_origins_raw:
        cors_allow_origins = [x.strip() for x in cors_allow_origins_raw.split(",") if x.strip()]
    elif environment.strip().lower() in {"prod", "production"}:
        cors_allow_origins = []
    else:
        cors_allow_origins = ["http://localhost:5173"]

    def env_bool(name: str, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

    def env_int_list(name: str, default: str) -> list[int]:
        raw = os.getenv(name, default)
        out: list[int] = []
        for item in str(raw or "").split(","):
            item = item.strip()
            if not item:
                continue
            try:
                out.append(int(item))
            except Exception:
                continue
        return list(dict.fromkeys(out))

    schedule_until_raw = os.getenv("PARSER_DAILY_SCHEDULE_UNTIL_DATE", "").strip()
    try:
        parser_daily_schedule_until_date = date.fromisoformat(schedule_until_raw) if schedule_until_raw else None
    except ValueError as exc:
        raise ValueError("PARSER_DAILY_SCHEDULE_UNTIL_DATE must use YYYY-MM-DD format") from exc

    return Settings(
        process_role=process_role,
        environment=environment,
        app_timezone=os.getenv("APP_TIMEZONE", os.getenv("TZ", "Asia/Qyzylorda")).strip() or "Asia/Qyzylorda",
        cors_allow_origins=cors_allow_origins,
        phcenter_token=os.getenv("PHCENTER_TOKEN"),
        phcenter_base_url=os.getenv("PHCENTER_BASE_URL", "https://ph.center"),
        provisor_base_url=os.getenv("PROVISOR_BASE_URL", "https://api.provisor.kz"),
        provisor_login=os.getenv("PROVISOR_LOGIN"),
        provisor_password=os.getenv("PROVISOR_PASSWORD"),
        provisor_price_total_timeout_seconds=int(os.getenv("PROVISOR_PRICE_TOTAL_TIMEOUT_SECONDS", "120")),
        provisor_price_read_timeout_seconds=max(
            int(os.getenv("PROVISOR_PRICE_READ_TIMEOUT_SECONDS", os.getenv("PROVISOR_PRICE_TOTAL_TIMEOUT_SECONDS", "120"))),
            int(os.getenv("PROVISOR_PRICE_TOTAL_TIMEOUT_SECONDS", "120")),
        ),
        provisor_price_connect_timeout_seconds=int(os.getenv("PROVISOR_PRICE_CONNECT_TIMEOUT_SECONDS", "10")),
        provisor_goods_connect_timeout_seconds=max(0.1, float(os.getenv("PROVISOR_GOODS_CONNECT_TIMEOUT_SECONDS", "5"))),
        provisor_goods_read_timeout_seconds=max(0.1, float(os.getenv("PROVISOR_GOODS_READ_TIMEOUT_SECONDS", "12"))),
        provisor_goods_total_timeout_seconds=max(0.1, float(os.getenv("PROVISOR_GOODS_TOTAL_TIMEOUT_SECONDS", "15"))),
        provisor_goods_search_max_results=max(1, min(100, int(os.getenv("PROVISOR_GOODS_SEARCH_MAX_RESULTS", "100")))),
        provisor_auto_refresh_enabled=env_bool("PROVISOR_AUTO_REFRESH_ENABLED", False),
        provisor_auto_refresh_cron=os.getenv("PROVISOR_AUTO_REFRESH_CRON", "0 2 * * *"),
        provisor_auto_refresh_mode=os.getenv("PROVISOR_AUTO_REFRESH_MODE", "selected").strip().lower() or "selected",
        provisor_auto_refresh_max_parallel_accounts=max(1, int(os.getenv("PROVISOR_AUTO_REFRESH_MAX_PARALLEL_ACCOUNTS", "2"))),
        provisor_auto_refresh_max_parallel_plk=max(1, int(os.getenv("PROVISOR_AUTO_REFRESH_MAX_PARALLEL_PLK", "1"))),
        provisor_auto_refresh_keep_last_success=env_bool("PROVISOR_AUTO_REFRESH_KEEP_LAST_SUCCESS", True),
        provisor_auto_refresh_timezone=os.getenv("PROVISOR_AUTO_REFRESH_TIMEZONE", os.getenv("TZ", "Asia/Qyzylorda")).strip() or "Asia/Qyzylorda",
        emit_worker_enabled=env_bool("EMIT_WORKER_ENABLED", False),
        emit_filial_ids=env_int_list("EMIT_FILIAL_IDS", "1106,1107,1108,1111,1114,1149,8371"),
        emit_temp_dir=os.getenv("EMIT_TEMP_DIR", "/tmp/emit"),
        emit_download_timeout_seconds=max(1, int(os.getenv("EMIT_DOWNLOAD_TIMEOUT_SECONDS", "7200"))),
        emit_max_file_size_gb=max(0.1, float(os.getenv("EMIT_MAX_FILE_SIZE_GB", "10"))),
        emit_batch_insert_size=max(1, int(os.getenv("EMIT_BATCH_INSERT_SIZE", "5000"))),
        emit_delete_temp_after_success=env_bool("EMIT_DELETE_TEMP_AFTER_SUCCESS", True),
        emit_cleanup_temp_hours=max(1, int(os.getenv("EMIT_CLEANUP_TEMP_HOURS", "24"))),
        emit_min_free_disk_gb=max(0.0, float(os.getenv("EMIT_MIN_FREE_DISK_GB", "15"))),
        emit_max_concurrent_filials=max(1, int(os.getenv("EMIT_MAX_CONCURRENT_FILIALS", "1"))),
        emit_min_final_rows=max(1, int(os.getenv("EMIT_MIN_FINAL_ROWS", "100"))),
        emit_min_row_ratio=max(0.0, float(os.getenv("EMIT_MIN_ROW_RATIO", "0.5"))),
        emit_refresh_stale_timeout_seconds=max(
            1,
            int(os.getenv("EMIT_STALE_JOB_TIMEOUT_SECONDS", os.getenv("EMIT_REFRESH_STALE_TIMEOUT_SECONDS", "14400"))),
        ),
        emit_heartbeat_interval_seconds=max(1, int(os.getenv("EMIT_HEARTBEAT_INTERVAL_SECONDS", "30"))),
        emit_max_memory_mb=max(0, int(os.getenv("EMIT_MAX_MEMORY_MB", "0"))),
        emit_cron=os.getenv("EMIT_CRON", "0 3 * * *"),
        emit_timezone=os.getenv("EMIT_TIMEZONE", os.getenv("TZ", "Asia/Qyzylorda")).strip() or "Asia/Qyzylorda",
        parser_worker_poll_interval_seconds=max(0.5, float(os.getenv("PARSER_WORKER_POLL_INTERVAL_SECONDS", "5"))),
        parser_worker_concurrency=max(1, int(os.getenv("PARSER_WORKER_CONCURRENCY", "1"))),
        parser_worker_stale_heartbeat_seconds=max(30, int(os.getenv("PARSER_WORKER_STALE_HEARTBEAT_SECONDS", "180"))),
        parser_daily_schedule_enabled=env_bool("PARSER_DAILY_SCHEDULE_ENABLED", False),
        parser_daily_schedule_cron=os.getenv("PARSER_DAILY_SCHEDULE_CRON", "0 2 * * *").strip() or "0 2 * * *",
        parser_daily_catchup_max_hours=max(0.0, float(os.getenv("PARSER_DAILY_CATCHUP_MAX_HOURS", "6"))),
        parser_daily_schedule_until_date=parser_daily_schedule_until_date,
        parser_daily_baseline_window=max(1, int(os.getenv("PARSER_DAILY_BASELINE_WINDOW", "7"))),
        parser_daily_baseline_min_samples=max(1, int(os.getenv("PARSER_DAILY_BASELINE_MIN_SAMPLES", "3"))),
        parser_daily_baseline_ratio=max(0.0, min(1.0, float(os.getenv("PARSER_DAILY_BASELINE_RATIO", "0.65")))),
        vidman_base_url=os.getenv("VIDMAN_BASE_URL", "https://1.provizor.kz"),
        vidman_login_path=os.getenv("VIDMAN_LOGIN_PATH", "/pages/login"),
        vidman_price_base_url=os.getenv("VIDMAN_PRICE_BASE_URL", "https://prv.kz"),
        price_source_secret=os.getenv("PRICE_SOURCE_SECRET"),
        redis_url=os.getenv("REDIS_URL"),
        sap_balance_enabled=env_bool("SAP_BALANCE_ENABLED", False),
        sap_balance_schedule_enabled=env_bool("SAP_BALANCE_SCHEDULE_ENABLED", False),
        sap_balance_base_url=os.getenv(
            "SAP_BALANCE_BASE_URL", "https://sap-piq.inkar.kz:53443/RESTAdapter/getBalance"
        ).strip(),
        sap_balance_username=os.getenv("SAP_BALANCE_USERNAME"),
        sap_balance_password=os.getenv("SAP_BALANCE_PASSWORD"),
        sap_balance_company_code=os.getenv("SAP_BALANCE_COMPANY_CODE"),
        sap_balance_cron=os.getenv("SAP_BALANCE_CRON", "0 4 * * *").strip() or "0 4 * * *",
        sap_balance_timeout_seconds=max(1, int(os.getenv("SAP_BALANCE_TIMEOUT_SECONDS", "120"))),
        sap_balance_branch_map_json=os.getenv("SAP_BALANCE_BRANCH_MAP_JSON", "{}").strip() or "{}",
        sap_manual_precedence_hours=max(0, int(os.getenv("SAP_MANUAL_PRECEDENCE_HOURS", "24"))),
    )

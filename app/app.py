import os
import time
import threading
import logging
import psycopg2
from psycopg2 import pool
import hvac
from flask import Flask, jsonify

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Global state — shared across all request threads
# ─────────────────────────────────────────────────────────────────────────────
_connection_pool = None
_pool_lock = threading.Lock()
_current_password = None
_vault_client = None
_secret_version = None
_request_counter = 0
_failure_counter = 0
_rotation_events = []

# ─────────────────────────────────────────────────────────────────────────────
# Vault interaction
# ─────────────────────────────────────────────────────────────────────────────

def get_vault_client():
    global _vault_client
    if _vault_client is None:
        vault_addr = os.environ.get('VAULT_ADDR', 'http://vault:8200')
        vault_token = os.environ.get('VAULT_TOKEN', 'root')
        _vault_client = hvac.Client(url=vault_addr, token=vault_token)
        if not _vault_client.is_authenticated():
            raise Exception("Vault authentication failed")
        logger.info(f"[VAULT] Connected to Vault at {vault_addr}")
    return _vault_client

def fetch_secret_from_vault():
    """
    Retrieves the current database password from Vault.
    Returns (password, version) tuple.
    This function is called at startup and whenever an auth failure is detected.
    """
    client = get_vault_client()
    secret_path = os.environ.get('VAULT_SECRET_PATH', 'secret/db')
    path = secret_path.replace('secret/', '').replace('data/', '')

    response = client.secrets.kv.v2.read_secret_version(
        path=path,
        mount_point='secret'
    )

    password = response['data']['data']['password']
    version = response['data']['metadata']['version']
    logger.info(f"[VAULT] Fetched secret version {version} from path: {secret_path}")
    return password, version

# ─────────────────────────────────────────────────────────────────────────────
# Connection pool management
# ─────────────────────────────────────────────────────────────────────────────

def build_connection_pool(password):
    """
    Creates a new psycopg2 ThreadedConnectionPool using the provided password.
    Pool size is configurable via environment variable.
    """
    pool_size = int(os.environ.get('POOL_SIZE', 5))
    return pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=pool_size,
        host=os.environ['DB_HOST'],
        port=int(os.environ.get('DB_PORT', 5432)),
        dbname=os.environ['DB_NAME'],
        user=os.environ['DB_USER'],
        password=password,
        connect_timeout=3
    )

def initialize_pool():
    """
    Startup function. Fetches credentials from Vault and creates the initial
    connection pool. Called once when the application starts.
    """
    global _connection_pool, _current_password, _secret_version

    logger.info("[STARTUP] Initializing application...")
    logger.info("[STARTUP] Fetching initial credentials from Vault...")

    password, version = fetch_secret_from_vault()
    _current_password = password
    _secret_version = version

    logger.info("[STARTUP] Building initial connection pool...")
    _connection_pool = build_connection_pool(password)

    logger.info(f"[STARTUP] Application ready. Vault secret version: {version}")

def rotate_connection_pool(new_password, new_version, trigger):
    """
    The core zero-downtime rotation function.

    This function implements the graceful pool replacement pattern:
    1. Build a new connection pool with the new password BEFORE closing the old one
    2. Under a lock, atomically swap the global pool reference
    3. Close the old pool AFTER the swap — in-flight requests on old connections
       complete normally, new requests use the new pool

    The lock ensures no request thread sees a None pool during the swap.
    The build-before-close ordering ensures the new pool is ready before
    any request could be routed to it.
    """
    global _connection_pool, _current_password, _secret_version, _rotation_events

    logger.info(f"[ROTATION] Starting pool rotation. Trigger: {trigger}")
    logger.info(f"[ROTATION] Building new connection pool with updated credentials...")

    rotation_start = time.time()

    try:
        new_pool = build_connection_pool(new_password)

        with _pool_lock:
            old_pool = _connection_pool
            _connection_pool = new_pool
            _current_password = new_password
            _secret_version = new_version

        rotation_duration = round((time.time() - rotation_start) * 1000, 2)

        logger.info(f"[ROTATION] Pool swap complete in {rotation_duration}ms")
        logger.info(f"[ROTATION] Now using Vault secret version: {new_version}")

        if old_pool:
            try:
                old_pool.closeall()
                logger.info("[ROTATION] Old connection pool closed gracefully")
            except Exception as e:
                logger.warning(f"[ROTATION] Error closing old pool (non-fatal): {e}")

        event = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trigger": trigger,
            "new_version": new_version,
            "rotation_duration_ms": rotation_duration,
            "requests_at_rotation": _request_counter
        }
        _rotation_events.append(event)

        logger.info(f"[ROTATION] Rotation complete. Zero downtime achieved.")
        return True

    except Exception as e:
        logger.error(f"[ROTATION] Pool rotation failed: {e}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
# Background secret watcher
# ─────────────────────────────────────────────────────────────────────────────

def secret_watcher():
    """
    Background thread that polls Vault every SECRET_REFRESH_INTERVAL seconds.
    When it detects a new secret version, it triggers a proactive pool rotation
    BEFORE any request fails.

    This is the proactive rotation path — the application detects the change
    and rotates without waiting for a connection failure.
    """
    interval = int(os.environ.get('SECRET_REFRESH_INTERVAL', 30))
    logger.info(f"[WATCHER] Secret watcher started. Polling every {interval}s")

    while True:
        time.sleep(interval)
        try:
            _, new_version = fetch_secret_from_vault()

            if new_version != _secret_version:
                logger.info(
                    f"[WATCHER] Secret version changed: {_secret_version} → {new_version}"
                )
                new_password, confirmed_version = fetch_secret_from_vault()
                rotate_connection_pool(new_password, confirmed_version, "watcher_detected_version_change")
            else:
                logger.debug(f"[WATCHER] Secret unchanged at version {new_version}")

        except Exception as e:
            logger.error(f"[WATCHER] Error checking secret version: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Database query execution with retry-on-auth-failure
# ─────────────────────────────────────────────────────────────────────────────

def execute_with_retry(query, max_retries=2):
    """
    Executes a database query with automatic credential rotation on auth failure.

    This is the reactive rotation path — a fallback for cases where the
    background watcher has not yet detected the rotation, or where the
    rotation happened in the brief interval between watcher polls.

    Flow:
    1. Get a connection from the current pool
    2. Execute the query
    3. If psycopg2 raises OperationalError with auth failure text:
       a. Fetch fresh credentials from Vault
       b. Rotate the connection pool
       c. Retry the query with the new connection
    4. Return the result — caller never knows a retry happened

    This is transparent to the HTTP request handler and therefore transparent
    to the API consumer. From the outside, the request takes slightly longer
    during a reactive retry — typically 200-500ms — but returns 200 OK.
    """
    global _request_counter, _failure_counter

    _request_counter += 1

    for attempt in range(max_retries + 1):
        conn = None
        try:
            with _pool_lock:
                current_pool = _connection_pool

            conn = current_pool.getconn()
            cursor = conn.cursor()
            cursor.execute(query)
            result = cursor.fetchone()
            cursor.close()
            current_pool.putconn(conn)
            return result, attempt

        except psycopg2.OperationalError as e:
            error_msg = str(e).lower()

            if conn:
                try:
                    with _pool_lock:
                        _connection_pool.putconn(conn, close=True)
                except Exception:
                    pass

            is_auth_failure = any(phrase in error_msg for phrase in [
                'password authentication failed',
                'authentication failed',
                'invalid password',
                'connection refused',
                'could not connect'
            ])

            if is_auth_failure and attempt < max_retries:
                _failure_counter += 1
                logger.warning(
                    f"[RETRY] Auth failure on attempt {attempt + 1}. "
                    f"Fetching fresh credentials from Vault..."
                )
                try:
                    new_password, new_version = fetch_secret_from_vault()
                    rotate_connection_pool(new_password, new_version, "reactive_auth_failure_retry")
                    logger.info(f"[RETRY] Pool rotated. Retrying query (attempt {attempt + 2})...")
                    time.sleep(0.1)
                    continue
                except Exception as vault_err:
                    logger.error(f"[RETRY] Vault fetch failed during retry: {vault_err}")
                    raise

            raise

        except Exception as e:
            if conn:
                try:
                    with _pool_lock:
                        _connection_pool.putconn(conn, close=True)
                except Exception:
                    pass
            raise

# ─────────────────────────────────────────────────────────────────────────────
# API endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({
        "status": "ok",
        "vault_secret_version": _secret_version,
        "total_requests": _request_counter,
        "auth_failure_retries": _failure_counter,
        "rotation_events": len(_rotation_events)
    }), 200

@app.route('/db-check')
def db_check():
    """
    Primary endpoint used by the traffic generator.
    Every request hits the database and returns connection metadata.
    This endpoint must return 200 throughout the rotation sequence.
    """
    start_time = time.time()
    try:
        result, retries_used = execute_with_retry(
            "SELECT version(), current_database(), now(), pg_postmaster_start_time()"
        )

        elapsed = round((time.time() - start_time) * 1000, 2)

        return jsonify({
            "status": "ok",
            "postgres_version": result[0].split(',')[0],
            "database": result[1],
            "server_time": str(result[2]),
            "db_uptime_since": str(result[3]),
            "vault_secret_version": _secret_version,
            "retries_used": retries_used,
            "response_time_ms": elapsed
        }), 200

    except Exception as e:
        elapsed = round((time.time() - start_time) * 1000, 2)
        logger.error(f"[REQUEST] db-check failed: {e}")
        return jsonify({
            "status": "error",
            "error": str(e),
            "vault_secret_version": _secret_version,
            "response_time_ms": elapsed
        }), 500

@app.route('/rotation-log')
def rotation_log():
    """
    Returns the history of all rotation events that occurred during this
    application's lifetime. Used to verify rotation happened and confirm
    timing relative to the traffic generator output.
    """
    return jsonify({
        "total_rotation_events": len(_rotation_events),
        "total_requests_served": _request_counter,
        "total_auth_failure_retries": _failure_counter,
        "current_vault_secret_version": _secret_version,
        "rotation_history": _rotation_events
    }), 200

@app.route('/secret-version')
def secret_version():
    """
    Returns current secret version without exposing the actual credential.
    Used during the lab to verify which version of the secret the app is using.
    """
    return jsonify({
        "current_version": _secret_version,
        "total_requests": _request_counter,
        "auth_failure_retries": _failure_counter
    }), 200

@app.route('/simulate-reactive-rotation')
def simulate_reactive_rotation():
    """
    Forces the reactive rotation path for demonstration purposes.
    Simulates what happens when a request fails due to auth error and
    the application recovers transparently.
    """
    try:
        new_password, new_version = fetch_secret_from_vault()
        if new_version == _secret_version:
            return jsonify({
                "message": "No new secret version available in Vault",
                "current_version": _secret_version,
                "hint": "Use vault kv put secret/db to write a new secret version first"
            }), 200

        success = rotate_connection_pool(new_password, new_version, "manual_simulation")
        return jsonify({
            "status": "rotated" if success else "failed",
            "new_version": new_version,
            "rotation_history": _rotation_events
        }), 200 if success else 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# Application startup
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    initialize_pool()

    watcher_thread = threading.Thread(target=secret_watcher, daemon=True)
    watcher_thread.start()

    logger.info("[STARTUP] Background secret watcher thread started")
    logger.info("[STARTUP] Flask server starting on port 8080")

    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)

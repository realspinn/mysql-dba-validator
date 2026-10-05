# MySQL DBA Validator — Future Connection Architecture Design

**Status:** Design Only — Implementation Ready  
**Date:** 2026-08-22  
**Baseline:** V2.2B (158 tests passed, P0-P1 hardening complete)  
**Scope:** Multi-user, multi-database connection model for open-source deployment  

---

## 1. CURRENT ARCHITECTURE

### 1.1 Configuration Flow

```
Application Startup
    ↓
load_dotenv()                          [backend/main.py:5]
    ↓
Environment variables
    MYSQL_HOST
    MYSQL_PORT
    MYSQL_USER
    MYSQL_PASSWORD
    MYSQL_DATABASE
    ↓
DBConfig.from_env()                    [backend/db.py:49-61]
    ↓
Global singleton: _client = DBClient(config)
    ↓
All endpoints share _client
    ↓
HTTP Request
    ├── /api/validate(sql) → uses _client.config.database
    ├── /api/evidence(sql) → uses _client.config.database
    └── /health → no DB required
```

### 1.2 Current DBConfig

```python
@dataclass
class DBConfig:
    host: Optional[str] = None
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None
    database: Optional[str] = None

# Property
@property
def configured(self) -> bool:
    return bool(self.host and self.user and self.database)
```

**Key behaviors:**
- All fields loaded from environment variables
- No hardcoded defaults except port=3306 (applied at connection time)
- If `configured` returns False, no network operations attempted
- All environment strings stripped of whitespace

### 1.3 Current DBClient

```python
# Global singleton
_client: Optional[DBClient] = None

def get_client() -> DBClient:
    global _client
    if _client is None:
        _client = DBClient()
    return _client
```

**Key behaviors:**
- Process-wide singleton shared by all requests
- Single config for entire application lifetime
- Cannot change database without restarting application
- Per-operation connections are fresh (not pooled)

### 1.4 Current Connection Lifecycle

```
HTTP Request → /api/validate
    ↓
analyze_batch(req.sql)
    ├── parse_sql()                    → no DB needed
    ├── analyze_batch()                → no DB needed
    ├── score_statement()              → no DB needed [V1 static]
    └── DBClient.execute_readonly()    → EXPLAIN/metadata phase
        ├── _connect()
        │   └── pymysql.connect(host, user, password, database=REQUIRED)
        ├── validate_readonly_select(sql)
        ├── cursor.execute()
        └── finally: connection.close()
```

**Critical fact:** `pymysql.connect()` requires `database` parameter. You cannot establish a connection and then select a database later.

### 1.5 EXPLAIN Operations (db_evidence.py)

```python
def collect_statement_evidence(sql, connection=None):
    if connection is None:
        connection = get_connection()  # Creates new connection with config.database
    
    # Constructs: "EXPLAIN SELECT ..."
    explain_sql = f"EXPLAIN {cleaned};"
    cursor.execute(explain_sql)        # Runs against config.database
    
    # Retrieves database name: SELECT DATABASE()
    database_name = cursor.execute("SELECT DATABASE() AS db_name")
    
    return evidence_with_database_context
```

**Key behaviors:**
- EXPLAIN runs against database from global config
- Database name retrieved post-execution, not pre-configured
- No database-specific EXPLAIN syntax handling (simple prefix approach)
- Fails gracefully if database is unavailable

### 1.6 Metadata Operations (metadata.py)

```python
def collect_metadata(statement_facts, connection=None, database=None):
    # Database MUST be specified
    if database is None:
        database = get_client().config.database
        if not database:
            return MetadataEvidence(status="unavailable")
    
    if connection is None:
        connection = get_client().connect_readonly()
    
    # Runs: SELECT ... FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s
    # Parameterized: database name is SQL parameter, not constructed
    cursor.execute(TABLES_SQL, (database, table_name, ...))
```

**Key behaviors:**
- Metadata collection **requires** database name
- Database name is parameterized in WHERE clause (SQL injection safe)
- Connection is bound to global config database
- If database None in config and not provided, returns unavailable
- Metadata collection supports injection of custom connection and database

### 1.7 Current API Contracts

| Endpoint | Request | Response | DB Dependency |
|----------|---------|----------|---|
| `POST /api/validate` | `ValidateRequest(sql)` | `ValidationResponse(...)` | Requires config.database for EXPLAIN/metadata |
| `POST /api/evidence` | `EvidenceRequest(sql)` | `EvidenceResponse(...)` | Requires config.database |
| `GET /health` | None | `{status: "ok"}` | None |

**No per-request configuration override mechanism exists.**

### 1.8 Test Architecture

Tests mock DB operations at multiple levels:
- Mock `get_client()` → replace entire function
- Mock `DBClient._connect()` → replace connection logic
- Mock `get_connection()` → replace lease wrapper
- Monkeypatch environment variables → control config loading
- Fake pymysql module → inject into sys.modules

Tests assume:
- Global singleton DBClient for entire app
- Fresh connection per operation (not pooled)
- No per-request configuration
- Environment variables are authoritative

---

## 2. CURRENT DB CONFIGURATION FLOW

```
START: Application initialization
    ↓
[main.py:5] load_dotenv()
    → Loads .env file (if exists)
    → Sets process environment variables
    ↓
[db.py:196-200] First call to get_client()
    → Creates global DBConfig via from_env()
    → Reads: MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE
    → Stores in global _client singleton
    → DBClient ready
    ↓
[main.py:X] First HTTP request
    → DBClient.config used for all DB operations
    → Same config for all subsequent requests
    → Application lifetime: config is static
    ↓
CONSTRAINT: To use different database or credentials
    → Must restart application
    → Must modify .env file
    → No runtime configuration possible
```

**Current limitations:**
- Single server/credentials only
- Single database only
- No per-request override
- No credential rotation without restart
- No multi-tenant capability

---

## 3. CURRENT CONNECTION LIFECYCLE

### Per-Operation Flow (Current State)

```
Operation (EXPLAIN, metadata, or health check)
    ↓
IF not configured:
    → Return unavailable/error
    ↓
IF configured:
    → _connect()
        ├── Close stale _conn if present
        ├── import pymysql (lazy)
        ├── pymysql.connect(
        │       host=config.host,
        │       user=config.user,
        │       password=config.password,
        │       database=config.database,  ← REQUIRED
        │       port=config.port or 3306,
        │       connect_timeout=5,
        │       read_timeout=10.0
        │   )
        ├── Store in self._conn
        └── Return connection
    ↓
EXECUTE operation
    (EXPLAIN, metadata query, etc.)
    ↓
CLOSE connection
    (in finally block)
    ↓
End operation
```

### Key Properties

- **Scope:** Per-operation (not pooled, not persistent)
- **Ownership:** Caller-managed via `_ConnectionLease` wrapper
- **Database binding:** Happens at pymysql.connect() time
- **Credentials:** Stored in memory in DBConfig for lifetime of process
- **Timeout:** connect_timeout=5, read_timeout=configurable (default 10)
- **Cursor type:** DictCursor (returns dicts, not tuples)

### Thread Safety

- Global `_client` singleton shared across threads
- Each thread/request gets independent connection from _connect()
- Stale connection check (`self._conn` close) handles cleanup
- No connection pooling → concurrent requests don't starve

---

## 4. PROPOSED CONNECTION ABSTRACTION

### 4.1 Conceptual Layers

```
┌─────────────────────────────────────────┐
│  API Endpoints (Frozen behavior)        │
│  /api/validate, /api/evidence           │
└──────────────┬──────────────────────────┘
               ↓
┌─────────────────────────────────────────┐
│  Future: Session Layer (NEW)            │
│  - Holds user-supplied credentials      │
│  - Manages logical database selection   │
│  - NOT a persistent connection pool     │
└──────────────┬──────────────────────────┘
               ↓
┌─────────────────────────────────────────┐
│  Connection Scope (NEW ABSTRACTION)     │
│  - host, port, user, password           │
│  - Short-lived, per-operation creation  │
│  - Parameterized database selection     │
└──────────────┬──────────────────────────┘
               ↓
┌─────────────────────────────────────────┐
│  Analysis Scope (EXISTING)              │
│  - Selected database name               │
│  - Passed to EXPLAIN, metadata          │
│  - Parameterized in SQL queries         │
└──────────────┬──────────────────────────┘
               ↓
┌─────────────────────────────────────────┐
│  Score Pipeline (FROZEN)                │
│  V1 → EXPLAIN → Phase2 → Metadata →     │
│  M1 → M2 → Response                     │
└─────────────────────────────────────────┘
```

### 4.2 Core Principle

**Connection Scope ≠ Analysis Scope**

```
Connection Scope:
    host, port, username, password
    → Can be established once per user/session
    → Doesn't require selecting a database upfront

Analysis Scope:
    database_name
    → Selected AFTER connection established
    → Can change per API request
    → Passed as parameterized SQL value
```

### 4.3 Proposed ConnectionProfile (Logical Container)

```python
@dataclass
class ConnectionProfile:
    """User-supplied credentials and server endpoint."""
    host: str
    port: int = 3306
    username: str
    password: str
    
    # Optional
    connect_timeout: int = 5
    read_timeout: int = 10
    
    def display_name(self) -> str:
        """For UI: 'host:port' without credentials."""
        return f"{self.host}:{self.port}"
    
    def to_pymysql_params(self) -> dict:
        """Generate pymysql.connect() kwargs (without database)."""
        return {
            "host": self.host,
            "port": self.port,
            "user": self.username,
            "password": self.password,
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "cursorclass": pymysql.cursors.DictCursor,
        }
```

### 4.4 Proposed AnalysisSession (Logical Context)

```python
@dataclass
class AnalysisSession:
    """Binds a connection profile with a selected database."""
    connection_profile: ConnectionProfile
    database: str  # User-selected database name
    
    # Do NOT store persistent MySQL connection
    # Create fresh connection per operation
    
    def validate_database_identifier(self) -> bool:
        """Check database name is valid MySQL identifier."""
        # alphanumeric, underscore, dollar, max 64 chars
        return bool(VALID_IDENTIFIER_PATTERN.match(self.database))
    
    def get_connection(self, read_timeout: Optional[float] = None) -> Connection:
        """
        Create fresh connection for this session's database.
        Connection must be closed by caller (or use context manager).
        """
        params = self.connection_profile.to_pymysql_params()
        if read_timeout is not None:
            params["read_timeout"] = read_timeout
        params["database"] = self.database  # Add database selection
        return pymysql.connect(**params)
```

### 4.5 Backward Compatibility: Legacy .env Mode

```python
class EnvironmentSession(AnalysisSession):
    """Backward-compatible: loads from .env like current implementation."""
    
    @classmethod
    def from_env(cls) -> Optional['EnvironmentSession']:
        """
        Load from environment variables (current behavior).
        Returns None if incomplete configuration.
        """
        config = DBConfig.from_env()
        if not config.configured:
            return None
        
        profile = ConnectionProfile(
            host=config.host,
            port=config.port,
            username=config.user,
            password=config.password,
        )
        return cls(
            connection_profile=profile,
            database=config.database
        )
```

---

## 5. CONNECTION SCOPE VS ANALYSIS SCOPE

### Current Conflation (Problem)

```
Current:
    Connection ← database name is REQUIRED
    ↓
    EXPLAIN runs against hard-bound database
    ↓
    Metadata queries run against hard-bound database
    ↓
    To analyze different database: Restart app, change .env
```

### Proposed Separation (Solution)

```
Future:
    ConnectionProfile ← host, port, credentials only
        ↓
    AnalysisSession(profile + database_name)
        ├── EXPLAIN uses selected database
        ├── Metadata uses selected database
        ├── Can change database per request
        └── Same connection profile for multiple databases
        
    User Flow:
        1. Enter credentials once (ConnectionProfile)
        2. Get list of available databases
        3. Select one database (AnalysisSession)
        4. Validate SQL against selected database
        5. Switch to different database (new AnalysisSession, same ConnectionProfile)
```

### Technical Implementation (Future)

**Phase 0 (Current):**
```
.env → DBConfig → DBClient → Single database forever
```

**Phase 1 (Planned - Next milestone):**
```
.env → ConnectionProfile → AnalysisSession(.database) → Per-request database
```

**Phase 2 (Future):**
```
UI input → ConnectionProfile → Discover databases → AnalysisSession → SQL validation
```

---

## 6. DATABASE DISCOVERY DESIGN

### 6.1 Discovery Query

**Operation:** List all databases visible to authenticated account

```sql
SHOW DATABASES;
```

**Characteristics:**
- ✅ Read-only
- ✅ Returns only databases the account has ANY privilege on
- ✅ Accounts without privileges see empty result (not error)
- ✅ No special privilege requirement (not SUPER)
- ✅ Returns simple list: database_name

**Alternative (more structured):**
```sql
SELECT SCHEMA_NAME 
FROM information_schema.SCHEMATA 
ORDER BY SCHEMA_NAME;
```

Equivalent security model; more structured response.

### 6.2 System Database Handling

**Default MySQL system databases:**
- `information_schema` (metadata, available to everyone)
- `mysql` (system tables, requires specific privilege)
- `performance_schema` (diagnostics, available to some accounts)
- `sys` (helpers, available to some accounts)
- User databases (e.g., `production`, `staging`)

**Filtering strategy:**
- Show ALL returned databases to user
- Do NOT hide system databases in discovery response
- User may explicitly select `information_schema` for metadata analysis
- If user selects database they lack privileges for, error will surface at EXPLAIN/metadata time

**Rationale:** DBA users know their privilege model; hiding databases is confusing.

### 6.3 Database Enumeration Response

**Proposed API response:**

```json
{
  "connection": {
    "host": "db.example.com",
    "port": 3306,
    "status": "connected"
  },
  "databases": [
    "information_schema",
    "mysql",
    "performance_schema",
    "production",
    "staging",
    "analytics"
  ],
  "count": 6
}
```

**No credentials in response; no passwords; no raw error messages.**

### 6.4 Error Handling

**Case 1: Connection fails**
```json
{
  "status": "error",
  "message": "Could not connect to database server",
  "details": {
    "host": "db.example.com:3306",
    "connection_error": true
  }
}
```

**Case 2: Connection succeeds but discovery query fails**
```json
{
  "status": "warning",
  "message": "Connected but unable to list databases",
  "databases": [],
  "note": "Account may have limited privileges. You can still manually enter a database name."
}
```

**No driver error details; no SQL exception text; no host resolution failures revealed.**

### 6.5 Manual Database Entry

**Fallback mechanism:**
- If discovery fails but connection succeeds, allow user to manually enter database name
- Validation happens at first SQL operation (EXPLAIN/metadata)
- Error will surface clearly if database doesn't exist or no access

```
UI:
    Database Discovery: [FAILED]
    ↓
    Can't list databases?
    Enter manually: [________] [Validate]
    ↓
    User enters: "production"
    ↓
    POST /api/validate with database="production"
    ↓
    Error: "Unknown database 'production'" (from MySQL)
```

### 6.6 Database Identifier Validation

**Before sending to MySQL, validate identifier:**

```python
import re

# MySQL identifier: alphanumeric, underscore, dollar sign, max 64 chars
# Cannot start with digit (though MySQL allows some edge cases)
MYSQL_IDENTIFIER = re.compile(r"^[a-zA-Z_$][a-zA-Z0-9_$]{0,63}$")

def is_valid_database_name(db_name: str) -> bool:
    return MYSQL_IDENTIFIER.match(db_name) is not None
```

**Purpose:**
- Catch typos early (before network call)
- Prevent accidental SQL injection
- Reject obviously invalid names

**When to use:**
- User manually enters database name
- Validate before attempting connection or EXPLAIN
- Return clear error if invalid

### 6.7 Implementation Readiness (for future)

**Future endpoint (NOT implemented now):**

```
POST /api/connections/discover-databases

Request:
{
  "connection_profile": {
    "host": "db.example.com",
    "port": 3306,
    "username": "dba_user",
    "password": "secret"
  }
}

Response:
{
  "status": "success",
  "databases": ["information_schema", "production", ...],
  "connection_test": true
}
```

---

## 7. DATABASE SELECTION DESIGN

### 7.1 Selection Flow

```
User clicks: "Select database"
    ↓
User chooses from dropdown:
    - production
    - staging
    - analytics
    ↓
Application stores: AnalysisSession(profile, database="production")
    ↓
User submits SQL for validation
    ↓
/api/validate(sql, database="production")
    ↓
Validation pipeline uses selected database
    └─→ EXPLAIN runs against "production"
    └─→ Metadata queries run against "production"
```

### 7.2 Database Switching

**Key property:** Switching databases should NOT leak state

```
Scenario:
    1. User validates: SELECT * FROM customers → runs against "production"
    2. Database discovery cached: [information_schema, mysql, production, staging]
    3. User switches: /api/validate(sql, database="staging")
    4. New EXPLAIN runs against "staging"
    5. Evidence/metadata from "production" are NOT reused

Requirement:
    - Fresh EXPLAIN for new database
    - No EXPLAIN result reuse across database switches
    - No metadata cache leakage
```

**Implementation:** Current architecture naturally safe because:
- No persistent metadata cache exists in current code
- Each EXPLAIN operation creates fresh connection
- Each metadata operation is independent
- No session-level cache to invalidate

### 7.3 API Contract (Future, Not Implemented)

**Current (does not accept database parameter):**
```
POST /api/validate
{
  "sql": "SELECT * FROM customers WHERE id = 1"
}
```

**Proposed future (ADDITIVE, backward compatible):**
```
POST /api/validate
{
  "sql": "SELECT * FROM customers WHERE id = 1",
  "database": "production"  ← Optional, additive
}
```

**Backward compatibility:**
- If `database` not provided, use AnalysisSession.database (from UI state)
- If neither provided, use .env fallback (legacy mode)
- Current callers (without database param) continue to work

### 7.4 Selected Database Visibility

**What the user sees:**

```
┌─────────────────────────────────┐
│  MySQL DBA Validator            │
├─────────────────────────────────┤
│ Connection: db.example.com:3306 │
│ Database: [production]  [switch] │
├─────────────────────────────────┤
│ SQL Validator                   │
│ [paste SQL here]                │
│ [Validate]                      │
└─────────────────────────────────┘
```

**What the user does NOT see:**
- Username/password (entered once, not redisplayed)
- Raw EXPLAIN output (summarized into findings)
- information_schema query text
- Error details from MySQL driver

---

## 8. SECURITY MODEL

### 8.1 Self-Hosted Open-Source Mode

**Assumption:** User runs application locally or on trusted infrastructure.

**Credential handling:**
- ✅ Accept credentials via HTTP POST (TLS required in production)
- ✅ Store credentials in memory only (not disk, not logs)
- ✅ Credentials exist only for active session
- ✅ Each user session has independent credentials
- ✅ Credentials cleared when user logs out or session expires

**Plaintext password lifetime:**
- From: User types password in browser
- To: Application converts to pymysql.connect() kwargs
- After: Password remains in connection object only
- When closed: Memory freed by Python GC

**Credential storage:**
- .env file: Backward compatibility only (developer/self-hosted mode)
- Per-session: Credentials provided via HTTP POST
- Never: Credentials stored in database, logs, or config files

### 8.2 Hosted Public/SaaS Mode (Future Reference, Not Implemented)

**Would require substantially different security model:**
- TLS/SSL certificate pinning for MySQL connections
- Credential encryption-at-rest in application database (if persistent)
- Audit logging of all SQL operations
- Multi-tenant isolation (credentials belong to tenant, not shared)
- Database user-level RBAC per tenant
- SSRF protection if application connects to user-supplied MySQL hosts
- Rate limiting per tenant
- Connection attempt logging/alerting
- Credential rotation mechanisms
- Data residency compliance

**Current application is SELF-HOSTED ONLY.**

---

## 9. LEAST-PRIVILEGE MYSQL MODEL

### 9.1 Minimum Privileges Required

| Operation | Query | Privileges |
|-----------|-------|-----------|
| **Health check** | `SELECT 1` | None (connection success = health) |
| **Database discovery** | `SHOW DATABASES` | Implicit (returns only accessible DBs) |
| **EXPLAIN SELECT** | `EXPLAIN SELECT ...` | `SELECT` on target table |
| **EXPLAIN UPDATE** | `EXPLAIN UPDATE ...` | `SELECT` on target table |
| **EXPLAIN DELETE** | `EXPLAIN DELETE ...` | `SELECT` on target table |
| **Metadata: TABLES** | `SELECT ... FROM information_schema.TABLES` | `SELECT` on information_schema |
| **Metadata: STATISTICS** | `SELECT ... FROM information_schema.STATISTICS` | `SELECT` on information_schema |
| **Metadata: COLUMNS** | `SELECT ... FROM information_schema.COLUMNS` | `SELECT` on information_schema |

### 9.2 Privilege Grant Example

```sql
-- Create read-only DBA validator account
CREATE USER 'validator'@'%' IDENTIFIED BY 'secure_password';

-- Grant on all databases
GRANT SELECT ON *.* TO 'validator'@'%';

-- Grant on information_schema (usually redundant if *.* granted)
GRANT SELECT ON information_schema.* TO 'validator'@'%';

-- Flush privileges
FLUSH PRIVILEGES;
```

### 9.3 Explicitly NOT Required

- `INSERT`, `UPDATE`, `DELETE` — Application never writes
- `CREATE`, `ALTER`, `DROP` — Application never modifies schema
- `EXECUTE` — Application never calls stored procedures
- `SUPER` — Application never needs elevated privileges
- `FILE` — Application never reads/writes files
- `GRANT` — Application never modifies user privileges
- `SHOW PROCESSLIST`, `SHOW ENGINES` — Not required for core functionality

### 9.4 Privilege Validation

**Question:** How do we know if a connected account has sufficient privileges?

**Current approach:**
- No explicit validation
- EXPLAIN/metadata queries fail if privileges insufficient
- Error is caught and sanitized

**Proposed future approach:**
```
After successful connection to a database:
1. Attempt SHOW TABLES (minimal operation)
2. If succeeds → account has SELECT on database
3. If fails → account lacks privilege
4. Surface clear error: "Account lacks SELECT privilege on this database"
```

**This check is OPTIONAL for Phase 0 (next milestone). Mark as: FUTURE/OPTIONAL**

---

## 10. SELF-HOSTED VS SAAS COMPARISON

### 10.1 Self-Hosted Model (Current & Immediate Future)

```
┌──────────────────────────┐
│  User's browser          │
│  (localhost:8420)        │
└────────────┬─────────────┘
             ↓
┌──────────────────────────┐
│  FastAPI application     │
│  (same machine/network)  │
│  Credentials in memory   │
│  per HTTP session        │
└────────────┬─────────────┘
             ↓
┌──────────────────────────┐
│  User's MySQL server     │
│  (localhost or LAN)      │
│  Credentials: user's own │
└──────────────────────────┘
```

**Security properties:**
- ✅ Credentials never leave user's infrastructure
- ✅ No network SSRF risk (connecting to user's own server)
- ✅ No encryption needed (trusted network)
- ✅ No authentication needed for web UI (localhost)
- ✅ No audit logging required (user trusts themselves)
- ✅ No multi-tenant isolation needed

### 10.2 Hosted SaaS Model (Future Only; Not Implemented)

```
┌──────────────────────────┐
│  User's browser          │
│  (internet)              │
└────────────┬─────────────┘
             ↓ HTTPS
┌──────────────────────────┐
│  Your public server      │
│  (Hosted by you)         │
│  Stores encrypted        │
│  credentials per tenant  │
│  Connects to user's      │
│  MySQL (user's network)  │
└────────────┬─────────────┘
             ↓ ??? encrypted ???
┌──────────────────────────┐
│  User's MySQL server     │
│  (user's infrastructure) │
│  Accessed via internet   │
│  Credentials: hosted app │
└──────────────────────────┘
```

**Would require (not implemented):**
- TLS 1.2+ for all connections
- Credential encryption (AES-256 at rest)
- Per-tenant credential isolation
- Audit logging of all SQL
- User authentication (OAuth, SAML, etc.)
- Rate limiting
- SSRF protection (verify user's MySQL host is not internal)
- Data residency compliance
- Backup/recovery procedures
- DLP (Data Loss Prevention)

**Do NOT implement SaaS features in this milestone.**

---

## 11. API CONTRACT PROPOSAL

### 11.1 Existing Endpoints (Backward Compatible)

These MUST continue to work as-is:

```
POST /api/validate
  Request: { "sql": "..." }
  Response: { "score": N, "findings": [...], "evidence": {...} }

POST /api/evidence
  Request: { "sql": "..." }
  Response: { "evidence": {...} }

GET /health
  Response: { "status": "ok" }
```

### 11.2 Proposed New Endpoints (Future, Not Implemented)

**Phase 1: Connection Testing**

```
POST /api/connections/test
  Request: {
    "host": "db.example.com",
    "port": 3306,
    "username": "dba_user",
    "password": "secret"
  }
  Response: {
    "status": "success" | "error",
    "connected": true | false,
    "error_message": "if status=error"
  }
  
  Notes:
  - Password is NOT returned
  - Credentials not stored
  - Connection closed after test
```

**Phase 2: Database Discovery**

```
POST /api/connections/discover-databases
  Request: {
    "host": "db.example.com",
    "port": 3306,
    "username": "dba_user",
    "password": "secret"
  }
  Response: {
    "status": "success" | "error",
    "databases": ["information_schema", "mysql", "production", "staging"],
    "error_message": "if status=error"
  }
  
  Notes:
  - Password is NOT returned
  - Only databases the account can access
  - System databases included (user will filter in UI if needed)
  - Errors sanitized (no MySQL error codes)
```

**Phase 3: Extended /api/validate (Additive)**

```
POST /api/validate
  Request: {
    "sql": "...",
    "connection_profile": {               ← NEW, OPTIONAL
      "host": "...",
      "port": 3306,
      "username": "...",
      "password": "..."
    },
    "database": "production"               ← NEW, OPTIONAL
  }
  Response: { same as before }
  
  Backward compatibility:
  - If no connection_profile: use .env (legacy mode)
  - If connection_profile missing: return error
  - If database missing: use default from config or error
```

### 11.3 Authentication Assumptions

**Self-hosted mode (no application-level auth required):**
- Browser access is trusted (localhost or VPN)
- No login required
- Credentials passed per-request

**SaaS mode (future, would require):**
- OAuth/SAML login
- Session tokens
- User identification for audit logging
- Tenant isolation in API responses

**NOT IMPLEMENTED YET.**

### 11.4 Error Response Format

**Sanitized (no credentials, no driver details):**

```json
{
  "status": "error",
  "error_code": "invalid_database",
  "message": "Database 'typo_db' does not exist or no access",
  "request_id": "uuid-here"
}
```

**What we hide:**
- MySQL error codes (2003, 1049, 1045, etc.)
- Connection details (resolved IP, port, driver version)
- SQL exception text
- Stack traces
- Authentication details

---

## 12. FRONTEND UX PROPOSAL

**Current state:** Simple SQL text area, no DB management

**Proposed UX (Conceptual Only — Do NOT Modify frontend.py):**

### 12.1 Flow Diagram

```
┌────────────────────────────────────────┐
│  Connection Setup (NEW SCREEN)         │
│                                        │
│  MySQL Host:     [________________]    │
│  Port:           [3306]                │
│  Username:       [________________]    │
│  Password:       [________________]    │
│                  (masked dots)         │
│  [Test Connection]  [Clear]  [Save]   │
│                                        │
│  Status: (not connected)               │
└────────────────────────────────────────┘
                  ↓
        IF successful:
        Show available databases
┌────────────────────────────────────────┐
│  Database Selection (NEW SCREEN)       │
│                                        │
│  Available databases:                  │
│  ○ information_schema                  │
│  ○ mysql                               │
│  ○ performance_schema                  │
│  ○ production         ← Selected       │
│  ○ staging                             │
│  ○ analytics                           │
│                                        │
│  [Or enter manually: ___________]      │
│  [Validate Database]                   │
│  [Back to Connection Setup]            │
└────────────────────────────────────────┘
                  ↓
        Database selected
┌────────────────────────────────────────┐
│  SQL Validator (EXISTING SCREEN)       │
│  Connected: production@db.example.com  │
│                                        │
│  [Paste SQL here]                      │
│  [Validate]                            │
│                                        │
│  Results:                              │
│  ├── V1 Static: HIGH risk              │
│  ├── EXPLAIN: UPDATE scans 50K rows    │
│  ├── Metadata: No index on email       │
│  └── Confidence: HIGH                  │
│                                        │
│  [Switch Database] [New Connection]    │
└────────────────────────────────────────┘
```

### 12.2 Key UX Properties

**Password masking:**
- ✅ Always display as dots: `••••••••`
- ✅ Do NOT echo keystrokes
- ✅ Do NOT store in browser localStorage
- ✅ Do NOT include in URL query params

**Credential persistence:**
- ✅ Credentials exist only for current browser session
- ✅ Refresh page → prompt for credentials again
- ✅ Close browser → credentials cleared
- ✅ Do NOT write to localStorage
- ✅ Do NOT write to sessionStorage without encryption

**Database switching:**
- ✅ Show "Switch Database" button if already connected
- ✅ Allow quick switching between databases without re-entering credentials
- ✅ Show currently selected database in UI header

**Connection status:**
- ✅ Display: "Connected to: host:port" with database name
- ✅ Display: "Not connected" if credentials not entered
- ✅ Display: "Connection failed" with sanitized error if test failed

**Connection failure:**
- ✅ Show generic message: "Could not connect to database"
- ✅ Hide MySQL error codes
- ✅ Suggest: "Check host, port, and credentials"
- ✅ Provide: "Back" button to try again

**Discovery failure:**
- ✅ Show: "Connected but unable to list databases"
- ✅ Allow: "Enter database name manually"
- ✅ Don't panic: "This is okay if your account has limited privileges"

**Manually entering database name:**
- ✅ Show text input: "Database name: [____________]"
- ✅ Add validation feedback: "Valid MySQL identifier" or error
- ✅ Test on first SQL submission

**Credential display:**
- ✅ Do NOT display password after user enters it
- ✅ Do NOT show "Password: ••••" in UI
- ✅ Show "Connected as: dba_user" (username only, not password)

**Clear distinction: Static vs DB-assisted:**
- ✅ Show which evidence came from database (EXPLAIN, metadata)
- ✅ Show which evidence is static analysis (V1, parser-based)
- ✅ When no DB connection: "Validation using static analysis only. Connect for detailed evidence."

---

## 13. SESSION / ISOLATION MODEL

### 13.1 Logical Session Structure

```python
class UserSession:
    """Holds per-user state (not persistent MySQL connection)."""
    
    # Stored in-memory, cleared on logout/timeout
    session_id: str                      # UUID
    connection_profile: ConnectionProfile # Credentials
    selected_database: str               # User's choice
    created_at: datetime
    last_activity: datetime
    
    # NOT stored (to prevent leakage):
    _mysql_connection: None              # Never kept open
    _evidence_cache: None                # Never cached
    _metadata_cache: None                # Never cached
```

### 13.2 Isolation Guarantees

**Per-user isolation:**
```
User A (connected to 'production')
    → EXPLAIN against production
    → Metadata from production
    ↓
User B (connected to 'staging')
    → EXPLAIN against staging
    → Metadata from staging
    ↓
No leakage: User A's EXPLAIN results do not affect User B
No cache sharing: Each user gets fresh operations
```

**Per-request isolation:**
```
Request 1: Validate SQL against production
    → Fresh connection
    → Fresh EXPLAIN
    → Fresh metadata
    → Close connection
    ↓
Request 2: Validate different SQL against staging (same session)
    → Fresh connection (different database)
    → Fresh EXPLAIN
    → Fresh metadata (new session)
    → Close connection
    ↓
No state leakage between requests
```

### 13.3 Current Code is Naturally Safe

The current short-lived connection model is already safe:
- ✅ No persistent metadata cache
- ✅ No EXPLAIN result cache (recomputed per operation)
- ✅ No session-level state persistence
- ✅ Fresh connection per operation
- ✅ Connection closes immediately (in finally block)

**No architectural changes needed for isolation.**

### 13.4 Concurrent Sessions with Different Credentials

**Scenario:**
```
Browser Tab 1: User logs in with account A → connects to production
Browser Tab 2: User logs in with account B → connects to staging (different credentials)
```

**Implementation (future):**
- Session cookie tracks which credentials/database are active
- API checks session cookie and uses corresponding credentials
- Each tab has independent session
- Closing tab doesn't affect other tabs

**Current architecture:** Only supports one set of credentials (from .env). Future versions will support this naturally.

### 13.5 No Cross-User/Cross-Database State Leakage

**Risk:** EXPLAIN from Database A somehow contaminates metadata from Database B

**Current safeguards:**
- ✅ Metadata collection doesn't cache results
- ✅ Each metadata call creates fresh connection
- ✅ Database name is parameterized in WHERE clause (not from EXPLAIN)
- ✅ EXPLAIN and metadata are independent operations

**No changes needed.**

---

## 14. CONNECTION LIFECYCLE MODEL

### 14.1 Proposed Future Lifecycle

```
┌─────────────────────────────────────────────────┐
│ Startup: Load .env (if present)                 │
│ → Create EnvironmentSession (backward compat)   │
│ → Application ready for legacy requests         │
└─────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────┐
│ User opens browser: localhost:8420              │
│ → Shows "Connect to MySQL" form (NEW)           │
│ → User enters credentials (NOT stored on disk)  │
└─────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────┐
│ User clicks "Test Connection"                   │
│ → Create temporary ConnectionProfile            │
│ → Attempt pymysql.connect(host, port, user, pwd)│
│ → Check if connected (no database yet!)         │
│ → Close connection                              │
│ → Return "Connected: success"                   │
└─────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────┐
│ User clicks "Discover Databases"                │
│ → Create temporary ConnectionProfile            │
│ → Connect: pymysql.connect(NO database)         │
│ → Execute: SHOW DATABASES                       │
│ → Parse result: ["information_schema", ...]     │
│ → Close connection                              │
│ → Return list to UI                             │
└─────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────┐
│ User selects "production"                       │
│ → Create AnalysisSession(profile, "production") │
│ → Store in session (in-memory)                  │
│ → Show: "Connected: production@host:port"       │
└─────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────┐
│ User submits SQL: "SELECT * FROM customers"     │
│ → Validate SQL (no DB needed)                   │
│ → Create connection:                            │
│   pymysql.connect(host, port, user, pwd,        │
│                   database="production")        │
│ → Validate SELECT-only                          │
│ → Execute EXPLAIN                               │
│ → Close connection                              │
│ → Create new connection for metadata            │
│ → Query information_schema.TABLES                │
│   WHERE TABLE_SCHEMA = "production"             │
│ → Close connection                              │
│ → Compile results → return response             │
└─────────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────┐
│ User switches database: "staging"               │
│ → Update AnalysisSession(profile, "staging")    │
│ → Show: "Connected: staging@host:port"          │
│ → Next SQL operation uses "staging"             │
│ → No state leakage from "production"            │
└─────────────────────────────────────────────────┘
```

### 14.2 Key Lifecycle Properties

**What is stored (in-memory):**
- ConnectionProfile (credentials for duration of session)
- AnalysisSession (profile + selected database)
- Session ID (for tracking)

**What is NOT stored:**
- MySQL connections (short-lived, created per-operation)
- EXPLAIN results (not cached)
- Metadata (not cached)
- Evidence (computed fresh per request)

**Timeouts:**
- Connection timeout: 5 seconds (fail fast)
- Read timeout: 10 seconds (configurable per operation)
- Session timeout: 1 hour or browser close (future, not implemented)

**Cleanup:**
- After each operation: connection.close() in finally block
- Session deletion: User closes browser or timeout expires
- Credentials: Python GC clears memory when session is deleted

---

## 15. MIGRATION PLAN

### 15.1 Phase 0 (Current V2.2B)

✅ **Complete** 
- Environment variable configuration
- Global DBClient singleton
- Per-operation short-lived connections
- P0 read-only boundary enforcement
- P1 hardening complete
- 158 tests passing

### 15.2 Phase 1: Connection Abstraction (Next Milestone)

**Goal:** Prepare codebase for multi-database support (no API changes yet)

**Changes needed:**
- Extract `ConnectionProfile` class (connection scope only)
- Create `AnalysisSession` class (adds database selection)
- Refactor `DBClient._connect()` to accept database as parameter
- Refactor `collect_statement_evidence()` to accept optional database parameter
- Refactor `collect_metadata()` to support both config-database and per-call database
- Create backward-compatible `EnvironmentSession.from_env()`
- Add database identifier validation helper

**Preserved:**
- ✅ No scoring changes
- ✅ No API endpoint changes (internal only)
- ✅ No test changes (mock at same layers)
- ✅ Backward compatible (existing .env mode still works)

**Expected tests:**
- 158 → 165+ (new abstraction tests)
- All existing tests pass (no API contract change)

### 15.3 Phase 2: Connection Testing

**Goal:** Allow users to test connectivity without validation

**New:**
```
POST /api/connections/test
  ← NEW endpoint
  Request: {host, port, username, password}
  Response: {status, connected}
```

**Implementation:**
- Use ConnectionProfile directly (no database needed)
- Test pymysql.connect(host, port, user, pwd, database="test") with read-only database
- Catch and sanitize errors
- Return boolean result

**Preserved:**
- ✅ No changes to /api/validate
- ✅ /api/evidence unchanged
- ✅ Backward compatible

### 15.4 Phase 3: Database Discovery

**Goal:** Let users see available databases before selecting

**New:**
```
POST /api/connections/discover-databases
  ← NEW endpoint
  Request: {host, port, username, password}
  Response: {status, databases: [...]}
```

**Implementation:**
- Use ConnectionProfile
- Execute `SHOW DATABASES` or `SELECT SCHEMA_NAME FROM information_schema.SCHEMATA`
- Return list of database names
- Sanitize errors

**Preserved:**
- ✅ No changes to /api/validate
- ✅ No changes to /api/evidence
- ✅ Backward compatible

### 15.5 Phase 4: Database Selection (Client-Side)

**Goal:** Track selected database in UI state (no API change)

**Changes:**
- Frontend stores selected database in session storage (or memory)
- Frontend UI shows: "Connected: production@host:port"
- Frontend passes database to /api/validate requests (NEW)

**API Changes (Additive):**
```
POST /api/validate
  {
    "sql": "...",
    "database": "production"     ← NEW, optional
  }
```

**Implementation:**
- Accept database parameter in request
- If provided, use it
- If not provided, use AnalysisSession.database (from session state)
- If neither, use config.database (backward compat)

**Preserved:**
- ✅ Existing /api/validate calls still work (database optional)
- ✅ Backward compatible
- ✅ All frozen scoring code unchanged

### 15.6 Phase 5: Per-Request Connection Profile (User Input)

**Goal:** Accept credentials from user HTTP requests (not just .env)

**API Changes (Additive):**
```
POST /api/validate
  {
    "sql": "...",
    "connection_profile": {         ← NEW, optional
      "host": "...",
      "port": 3306,
      "username": "...",
      "password": "..."
    },
    "database": "production"
  }
```

**Implementation:**
- Extract ConnectionProfile from request (if provided)
- Create AnalysisSession(profile, database)
- Use for all DB operations
- Fall back to environment session if not provided

**Preserved:**
- ✅ Legacy .env mode still works
- ✅ Backward compatible for all existing callers
- ✅ Tests refactored but behavior preserved

### 15.7 Phase 6: Frontend Connection/Session UX

**Goal:** Beautiful connection/database selection UI

**New:**
- Connection Setup form (host, port, user, password)
- Database selection dropdown
- Connection status display
- Session tracking

**Preserved:**
- ✅ API contracts stable
- ✅ SQL validation logic unchanged
- ✅ Scoring unchanged

### 15.8 Phase Completion Criteria

| Phase | Tests | Baseline | Verdict | Notes |
|-------|-------|----------|---------|-------|
| 0 (Current) | 158 | ✅ PASS | V2.2B complete | Isolated venv verified |
| 1 (Abstraction) | ≥158 | ✅ PASS | Internal refactor | No API change |
| 2 (Test Connection) | ≥162 | ✅ PASS | New endpoint | No /api/validate change |
| 3 (Discovery) | ≥165 | ✅ PASS | New endpoint | No /api/validate change |
| 4 (Database Select) | ≥168 | ✅ PASS | Additive param | Backward compatible |
| 5 (Cred Input) | ≥171 | ✅ PASS | Additive param | Legacy .env works |
| 6 (UI/UX) | ≥171 | ✅ PASS | Frontend only | No scoring change |

**Every phase is backward compatible with previous phases.**

---

## 16. TEST STRATEGY

### 16.1 Unit Tests (For Future Implementation)

```python
# Phase 1: Connection Abstraction

def test_connection_profile_to_pymysql_params():
    """Converts to dict without database."""
    profile = ConnectionProfile("host", 3306, "user", "pwd")
    params = profile.to_pymysql_params()
    assert "database" not in params
    assert params["host"] == "host"
    assert params["user"] == "user"

def test_analysis_session_validate_database_identifier():
    """Accepts valid MySQL identifiers."""
    session = AnalysisSession(profile, "production")
    assert session.validate_database_identifier() == True
    
    session = AnalysisSession(profile, "123invalid")
    assert session.validate_database_identifier() == False

def test_environment_session_from_env():
    """Loads from .env like current implementation."""
    monkeypatch.setenv("MYSQL_HOST", "localhost")
    monkeypatch.setenv("MYSQL_USER", "user")
    monkeypatch.setenv("MYSQL_PASSWORD", "pwd")
    monkeypatch.setenv("MYSQL_DATABASE", "testdb")
    
    session = EnvironmentSession.from_env()
    assert session is not None
    assert session.database == "testdb"

def test_connection_profile_get_connection(monkeypatch):
    """Creates connection with correct database."""
    profile = ConnectionProfile("localhost", 3306, "user", "pwd")
    session = AnalysisSession(profile, "production")
    
    # Mock pymysql
    mock_connect = Mock(return_value=Mock())
    monkeypatch.setattr("pymysql.connect", mock_connect)
    
    conn = session.get_connection()
    
    # Verify database was passed
    mock_connect.assert_called_once()
    call_kwargs = mock_connect.call_args[1]
    assert call_kwargs["database"] == "production"
```

### 16.2 Integration Tests (DB Attached)

```python
# Phase 2: Connection Testing

def test_connection_test_success(test_mysql_server):
    """Valid credentials connect successfully."""
    response = client.post("/api/connections/test", json={
        "host": test_mysql_server.host,
        "port": test_mysql_server.port,
        "username": test_mysql_server.user,
        "password": test_mysql_server.password
    })
    assert response.status_code == 200
    assert response.json()["connected"] == True

def test_connection_test_failure():
    """Invalid host fails gracefully."""
    response = client.post("/api/connections/test", json={
        "host": "nonexistent.invalid",
        "port": 3306,
        "username": "user",
        "password": "pwd"
    })
    assert response.status_code == 200
    assert response.json()["connected"] == False
    assert "host" not in response.json()  # No error details

# Phase 3: Database Discovery

def test_discover_databases_success(test_mysql_server):
    """List all databases accessible to account."""
    response = client.post("/api/connections/discover-databases", json={
        "host": test_mysql_server.host,
        "port": test_mysql_server.port,
        "username": test_mysql_server.user,
        "password": test_mysql_server.password
    })
    assert response.status_code == 200
    databases = response.json()["databases"]
    assert "information_schema" in databases

def test_discover_databases_includes_system_dbs():
    """System databases are returned."""
    # Verify that mysql, information_schema, etc. are in result
    assert "mysql" in databases or "information_schema" in databases

# Phase 4: Database Selection

def test_validate_with_database_param(test_mysql_server):
    """Accept database in request body."""
    response = client.post("/api/validate", json={
        "sql": "SELECT * FROM information_schema.TABLES LIMIT 1",
        "database": "information_schema"
    })
    # Should use information_schema, not config.database
    assert response.status_code == 200

def test_validate_backward_compatible(test_mysql_server):
    """Old calls without database param still work."""
    response = client.post("/api/validate", json={
        "sql": "SELECT 1"
    })
    # Should use config.database (from .env or session)
    assert response.status_code == 200

# Phase 5: Connection Profile Input

def test_validate_with_custom_credentials(test_mysql_server):
    """Accept credentials in request body."""
    response = client.post("/api/validate", json={
        "sql": "SELECT * FROM information_schema.TABLES LIMIT 1",
        "connection_profile": {
            "host": test_mysql_server.host,
            "port": test_mysql_server.port,
            "username": test_mysql_server.user,
            "password": test_mysql_server.password
        },
        "database": "information_schema"
    })
    assert response.status_code == 200
    # Should NOT use .env credentials

def test_explain_uses_selected_database():
    """EXPLAIN runs against correct database."""
    # Create table in "production"
    # Query should EXPLAIN against production, not staging
    # Verify EXPLAIN results match production schema
    pass

def test_metadata_uses_selected_database():
    """Metadata queries run against correct database."""
    # Create table with specific structure in "production"
    # Metadata should return production table info, not staging
    pass

def test_session_isolation():
    """Different sessions don't share state."""
    # Session A: connects to production
    # Session B: connects to staging
    # Verify: Session A EXPLAIN doesn't affect Session B metadata
    # Verify: No cross-database result leakage
    pass

def test_database_switch_no_cache_leakage():
    """Switching databases clears old state."""
    # Validate SQL against production
    # Switch to staging
    # Validate different SQL
    # Verify: staging results don't include production data
    pass

# Phase 6: Credential Handling Security

def test_no_credential_in_error_response():
    """Error messages never expose credentials."""
    response = client.post("/api/validate", json={
        "sql": "...",
        "connection_profile": {
            "host": "invalid.host",
            "port": 3306,
            "username": "secret_user",
            "password": "secret_password"
        },
        "database": "testdb"
    })
    error_text = str(response.json())
    assert "secret_user" not in error_text
    assert "secret_password" not in error_text

def test_no_credential_in_logs():
    """Passwords never logged."""
    # Activate logging capture
    # Make failing request with credentials
    # Verify: credentials not in log output
    pass
```

### 16.3 Test Strategy Summary

**Layers to test:**
1. ✅ Connection abstraction (unit)
2. ✅ Session management (unit + integration)
3. ✅ Database discovery (integration)
4. ✅ Database selection (integration)
5. ✅ Credential handling (security tests)
6. ✅ Isolation between sessions (concurrency tests)
7. ✅ Backward compatibility (legacy mode tests)
8. ✅ Error sanitization (security tests)

**Existing tests (must continue to pass):**
- ✅ All 158 existing tests
- ✅ Frozen scoring behavior
- ✅ Read-only evidence boundary
- ✅ Request limits
- ✅ Connection lifecycle

---

## 17. RISKS / OPEN QUESTIONS

### 17.1 Technical Risks

| Risk | Mitigation | Priority |
|------|-----------|----------|
| **Credential in memory risk** | Clear on session close; use secure deletion if possible | HIGH |
| **SQL injection via database name** | Validate identifier before use; parameterized WHERE clauses | HIGH |
| **SSRF future risk (if SaaS)** | Validate host is not internal IP (future); not for self-hosted | FUTURE |
| **Connection leak** | Exhaustive testing of close() in finally blocks | MEDIUM |
| **Database discovery errors** | Sanitize and handle gracefully; allow manual entry fallback | MEDIUM |
| **Privilege escalation via credentials** | Rely on user to supply least-privilege account | OPERATIONAL |

### 17.2 Open Questions

| Question | Answer | Status |
|----------|--------|--------|
| Should we cache database list? | No. Fetch fresh each time (user may modify DBs). | DECIDED |
| How long to keep credentials in memory? | Until session timeout or app close (1 hour default). | DESIGNED |
| Support connection pooling in future? | No. Short-lived connections simpler & safer. | DECIDED |
| Support multi-database single query? | No. One database per analysis session. | DECIDED |
| Support stored procedures? | No. Out of scope for validation tool. | DECIDED |
| Support custom MySQL ports? | Yes. Port is configurable parameter. | DESIGNED |
| Support IPv6 hosts? | Yes. Pass to pymysql as-is (it supports IPv6). | DESIGNED |
| Support Unix socket connections? | Maybe future. Not in Phase 0-1. | FUTURE |
| Support connection compression? | Not required for Phase 0-1. | FUTURE |
| Support prepared statements? | Not needed (parameterized queries sufficient). | DESIGNED |

### 17.3 Security Review Points (for Future Implementation)

**Before deploying Phase 1+, verify:**
- [ ] Credentials never logged (grep logs for password patterns)
- [ ] Error messages never expose driver details
- [ ] Database identifiers validated before use
- [ ] SQL injection vectors explored and tested
- [ ] Session timeouts implemented
- [ ] Concurrent session isolation verified
- [ ] Memory cleanup on logout/timeout
- [ ] No credentials in URL query strings
- [ ] No credentials in response bodies
- [ ] TLS enforcement (future, if multi-server)

---

## 18. EXPECTED FILE CHANGES (NEXT IMPLEMENTATION MILESTONE)

### Files REQUIRED to Change (Phase 1)

| File | Change Type | Purpose | Impact |
|------|-------------|---------|--------|
| `backend/db.py` | Major refactor | Extract ConnectionProfile, AnalysisSession | Tests updated |
| `tests/test_db_evidence.py` | Minor update | Test new abstraction | Same mocking patterns |
| `tests/test_metadata.py` | Minor update | Test database parameter | Same mocking patterns |
| `.env.example` | No change | Already correct | No impact |

### Files NOT Changing (Frozen)

| File | Reason |
|------|--------|
| `backend/risk_engine.py` | Scoring unchanged |
| `backend/parser.py` | Parsing unchanged |
| `backend/evidence_scoring.py` | Evidence adjustment unchanged |
| `backend/metadata_scoring.py` | M1 scoring unchanged |
| `backend/m2_scoring.py` | M2 scoring unchanged |
| `backend/main.py` | API endpoints frozen (until Phase 4+) |
| `backend/analyzer.py` | Analysis logic unchanged |
| `backend/recommendations.py` | Recommendations unchanged |
| `backend/models.py` | Data models frozen |
| `frontend/index.html` | UI unchanged (until Phase 6) |
| `requirements.txt` | Dependencies unchanged |

### Files OPTIONAL to Change (Future Phases)

| File | Phase | Change |
|------|-------|--------|
| `backend/main.py` | Phase 2+ | Add new endpoints (/api/connections/test, etc.) |
| `frontend/index.html` | Phase 6 | Add connection/database selection UI |
| `README.md` | Phase 1+ | Document new architecture |

### Files DEFINITELY NOT Changing (in Any Phase)

- `backend/db_evidence.py` (reuses get_connection() interface)
- `backend/metadata.py` (supports optional database param, backward compat)
- `pytest.ini` (test config unchanged)
- `.gitignore` (version control unchanged)
- `.env` (developer config)

---

## 19. FROZEN FILES EXPLICIT LIST

### Core Scoring Pipeline (FROZEN — Do Not Modify)

```
backend/risk_engine.py
backend/parser.py
backend/evidence_scoring.py
backend/metadata_scoring.py
backend/m2_scoring.py
```

**Why:** All P0-P1 production hardening is locked in these modules. Modifying them would break the audit contract.

### Current Connection Layer (FROZEN — Do Not Modify Until Phase 1)

```
backend/main.py (API contracts frozen)
backend/db_evidence.py (interface frozen)
backend/metadata.py (interface frozen, but accepts optional params)
```

**Why:** Phase 1 refactors backend/db.py but keeps these module interfaces stable.

### Configuration and Models (FROZEN or Minimal Change)

```
backend/analyzer.py (analysis logic)
backend/recommendations.py (findings generation)
backend/models.py (data structures)
backend/__init__.py (exports)
```

**Why:** No changes needed until Phase 4+.

### Test Infrastructure (FROZEN)

```
tests/test_db_evidence.py (frozen until Phase 1 refactor)
tests/test_metadata.py (frozen until Phase 1 refactor)
tests/test_v2_integration.py (frozen)
tests/test_m2_scoring.py (frozen)
tests/test_risk_engine.py (frozen)
tests/test_parser.py (frozen)
pytest.ini (frozen)
```

**Why:** Tests verify frozen behavior. Minor updates needed in Phase 1 to test new abstractions, but test structure/coverage remains.

---

## 20. SUMMARY TABLE: ARCHITECTURE DECISIONS

| Decision | Current | Future (Phase 1+) | Rationale |
|----------|---------|-------------------|-----------|
| **Config source** | .env file | .env + HTTP POST | User flexibility |
| **Config scope** | Process-wide | Per-session | Multi-user support |
| **Database binding** | At connection time | At AnalysisSession | Separate concerns |
| **Connection pool** | None (per-op) | None (per-op) | Simplicity, safety |
| **Credential storage** | Memory only | Memory only | No disk exposure |
| **Session lifetime** | App lifetime | 1 hour or logout | Security |
| **Database selector** | Hardcoded in config | User-selected | Flexibility |
| **EXPLAIN scope** | Global database | Selected database | Multi-DB support |
| **Metadata scope** | Global database | Selected database | Multi-DB support |
| **Error messages** | Sanitized | Sanitized | No credential exposure |
| **Concurrent requests** | Isolated (fresh conn) | Isolated (fresh conn) | No state sharing |

---

## FINAL VERDICT

### Implementation Readiness Assessment

**Architecture is implementation-ready for Phase 1:**

✅ **Clear separation:** Connection scope (credentials) ≠ Analysis scope (database selection)  
✅ **Backward compatible:** .env mode continues to work alongside new per-request credentials  
✅ **Safe short-lived model:** No persistent connection pooling (simpler, safer)  
✅ **Secure credential handling:** Memory-only, cleared on session timeout  
✅ **Incremental migration:** Phase 0→1→2→3→...→6 each builds without breaking prior phases  
✅ **Test strategy clear:** Existing 158 tests preserved, new tests added per phase  
✅ **Files to change identified:** Minimal set for Phase 1 (db.py + tests)  
✅ **Frozen files explicit:** All scoring/parser logic protected from modification  
✅ **API evolution path:** Legacy /api/validate calls remain compatible through Phase 5+  
✅ **Security model defined:** Self-hosted mode clear; SaaS mode deferred (not this milestone)  
✅ **Open questions answered:** Database caching policy, connection pooling rejection, privilege model  
✅ **Risks documented:** Credential handling, SSRF (future), injection points, isolation guarantees  

---

**READY — CONNECTION ARCHITECTURE DESIGN COMPLETE**

This design document provides an implementation-ready blueprint for multi-database, multi-user MySQL connection management while preserving all P0/P1 production hardening and maintaining backward compatibility with the existing .env-based self-hosted deployment model.

The architecture is designed to evolve through 6 phases with each phase adding new capability while preserving existing behavior, allowing safe incremental deployment and testing.


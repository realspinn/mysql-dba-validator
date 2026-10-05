"""Single source of the application release version.

Used by the public API (/api/health, OpenAPI) and by the release build
(release/build_windows.py names the artifact from it). The connector keeps its
own protocol/component version (connector.server.CONNECTOR_VERSION).
"""

APP_VERSION = "0.2.0"

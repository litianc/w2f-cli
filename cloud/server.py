"""Apply additive schema migrations before exposing the HTTP listener."""
import uvicorn
from cloud.app import Settings
from cloud.store import Store

if __name__ == '__main__':
    Store(Settings.from_env()).migrate()
    uvicorn.run('cloud.app:create_app', factory=True, host='0.0.0.0', port=8080,
                access_log=False, proxy_headers=False)

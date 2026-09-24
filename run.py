import os
import uvicorn

if __name__ == '__main__':
    # Render injects PORT; keep 8000 as the local Windows default.
    port = int(os.getenv('PORT', '8000'))
    uvicorn.run('backend.app:app', host='0.0.0.0', port=port, reload=False, proxy_headers=True, forwarded_allow_ips='*')

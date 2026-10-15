"""Start a single app process on loopback, locally or behind an HTTPS proxy."""
import argparse
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / '.env')

if __name__ == '__main__':
    import uvicorn
    parser = argparse.ArgumentParser(description='票栈 · 发票与报销管理')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8765')))
    args = parser.parse_args()
    print(f'\n票栈启动：http://127.0.0.1:{args.port}\n按 Ctrl+C 停止。\n', flush=True)
    uvicorn.run('app.main:app', host='127.0.0.1', port=args.port, log_level='info',
                workers=1, proxy_headers=True, forwarded_allow_ips='127.0.0.1')

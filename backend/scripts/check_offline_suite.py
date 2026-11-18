"""运行现有离线测试，不调用付费接口，也不写入用户的 SQLite 数据库。"""
import contextlib
import io
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


def main():
    root=Path(__file__).resolve().parents[1]
    for name in ('REDIS_ENABLED','CACHE_ENABLED','MCP_WARMUP_ON_STARTUP'):
        os.environ[name]='false'
    for name in ('LLM_API_KEY','OPENAI_API_KEY','AMAP_API_KEY','UNSPLASH_ACCESS_KEY','BAIDU_SEARCH_API_KEY'):
        os.environ[name]=''
    real_db, real_connect = sqlite3.connect, socket.socket.connect
    with tempfile.TemporaryDirectory(prefix='tripmind-acceptance-') as temp:
        def database(path,*args,**kwargs):
            if Path(str(path)).resolve()==root/'data'/'tripmind.db':
                path=Path(temp)/'tripmind.db'
            return real_db(path,*args,**kwargs)
        def connect(sock,address):
            # Windows 的 asyncio 即使使用进程内测试客户端，也需要本机回环套接字对。
            if isinstance(address,tuple) and address[0] not in ('127.0.0.1','::1','localhost'):
                raise RuntimeError('Offline acceptance: external network disabled')
            return real_connect(sock,address)
        output=io.StringIO()
        with patch('sqlite3.connect',side_effect=database), patch.object(socket.socket,'connect',connect), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            suite=unittest.defaultTestLoader.discover(str(root/'tests'))
            result=unittest.TextTestRunner(stream=output,verbosity=0).run(suite)
        print(f'Ran {result.testsRun}; failures={len(result.failures)}; errors={len(result.errors)}; skipped={len(result.skipped)}')
        for case, trace in result.failures+result.errors:
            print(case.id()+':\n'+'\n'.join(trace.splitlines()[-9:]))
        return 0 if result.wasSuccessful() else 1


if __name__=='__main__':
    raise SystemExit(main())


import asyncio
import atexit
import logging
import os
import requests
import sys
import tortoise.contrib.fastapi
import urllib.parse
from fastapi import FastAPI
from fastapi import Request
from fastapi import status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi_utils.tasks import repeat_every
from pydantic import ValidationError

from app.constants import CONFIG, CLIENT_DIR, DATABASE_CONFIG, LIBRARY_PATH, QUALITY, VERSION
from app.models import Channels
from app.models import LiveStream
from app.models import Programs
from app.routers import ChannelsRouter
from app.routers import LiveStreamsRouter
from app.schemas import Config
from app.utils import Logging
from app.utils.EDCB import CtrlCmdUtil
from app.utils.EDCB import EDCBTuner


# このアプリケーションのイベントループ
loop = asyncio.get_event_loop()

# 環境設定のバリデーション
try:
    Config(**CONFIG)
except ValidationError as error:
    Logging.error(
        '設定内容が不正なため、KonomiTV を起動できません。\n          '
        '以下のエラーメッセージを参考に、config.yaml の記述が正しいかを確認してください。'
    )
    Logging.error(error)
    sys.exit(1)

## EDCB のホスト名とポートを追加で設定
## 毎回 URL を解析するのは非効率なため、ここで設定しておく
edcb_url_parse = urllib.parse.urlparse(CONFIG['general']['edcb_url'])
CONFIG['general']['edcb_host'] = edcb_url_parse.hostname
CONFIG['general']['edcb_port'] = edcb_url_parse.port

# サードパーティーライブラリが配置されているかのバリデーション
for library_name, library_path in LIBRARY_PATH.items():
    if not os.path.isfile(library_path):
        Logging.error(
            f'{library_name} がサードパーティーライブラリとして配置されていないため、KonomiTV を起動できません。\n          '
            f'{library_name} が {library_path} に配置されているかを確認してください。'
        )
        sys.exit(1)

# FastAPI を初期化
app = FastAPI(
    title='KonomiTV',
    description='KonomiTV: Kind and Optimized Next On-broadcasting watching and Management Infrastructure for TV',
    version=VERSION,
    openapi_url='/api/openapi.json',
    docs_url='/api/docs',
    redoc_url='/api/redoc',
)

# CORS の設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        '*' if CONFIG['general']['debug'] is True else '',  # デバッグ時のみ CORS ヘッダーを有効化
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ルーターの追加
app.include_router(ChannelsRouter.router)
app.include_router(LiveStreamsRouter.router)

# 静的ファイルの配信
app.mount('/assets', StaticFiles(directory=CLIENT_DIR / 'assets', html=True))

# ルート以下のルーティング
# ファイルが存在すればそのまま配信し、ファイルが存在しなければ index.html を返す
@app.get('/{file:path}', include_in_schema=False)
async def Root(file:str):

    # ファイルが存在する
    filepath = CLIENT_DIR / f'{file}'
    if os.path.isfile(filepath):
        fileext = os.path.splitext(file)[1]
        # MIMEタイプ
        if fileext == '.css':
            mime = 'text/css'
        elif fileext == '.html':
            mime = 'text/html'
        elif fileext == '.ico':
            mime = 'image/x-icon'
        elif fileext == '.js':
            mime = 'application/javascript'
        elif fileext == '.json':
            mime = 'application/json'
        else:
            mime = 'text/plain'
        return FileResponse(filepath, media_type=mime)

    # デフォルトドキュメント (index.html)
    # URL の末尾にスラッシュがついている場合のみ
    elif os.path.isfile(filepath / 'index.html') and (file == '' or file[-1] == '/'):
        return FileResponse(filepath / 'index.html', media_type='text/html')

    # 存在しない静的ファイルが指定された場合
    else:
        if file.startswith('api/'):
            # パスに api/ が前方一致で含まれているなら、404 Not Found を返す
            return JSONResponse({'detail': 'Not Found'}, status_code=status.HTTP_404_NOT_FOUND)
        else:
            # パスに api/ が前方一致で含まれていなければ、index.html を返す
            return FileResponse(CLIENT_DIR / 'index.html', media_type='text/html')

# Internal Server Error のハンドリング
@app.exception_handler(Exception)
async def ExceptionHandler(request:Request, exc: Exception):
    return JSONResponse(
        {'detail': f'Oops! {type(exc).__name__} did something. There goes a rainbow...'},
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )

# Tortoise ORM の初期化
## ロガーを Uvicorn に統合する
## ref: https://github.com/tortoise/tortoise-orm/issues/529
tortoise.contrib.fastapi.logging = logging.getLogger('uvicorn')
## Tortoise ORM を登録する
## ref: https://tortoise-orm.readthedocs.io/en/latest/contrib/fastapi.html
tortoise.contrib.fastapi.register_tortoise(
    app=app,
    config=DATABASE_CONFIG,
    generate_schemas=True,
    add_exception_handlers=True,
)

# サーバーの起動時に実行する
@app.on_event('startup')
async def Startup():

    try:

        # Mirakurun バックエンドの接続確認
        if CONFIG['general']['backend'] == 'Mirakurun':

            # 試しにリクエストを送り、200 (OK) が返ってきたときだけ有効な URL とみなす
            try:
                response = await asyncio.to_thread(requests.get, f'{CONFIG["general"]["mirakurun_url"]}/api/version', timeout=3)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                raise ValueError(f'Mirakurun ({CONFIG["general"]["mirakurun_url"]}) にアクセスできませんでした。Mirakurun が起動していないか、URL を間違えている可能性があります。')
            if response.status_code != 200:
                raise ValueError(f'{CONFIG["general"]["mirakurun_url"]} は Mirakurun の URL ではありません。Mirakurun の URL を間違えている可能性があります。')

        # EDCB バックエンドの接続確認
        elif CONFIG['general']['backend'] == 'EDCB':

            # ホスト名またはポートが指定されていない
            if ((CONFIG['general']['edcb_host'] is None) or
                (CONFIG['general']['edcb_port'] is None and CONFIG['general']['edcb_host'] != 'edcb-namedpipe')):
                raise ValueError(f'URL 内にホスト名またはポートが指定されていません。EDCB の URL を間違えている可能性があります。')

            # サービス一覧が取得できるか試してみる
            edcb = CtrlCmdUtil()
            edcb.setConnectTimeOutSec(3)  # 3秒後にタイムアウト
            result = await edcb.sendEnumService()
            if result is None:
                raise ValueError(f'EDCB ({CONFIG["general"]["edcb_url"]}) にアクセスできませんでした。EDCB が起動していないか、URL を間違えている可能性があります。')

    # エラー発生時
    except ValueError as exception:

        # ログ出力
        Logging.error(
            '設定内容が不正なため、KonomiTV を起動できません。\n          '
            '以下のエラーメッセージを参考に、config.yaml の記述が正しいかを確認してください。'
        )
        Logging.error(exception.args[0])

        # サーバーを終了
        os._exit(1)

    # チャンネル情報を更新
    await Channels.update()

    # ニコニコ実況関連のステータスを更新
    await Channels.updateJikkyoStatus()

    # 番組情報を更新
    await Programs.update()

    # 全てのチャンネル&品質のライブストリームを初期化する
    for channel in await Channels.all().order_by('channel_number').values():
        for quality in QUALITY:
            LiveStream(channel['channel_id'], quality)

# 1時間に1回、チャンネル情報を定期的に更新する
# チャンネル情報は頻繁に変わるわけではないけど、手動で再起動しなくても自動で変更が適用されてほしい
@app.on_event('startup')
@repeat_every(seconds=60 * 60, wait_first=True, logger=Logging.logger)
async def UpdateProgram():
    await Channels.update()
    await Channels.updateJikkyoStatus()

# 15分に1回、番組情報を定期的に更新する
# 番組情報の更新処理はかなり重くストリーム配信などの他の処理に影響してしまうため、マルチプロセスで実行する
@app.on_event('startup')
@repeat_every(seconds=15 * 60, wait_first=True, logger=Logging.logger)
async def UpdateProgram():
    await Programs.update(multiprocess=True)

# 30秒に1回、ニコニコ実況関連のステータスを定期的に更新する
@app.on_event('startup')
@repeat_every(seconds=0.5 * 60, wait_first=True, logger=Logging.logger)
async def UpdateJikkyoStatus():
    await Channels.updateJikkyoStatus()

# サーバーの終了時に実行する
cleanup = False
@app.on_event('shutdown')
async def Shutdown():

    # 2度呼ばれないように
    global cleanup
    if cleanup is True:
        return
    cleanup = True

    # 全てのライブストリームを終了する
    for livestream in LiveStream.getAllLiveStreams():
        livestream.setStatus('Offline', 'ライブストリームは Offline です。', True)

    # 全てのチューナーインスタンスを終了する (EDCB バックエンドのみ)
    if CONFIG['general']['backend'] == 'EDCB':
        await EDCBTuner.closeAll()

# shutdown イベントが発火しない場合も想定し、アプリケーションの終了時に Shutdown() が確実に呼ばれるように
# atexit は同期関数しか実行できないので、asyncio.run() でくるむ
atexit.register(asyncio.run, Shutdown())

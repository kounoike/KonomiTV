
import asyncio
import os
import requests
import socket
import subprocess
import time
from typing import BinaryIO, Literal, Optional, Union

from app.constants import CONFIG, LIBRARY_PATH, QUALITY
from app.models import Channels
from app.models import LiveStream
from app.models import Programs
from app.utils import Logging
from app.utils.EDCB import EDCBTuner


class LiveEncodingTask():


    def __init__(self):

        # エンコードタスクのリトライ回数のカウント
        self.retry_count = 0

        # エンコードタスクの最大リトライ回数
        # この数を超えた場合はエンコードタスクを再起動しない（無限ループ避け）
        self.max_retry_count = 5  # 5 回まで


    def isFullHDChannel(self, network_id:int, service_id:int) -> bool:
        """
        ネットワーク ID とサービス ID から、そのチャンネルでフル HD 放送が行われているかを返す
        放送波の PSI/SI から映像の横解像度を取得する手段がないので、現状 ID 決め打ちになっている
        ref: https://twitter.com/highwaymovies/status/1201282179390562305
        ref: https://scrapbox.io/ci7lus/%E5%9C%B0%E4%B8%8A%E6%B3%A2%E3%81%AA%E3%81%AE%E3%81%ABFHD%E3%81%AE%E6%94%BE%E9%80%81%E5%B1%80%E6%83%85%E5%A0%B1

        Args:
            network_id (int): ネットワーク ID
            service_id (int): サービス ID

        Returns:
            bool: フル HD 放送が行われているチャンネルかどうか
        """

        # 地デジでフル HD 放送を行っているチャンネルのネットワーク ID と一致する
        ## あいテレビ, びわ湖放送, 奈良テレビ, KBS京都, KNB北日本放送, ABS秋田放送
        if network_id in [31940, 32038, 32054, 32102, 32162, 32466]:
            return True

        # BS でフル HD 放送を行っているチャンネルのサービス ID と一致する
        ## NHK BSプレミアム・WOWOWプライム・WOWOWライブ・WOWOWシネマ・BS11
        if network_id == 4 and service_id in [103, 191, 192, 193, 211]:
            return True

        return False


    def buildFFmpegOptions(self, quality:str, is_fullhd_channel:bool = False) -> list:
        """
        FFmpeg に渡すオプションを組み立てる

        Args:
            quality (str): 映像の品質 (1080p ~ 240p)
            is_fullhd_channel (bool): フル HD 放送が実施されているチャンネルかどうか

        Returns:
            list: FFmpeg に渡すオプションが連なる配列
        """

        # オプションの入る配列
        options = []

        # 入力
        ## -analyzeduration をつけることで、ストリームの分析時間を短縮できる
        analyzeduration = round(500000 + (self.retry_count * 200000))  # リトライ回数に応じて少し増やす
        options.append(f'-f mpegts -analyzeduration {analyzeduration} -i pipe:0')

        # ストリームのマッピング
        ## 音声切り替えのため、主音声・副音声両方をエンコード後の TS に含む
        ## 副音声が検出できない場合にエラーにならないよう、? をつけておく
        options.append('-map 0:v:0 -map 0:a:0 -map 0:a:1 -map 0:d? -ignore_unknown')

        # フラグ
        ## 主に FFmpeg の起動を高速化するための設定
        max_interleave_delta = round(1 + self.retry_count)
        options.append(f'-fflags nobuffer -flags low_delay -max_delay 250000 -max_interleave_delta {max_interleave_delta} -threads auto')

        # 映像
        options.append(f'-vcodec libx264 -flags +cgop -vb {QUALITY[quality]["video_bitrate"]} -maxrate {QUALITY[quality]["video_bitrate_max"]}')
        options.append('-aspect 16:9 -r 30000/1001 -g 60 -preset veryfast -profile:v main')
        ## フル HD 放送が行われているチャンネルのみ、指定された品質が 1080p であればフル HD でエンコードする
        if quality == '1080p' and is_fullhd_channel is True:
            options.append('-vf yadif=0:-1:1,scale=1920:1080')
        else:
            options.append(f'-vf yadif=0:-1:1,scale={QUALITY[quality]["width"]}:{QUALITY[quality]["height"]}')

        # 音声
        ## 音声が 5.1ch かどうかに関わらず、ステレオにダウンミックスする
        options.append(f'-acodec aac -aac_coder twoloop -ac 2 -ab {QUALITY[quality]["audio_bitrate"]} -ar 48000 -af volume=2.0')

        # 出力
        options.append('-y -f mpegts')  # MPEG-TS 出力ということを明示
        options.append('pipe:1')  # 標準入力へ出力

        # オプションをスペースで区切って配列にする
        result = []
        for option in options:
            result += option.split(' ')

        return result


    def buildFFmpegOptionsForRadio(self) -> list:
        """
        FFmpeg に渡すオプションを組み立てる（ラジオチャンネル向け）
        音声の品質は変えたところでほとんど差がないため、1つだけに固定されている
        品質が固定ならコードにする必要はないんだけど、可読性を高めるために敢えてこうしてある

        Returns:
            list: FFmpeg に渡すオプションが連なる配列
        """

        # オプションの入る配列
        options = []

        # 入力
        ## -analyzeduration をつけることで、ストリームの分析時間を短縮できる
        analyzeduration = round(500000 + (self.retry_count * 200000))  # リトライ回数に応じて少し増やす
        options.append(f'-f mpegts -analyzeduration {analyzeduration} -i pipe:0')

        # ストリームのマッピング
        # 音声切り替えのため、主音声・副音声両方をエンコード後の TS に含む
        options.append('-map 0:a:0 -map 0:a:1 -map 0:d? -ignore_unknown')

        # フラグ
        ## 主に FFmpeg の起動を高速化するための設定
        max_interleave_delta = round(1 + self.retry_count)
        options.append(f'-fflags nobuffer -flags low_delay -max_delay 250000 -max_interleave_delta {max_interleave_delta} -threads auto')

        # 音声
        ## 音声が 5.1ch かどうかに関わらず、ステレオにダウンミックスする
        options.append(f'-acodec aac -aac_coder twoloop -ac 2 -ab 192K -ar 48000 -af volume=2.0')

        # 出力
        options.append('-y -f mpegts')  # MPEG-TS 出力ということを明示
        options.append('pipe:1')  # 標準入力へ出力

        # オプションをスペースで区切って配列にする
        result = []
        for option in options:
            result += option.split(' ')

        return result


    def buildHWEncCOptions(self, quality:str, encoder_type:Literal['QSVEncC', 'NVEncC', 'VCEEncC'], is_fullhd_channel:bool = False) -> list:
        """
        QSVEncC・NVEncC・VCEEncC (便宜上 HWEncC と総称) に渡すオプションを組み立てる

        Args:
            quality (str): 映像の品質 (1080p ~ 240p)
            encoder_type (Literal['QSVEncC', 'NVEncC', 'VCEEncC']): エンコーダー (QSVEncC or NVEncC or VCEEncC)
            is_fullhd_channel (bool): フル HD 放送が実施されているチャンネルかどうか

        Returns:
            list: HWEncC に渡すオプションが連なる配列
        """

        # オプションの入る配列
        options = []

        # 入力
        ## --input-probesize, --input-analyze をつけることで、ストリームの分析時間を短縮できる
        ## 両方つけるのが重要で、--input-analyze だけだとエンコーダーがフリーズすることがある
        input_probesize = round(1000 + (self.retry_count * 500))  # リトライ回数に応じて少し増やす
        input_analyze = round(0.7 + (self.retry_count * 0.2), 1)  # リトライ回数に応じて少し増やす
        options.append(f'--input-format mpegts --fps 30000/1001 --input-probesize {input_probesize}K --input-analyze {input_analyze} --input -')
        ## VCEEncC の HW デコーダーはエラー耐性が低く TS を扱う用途では不安定なので、SW デコーダーを利用する
        if encoder_type == 'VCEEncC':
            options.append('--avsw')
        ## QSVEncC・NVEncC は HW デコーダーを利用する
        else:
            options.append('--avhw')

        # ストリームのマッピング
        ## 音声切り替えのため、主音声・副音声両方をエンコード後の TS に含む
        ## 音声が 5.1ch かどうかに関わらず、ステレオにダウンミックスする
        options.append('--audio-stream 1?:stereo --audio-stream 2?:stereo --data-copy timed_id3')

        # フラグ
        ## 主に HWEncC の起動を高速化するための設定
        max_interleave_delta = round(1 + self.retry_count)
        options.append(f'-m fflags:nobuffer -m max_delay:250000 -m max_interleave_delta:{max_interleave_delta} --output-thread -1 --lowlatency')
        ## その他の設定
        options.append('--avsync forcecfr --max-procfps 60 --log-level debug')

        # 映像
        options.append(f'--vbr {QUALITY[quality]["video_bitrate"]} --max-bitrate {QUALITY[quality]["video_bitrate_max"]}')
        options.append(f'--dar 16:9 --gop-len 60 --profile main --interlace tff')
        ## インターレース解除
        if encoder_type == 'QSVEncC' or encoder_type == 'NVEncC':
            options.append('--vpp-deinterlace normal')
        elif encoder_type == 'VCEEncC':
            options.append('--vpp-afs preset=default')
        ## プリセット
        if encoder_type == 'QSVEncC':
            options.append('--quality balanced')
        elif encoder_type == 'NVEncC':
            options.append('--preset default')
        elif encoder_type == 'VCEEncC':
            options.append('--preset balanced')
        ## フル HD 放送が行われているチャンネルのみ、指定された品質が 1080p であればフル HD でエンコードする
        if quality == '1080p' and is_fullhd_channel is True:
            options.append('--output-res 1920x1080')
        else:
            options.append(f'--output-res {QUALITY[quality]["width"]}x{QUALITY[quality]["height"]}')

        # 音声
        options.append(f'--audio-codec aac:aac_coder=twoloop --audio-bitrate {QUALITY[quality]["audio_bitrate"]}')
        options.append(f'--audio-samplerate 48000 --audio-filter volume=2.0 --audio-ignore-decode-error 30')

        # 出力
        options.append('--output-format mpegts')  # MPEG-TS 出力ということを明示
        options.append('--output -')  # 標準入力へ出力

        # オプションをスペースで区切って配列にする
        result = []
        for option in options:
            result += option.split(' ')

        return result


    async def run(self, channel_id:str, quality:str) -> None:
        """
        エンコードタスクを実行する
        プロセス実行なども含めてすべて非同期にしようとすると収拾がつかない上に性能上の不安があるため、開始時・終了時の処理は非同期化した上で、
        役割の異なる reader()・writer()・controller() の3つの同期関数をスレッドプール上で同時に実行する構成になっている

        Args:
            channel_id (str): チャンネルID
            quality (str): 映像の品質 (1080p ~ 240p)
        """

        # ライブストリームのインスタンスを取得する
        livestream = LiveStream(channel_id, quality)

        # まだ Standby になっていなければ、ステータスを Standby に設定
        # 基本はエンコードタスクの呼び出し元である livestream.connect() の方で Standby に設定されるが、再起動の場合はそこを経由しないため必要
        if not (livestream.getStatus()['status'] == 'Standby' and livestream.getStatus()['detail'] == 'エンコードタスクを起動しています…'):
            livestream.setStatus('Standby', 'エンコードタスクを起動しています…')

        # チャンネル情報からサービス ID とネットワーク ID を取得する
        channel:Channels = await Channels.filter(channel_id=channel_id).first()

        # 現在の番組情報を取得する
        program_present:Programs = (await channel.getCurrentAndNextProgram())[0]

        ## 番組情報が取得できなければ（放送休止中など）ここで Offline にしてエンコードタスクを停止する
        ## スターデジオは番組情報が取れないことが多いので(特に午後)、休止になっていても無視してストリームを開始する
        ## 放送大学ラジオは正しい番組情報が取得できるので制御しない
        if program_present is None and channel.channel_type != 'STARDIGIO':
            await asyncio.sleep(0.5)  # ちょっと待つのがポイント
            livestream.setStatus('Offline', 'この時間は放送を休止しています。')
            return
        Logging.info(f'LiveStream:{livestream.livestream_id} Title:{program_present.title}')

        # tsreadex の起動
        ## 放送波の前処理を行い、エンコードを安定させるツール
        ## オプション内容は https://github.com/xtne6f/tsreadex を参照
        tsreadex:subprocess.Popen = await asyncio.to_thread(subprocess.Popen,
            [
                ## tsreadex のパス
                LIBRARY_PATH['tsreadex'],
                # 取り除く TS パケットの10進数の PID
                ## EIT の PID を指定
                '-x', '18/38/39',
                # 特定サービスのみを選択して出力するフィルタを有効にする
                ## 有効にすると、特定のストリームのみ PID を固定して出力される
                '-n', '-1',
                # 主音声ストリームが常に存在する状態にする
                ## ストリームが存在しない場合、無音の AAC ストリームが出力される
                ## 音声がモノラルであればステレオにする
                ## デュアルモノを2つのモノラル音声に分離し、右チャンネルを副音声として扱う
                '-a', '13',
                # 副音声ストリームが常に存在する状態にする
                ## ストリームが存在しない場合、無音の AAC ストリームが出力される
                ## 音声がモノラルであればステレオにする
                '-b', '5',
                # 字幕ストリームが常に存在する状態にする
                ## ストリームが存在しない場合、PMT の項目が補われて出力される
                '-c', '1',
                # 文字スーパーストリームが常に存在する状態にする
                ## ストリームが存在しない場合、PMT の項目が補われて出力される
                '-u', '1',
                # 字幕と文字スーパーを aribb24.js が解釈できる ID3 timed-metadata に変換する
                ## +4: FFmpeg のバグを打ち消すため、変換後のストリームに規格外の5バイトのデータを追加する
                ## +8: FFmpeg のエラーを防ぐため、変換後のストリームの PTS が単調増加となるように調整する
                '-d', '13',
                # 標準入力を設定
                '-',
            ],
            stdin=subprocess.PIPE,  # 受信した放送波を書き込む
            stdout=subprocess.PIPE,  # エンコーダーに繋ぐ
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),  # conhost を開かない
        )

        # ***** エンコーダープロセスの作成と実行 *****

        # エンコーダーの起動には時間がかかるので、先にエンコーダーを起動しておいた後、あとからチューナーを起動する
        # チューナーの起動後にエンコーダー (正確には tsreadex) に受信した放送波が書き込まれる
        # チューナーの起動にも時間がかかるが、エンコーダーの起動は非同期なのに対し、チューナーの起動は EDCB の場合は同期的

        # フル HD 放送が行われているチャンネルかを取得
        is_fullhd_channel = self.isFullHDChannel(channel.network_id, channel.service_id)

        # エンコーダーの種類を取得
        ## ラジオチャンネルでは HW エンコードの意味がないため、FFmpeg に固定する
        if channel.is_radiochannel is True:
            encoder_type = 'FFmpeg'
        else:
            encoder_type = CONFIG['livestream']['encoder']

        # FFmpeg
        if encoder_type == 'FFmpeg':

            # オプションを取得
            # ラジオチャンネルかどうかでエンコードオプションを切り替え
            if channel.is_radiochannel is True:
                encoder_options = self.buildFFmpegOptionsForRadio()
            else:
                encoder_options = self.buildFFmpegOptions(quality, is_fullhd_channel)
            Logging.info(f'LiveStream:{livestream.livestream_id} FFmpeg Commands:\nffmpeg {" ".join(encoder_options)}')

            # プロセスを非同期で作成・実行
            encoder:subprocess.Popen = await asyncio.to_thread(subprocess.Popen,
                [LIBRARY_PATH['FFmpeg']] + encoder_options,
                stdin=tsreadex.stdout,  # tsreadex からの入力
                stdout=subprocess.PIPE,  # ストリーム出力
                stderr=subprocess.PIPE,  # ログ出力
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),  # conhost を開かない
            )

        # HWEncC
        elif encoder_type == 'QSVEncC' or encoder_type == 'NVEncC' or encoder_type == 'VCEEncC':

            # オプションを取得
            encoder_options = self.buildHWEncCOptions(quality, encoder_type, is_fullhd_channel)
            Logging.info(f'LiveStream:{livestream.livestream_id} {encoder_type} Commands:\n{encoder_type} {" ".join(encoder_options)}')

            # プロセスを非同期で作成・実行
            encoder:subprocess.Popen = await asyncio.to_thread(subprocess.Popen,
                [LIBRARY_PATH[encoder_type]] + encoder_options,
                stdin=tsreadex.stdout,  # tsreadex からの入力
                stdout=subprocess.PIPE,  # ストリーム出力
                stderr=subprocess.PIPE,  # ログ出力
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0),  # conhost を開かない
            )

        # ***** チューナーの起動と接続 *****

        # Mirakurun バックエンド
        if CONFIG['general']['backend'] == 'Mirakurun':

            # Mirakurun 形式のサービス ID
            # NID と SID を 5 桁でゼロ埋めした上で int に変換する
            mirakurun_service_id = int(str(channel.network_id).zfill(5) + str(channel.service_id).zfill(5))
            # Mirakurun API の URL を作成
            mirakurun_stream_api_url = f'{CONFIG["general"]["mirakurun_url"]}/api/services/{mirakurun_service_id}/stream'

            # HTTP リクエストを開始
            ## stream=True を設定することで、レスポンスの返却を待たずに処理を進められる
            try:
                livestream.setStatus('Standby', 'チューナーを起動しています…')
                response:requests.Response = await asyncio.to_thread(requests.get,
                    url=mirakurun_stream_api_url,
                    headers={'X-Mirakurun-Priority': '0'},
                    stream=True,
                    timeout=15,
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                # 番組名に「放送休止」などが入っていれば停波によるものとみなし、そうでないならチューナーへの接続に失敗したものとする
                if (('放送休止' in program_present.title) or
                    ('放送終了' in program_present.title) or
                    ('休止' in program_present.title) or
                    ('停波' in program_present.title)):
                    livestream.setStatus('Offline', 'この時間は放送を休止しています。')
                else:
                    livestream.setStatus('Offline', 'チューナーへの接続に失敗しました。チューナー側に何らかの問題があるかもしれません。')
                return

        # EDCB バックエンド
        elif CONFIG['general']['backend'] == 'EDCB':

            # チューナーインスタンスを初期化
            tuner = EDCBTuner(channel.network_id, channel.service_id, channel.transport_stream_id)

            # チューナーを起動する
            # アンロック状態のチューナーインスタンスがあれば、自動的にそのチューナーが再利用される
            livestream.setStatus('Standby', 'チューナーを起動しています…')
            is_tuner_opened = await tuner.open()

            # チューナーの起動に失敗した
            # ほとんどがチューナー不足によるものなので、ステータス詳細でもそのように表示する
            # 成功時は tuner.close() するか予約などに割り込まれるまで起動しつづけるので注意
            if is_tuner_opened is False:
                livestream.setStatus('Offline', 'チューナーの起動に失敗しました。チューナー不足が原因かもしれません。')
                return

            # チューナーをロックする
            # ロックしないと途中でチューナーの制御を横取りされてしまう
            tuner.lock()

            # チューナーに接続する
            # 放送波が送信される TCP ソケットまたは名前付きパイプを取得する
            livestream.setStatus('Standby', 'チューナーに接続しています…')
            pipe_or_socket:Optional[Union[BinaryIO, socket.socket]] = await tuner.connect()

            # チューナーへの接続に失敗した
            if pipe_or_socket is None:
                livestream.setStatus('Offline', 'チューナーへの接続に失敗しました。チューナー側に何らかの問題があるかもしれません。')
                return

            # ライブストリームにチューナーインスタンスを設定する
            # Idling への切り替え、ONAir への復帰時に LiveStream 側でチューナーのアンロック/ロックが行われる
            livestream.setTunerInstance(tuner)

        # ***** エンコーダーへの入力の読み込み *****

        # CPU バウンドな部分なので、同期関数をマルチスレッドで実行する
        def reader():

            # 受信した放送波が入るイテレータ
            # R/W バッファ: 188B (TS Packet Size) * 256 = 48128B
            ## Mirakurun
            if CONFIG['general']['backend'] == 'Mirakurun':
                # Mirakurun の HTTP API から受信
                stream_iterator = response.iter_content(chunk_size=48128)
            ## EDCB
            elif CONFIG['general']['backend'] == 'EDCB':
                if type(pipe_or_socket) is socket.socket:
                    # EDCB の TCP ソケットから受信
                    stream_iterator = iter(lambda: pipe_or_socket.recv(48128), b'')
                else:
                    # EDCB の名前付きパイプから受信
                    stream_iterator = iter(lambda: pipe_or_socket.read(48128), b'')

            # Mirakurun / EDCB から受信した放送波を随時 tsreadex の入力に書き込む
            try:
                for chunk in stream_iterator:

                    # ストリームデータを tsreadex の標準入力に書き込む
                    try:
                        tsreadex.stdin.write(bytes(chunk))
                    except BrokenPipeError:
                        break
                    except OSError:
                        break

                    # Mirakurun からエラーが返された
                    if CONFIG['general']['backend'] == 'Mirakurun' and response.status_code is not None and response.status_code != 200:
                        # Offline にしてエンコードタスクを停止する
                        if response.status_code == 503:
                            livestream.setStatus('Offline', 'チューナーの起動に失敗しました。チューナー不足が原因かもしれません。')
                        else:
                            livestream.setStatus('Offline', 'チューナーで不明なエラーが発生しました。Mirakurun 側に問題があるかもしれません。')
                        break

                    # 現在 ONAir でかつストリームデータの最終書き込み時刻から 5 秒以上が経過しているなら、エンコーダーがフリーズしたものとみなす
                    # 現在 Standby でかつストリームデータの最終書き込み時刻から 20 秒以上が経過している場合も、エンコーダーがフリーズしたものとみなす
                    # stdout も stderr もブロッキングされてしまっている場合を想定し、このスレッドでも確認する
                    livestream_status = livestream.getStatus()
                    if ((livestream_status['status'] == 'ONAir' and time.time() - livestream.stream_data_written_at > 5) or
                        (livestream_status['status'] == 'Standby' and time.time() - livestream.stream_data_written_at > 20)):

                        # エンコーダーを強制終了させないと次の処理に進まない事が想定されるので、エンコーダーを強制終了
                        if encoder is not None:
                            encoder.kill()

                        # エンコードタスクを再起動
                        if self.retry_count < self.max_retry_count:  # リトライの制限内であれば
                            Logging.debug('Detects encoder termination (Reader thread).')
                            livestream.setStatus('Restart', 'エンコードが途中で停止しました。エンコードタスクを再起動します。')
                        break

                    # tsreadex が既に終了しているか、接続が切断された
                    # ref: https://stackoverflow.com/a/45251241/17124142
                    if ((tsreadex.poll() is not None) or
                        (CONFIG['general']['backend'] == 'Mirakurun' and response.raw.closed is True) or
                        (CONFIG['general']['backend'] == 'EDCB' and type(pipe_or_socket) is socket.socket and pipe_or_socket.fileno() < 0) or
                        (CONFIG['general']['backend'] == 'EDCB' and type(pipe_or_socket) is BinaryIO and pipe_or_socket.closed is True)):

                        # この時点でまだ Offline 状態でなければエンコードタスクを再起動する
                        ## 通常ここが呼ばれるのは正常に Offline に設定された後なので、Offline 状態になっていないとおかしい
                        ## 放送休止によるストリーム出力の終了でタイムアウトしたり、バックエンドのサービスが停止されたなどの理由が考えられる
                        if livestream.getStatus()['status'] != 'Offline':
                            livestream.setStatus('Restart', 'チューナーとの接続が切断されました。エンコードタスクを再起動します。')
                        break

            except OSError:
                pass

            # チューナーとの接続を明示的に閉じる
            try:
                tsreadex.stdin.close()
            except OSError:
                pass
            if CONFIG['general']['backend'] == 'Mirakurun':
                response.close()
            elif CONFIG['general']['backend'] == 'EDCB':
                pipe_or_socket.close()

        # スレッドプール上で非同期に実行する
        asyncio.create_task(asyncio.to_thread(reader))

        # ***** エンコーダーからの出力の書き込み *****

        # CPU バウンドな部分なので、同期関数をマルチスレッドで実行する
        def writer():

            # R/W バッファ: 188B (TS Packet Size) * 128 = 24064B
            # エンコードによってかなりデータ量が減るので、reader よりもバッファを減らしてみる
            # 810p 以上ではデータ量が多くなるので、バッファを 188B (TS Packet Size) * 192 = 36096B に増やす
            # ラジオチャンネルではデータ量がかなり少なくなるので、バッファを 188B (TS Packet Size) * 48 = 9024B に減らす
            buffer = 24064
            if quality == '810p' or quality == '1080p':
                buffer = 36096
            elif channel.is_radiochannel is True:
                buffer = 9024

            # エンコーダーの出力を受け取るイテレータ
            stream_iterator = iter(lambda: encoder.stdout.read(buffer), b'')

            for chunk in stream_iterator:

                # エンコーダーから受けた出力をライブストリームの Queue に書き込む
                livestream.write(chunk)

                # エンコーダープロセスが終了していたらループを抜ける
                if encoder.poll() is not None:

                    # ループを抜ける前に、接続している全てのクライアントの Queue にライブストリームの終了を知らせる None を書き込む
                    # クライアントは None を受信した場合、ストリーミングを終了するようになっている
                    # これがないとクライアントはライブストリームが終了した事に気づかず、無限ループになってしまう
                    for client in livestream.clients:
                        if client is not None:
                            client.queue.put(None)

                    # この時点で全てのクライアントの接続が切断されているので、クライアントが入るリストをクリア
                    livestream.clients = list()

                    # ループを抜ける
                    break

        # スレッドプール上で非同期に実行する
        asyncio.create_task(asyncio.to_thread(writer))

        # ***** エンコーダーの出力監視と制御 *****

        # エンコード終了後にエンコードタスクを再起動すべきかのフラグ
        is_restart_required:bool = False

        # エンコーダーのログ出力が同期的なので、同期関数をマルチスレッドで実行する
        def controller():

            # メインスレッドのイベントループを取得
            from app.app import loop

            # 1つ上のスコープ (Enclosing Scope) の変数を書き替えるために必要
            # ref: https://excel-ubara.com/python/python014.html#sec04
            nonlocal program_present, is_restart_required

            # エンコードが停止している際のタイムスタンプ
            ## できるだけエンコーダー側のエラーメッセージを拾ってエラー表示したいので、
            ## すぐにはエンコードタスクを再起動せずに1秒待つためのもの
            encoder_terminated_at = None

            # エンコーダーの出力結果を取得
            line:str = str()  # 出力行
            lines:list = list()  # 出力行のリスト
            linebuffer:bytes = bytes()  # 出力行のバッファ
            while True:

                # ライブストリームのステータスを取得
                livestream_status = livestream.getStatus()

                # 1バイトずつ読み込む
                buffer:bytes = encoder.stderr.read(1)
                if buffer:  # データがあれば

                    # 行バッファに追加
                    linebuffer = linebuffer + buffer

                    # 画面更新 or 改行があれば
                    linebreak = b'\r' if os.name == 'nt' else b'\n'
                    if (b'\r' in buffer) or (linebreak in buffer):

                        # 行（文字列）を取得
                        try:
                            # 余計な改行や空白を削除
                            # インデントが消えるので見栄えは悪いけど、プログラムで扱う分にはちょうどいい
                            line = linebuffer.decode('utf-8').strip()
                        # UnicodeDecodeError は握りつぶす（どっちみちチャンネル名とか解読できないし）
                        except UnicodeDecodeError:
                            pass

                        # リストに追加
                        # 山ほど出力されるメッセージは除外
                        if ('Delay between the first packet and last packet in the muxing queue' not in line and
                            'removing 2 bytes from input bitstream not read by decoder.' not in line):
                            lines.append(line)

                        # 行バッファを消去
                        linebuffer = bytes()

                        # ストリーム関連のログを表示
                        if 'Stream #0:' in line:
                            Logging.debug_simple(line)

                        # エンコードの進捗を判定し、ステータスを更新する
                        # 誤作動防止のため、ステータスが Standby の間のみ更新できるようにする
                        if livestream_status['status'] == 'Standby':
                            # FFmpeg
                            if encoder_type == 'FFmpeg':
                                if 'arib parser was created' in line or 'Invalid frame dimensions 0x0.' in line:
                                    livestream.setStatus('Standby', 'エンコードを開始しています…')
                                elif 'frame=    1 fps=0.0 q=0.0' in line or 'size=       0kB time=00:00' in line:
                                    livestream.setStatus('Standby', 'バッファリングしています…')
                                elif 'frame=' in line or 'bitrate=' in line:
                                    livestream.setStatus('ONAir', 'ライブストリームは ONAir です。')
                            ## HWEncC
                            elif encoder_type == 'QSVEncC' or encoder_type == 'NVEncC' or encoder_type == 'VCEEncC':
                                if 'opened file "pipe:0"' in line:
                                    livestream.setStatus('Standby', 'エンコードを開始しています…')
                                elif 'starting output thread...' in line:
                                    livestream.setStatus('Standby', 'バッファリングしています…')
                                elif 'Encode Thread:' in line:
                                    livestream.setStatus('Standby', 'バッファリングしています…')
                                elif ' frames: ' in line:
                                    livestream.setStatus('ONAir', 'ライブストリームは ONAir です。')

                        # 特定のエラーログが出力されている場合は回復が見込めないため、エンコーダーを終了する
                        # エンコーダーを再起動することで回復が期待できる場合は、ステータスを Restart に設定しエンコードタスクを再起動する
                        ## FFmpeg
                        if encoder_type == 'FFmpeg':
                            if 'Stream map \'0:v:0\' matches no streams.' in line:
                                # 何らかの要因で tsreadex から放送波が受信できなかったことによるエラーのため、エンコーダーの再起動は行わない
                                ## 番組名に「放送休止」などが入っていれば停波によるものとみなし、そうでないなら放送波の受信に失敗したものとする
                                if (('放送休止' in program_present.title) or
                                    ('放送終了' in program_present.title) or
                                    ('休止' in program_present.title) or
                                    ('停波' in program_present.title)):
                                    livestream.setStatus('Offline', 'この時間は放送を休止しています。')
                                else:
                                    livestream.setStatus('Offline', 'チューナーからの放送波の受信に失敗したため、エンコードを開始できません。')
                                break
                            elif 'Conversion failed!' in line:
                                # 捕捉されないエラー
                                is_restart_required = True  # エンコーダーの再起動を要求
                                if self.retry_count < self.max_retry_count:  # リトライの制限内であれば
                                    livestream.setStatus('Restart', 'エンコード中に予期しないエラーが発生しました。エンコードタスクを再起動します。')
                                # 直近 50 件のログを表示
                                for log in lines[-51:-1]:
                                    Logging.warning(log)
                                break
                        ## HWEncC
                        elif encoder_type == 'QSVEncC' or encoder_type == 'NVEncC' or encoder_type == 'VCEEncC':
                            if 'error finding stream information.' in line:
                                # 何らかの要因で tsreadex から放送波が受信できなかったことによるエラーのため、エンコーダーの再起動は行わない
                                ## 番組名に「放送休止」などが入っていれば停波によるものとみなし、そうでないなら放送波の受信に失敗したものとする
                                if (('放送休止' in program_present.title) or
                                    ('放送終了' in program_present.title) or
                                    ('休止' in program_present.title) or
                                    ('停波' in program_present.title)):
                                    livestream.setStatus('Offline', 'この時間は放送を休止しています。')
                                else:
                                    livestream.setStatus('Offline', 'チューナーからの放送波の受信に失敗したため、エンコードを開始できません。')
                                break
                            elif 'due to the NVIDIA\'s driver limitation.' in line:
                                # NVEncC で、同時にエンコードできるセッション数 (Geforceだと3つ) を全て使い果たしている時のエラー
                                livestream.setStatus('Offline', 'NVENC のエンコードセッションが不足しているため、エンコードを開始できません。')
                                break
                            elif 'unable to decode by qsv.' in line:
                                # QSVEncC 非対応の環境
                                livestream.setStatus('Offline', 'QSVEncC 非対応の環境のため、エンコードを開始できません。')
                                break
                            elif 'CUDA not available.' in line:
                                # NVEncC 非対応の環境
                                livestream.setStatus('Offline', 'NVEncC 非対応の環境のため、エンコードを開始できません。')
                                break
                            elif 'Failed to initalize VCE factory:' in line:
                                # VCEEncC 非対応の環境
                                livestream.setStatus('Offline', 'VCEEncC 非対応の環境のため、エンコードを開始できません。')
                                break
                            elif 'Consider increasing the value for the --input-analyze and/or --input-probesize!' in line:
                                # --input-probesize or --input-analyze の期間内に入力ストリームの解析が終わらなかった
                                is_restart_required = True  # エンコーダーの再起動を要求
                                if self.retry_count < self.max_retry_count:  # リトライの制限内であれば
                                    livestream.setStatus('Restart', '入力ストリームの解析に失敗しました。エンコードタスクを再起動します。')
                                break
                            elif 'finished with error!' in line:
                                # 捕捉されないエラー
                                is_restart_required = True  # エンコーダーの再起動を要求
                                if self.retry_count < self.max_retry_count:  # リトライの制限内であれば
                                    livestream.setStatus('Restart', 'エンコード中に予期しないエラーが発生しました。エンコードタスクを再起動します。')
                                # 直近 150 件のログを表示
                                for log in lines[-151:-1]:
                                    Logging.warning(log)
                                break

                # 現在放送中の番組が終了した時
                if program_present is not None and time.time() > program_present.end_time.timestamp():

                    # 次の番組情報を取得する
                    # メインスレッドのイベントループで実行（そうしないとうまく動作しない）
                    program_following:Programs = asyncio.run_coroutine_threadsafe(channel.getCurrentAndNextProgram(), loop).result()[0]

                    # 次の番組が None でない
                    if program_following is not None:

                        # 次の番組のタイトルを表示
                        ## TODO: 番組の解像度が変わった際にエンコーダーがクラッシュorフリーズする可能性があるが、
                        ## その場合はここでエンコードタスクを再起動させる必要があるかも
                        Logging.info(f'LiveStream:{livestream.livestream_id} Title:{program_following.title}')

                    # 次の番組情報を現在の番組情報にコピー
                    program_present = program_following
                    del program_following

                # 現在 ONAir でかつクライアント数が 0 なら Idling（アイドリング状態）に移行
                if livestream_status['status'] == 'ONAir' and livestream_status['clients_count'] == 0:
                    livestream.setStatus('Idling', 'ライブストリームは Idling です。')

                # 現在 Idling でかつ最終更新から指定された秒数以上経っていたらエンコーダーを終了し、Offline 状態に移行
                if ((livestream_status['status'] == 'Idling') and
                    (time.time() - livestream_status['updated_at'] > CONFIG['livestream']['max_alive_time'])):
                    livestream.setStatus('Offline', 'ライブストリームは Offline です。')
                    break

                # すでに Restart 状態になっている場合、エンコーダーを終了する
                # エンコードタスク以外から Restart 状態に設定される事は今のところないが、念のため
                if livestream_status['status'] == 'Restart':
                    is_restart_required = True  # エンコーダーの再起動を要求
                    break

                # すでに Offline 状態になっている場合、エンコーダーを終了する
                # エンコードタスク以外から Offline 状態に設定される事も考えられるため
                if livestream_status['status'] == 'Offline':
                    break

                # 現在 ONAir でかつストリームデータの最終書き込み時刻から 5 秒以上が経過しているなら、エンコーダーがフリーズしたものとみなす
                # 現在 Standby でかつストリームデータの最終書き込み時刻から 20 秒以上が経過している場合も、エンコーダーがフリーズしたものとみなす
                ## 何らかの理由でエンコードが途中で停止した場合、livestream.write() が実行されなくなるのを利用する
                if ((livestream_status['status'] == 'ONAir' and time.time() - livestream.stream_data_written_at > 5) or
                    (livestream_status['status'] == 'Standby' and time.time() - livestream.stream_data_written_at > 20)):
                    ## できるだけエンコーダーのエラーメッセージを拾ってエラー表示したいので、1秒間実行を待機する
                    ## その間にエラーメッセージが終了判定に引っかかった場合はそのまま終了する
                    encoder_terminated_at = time.time()

                # エンコーダーがフリーズした時刻から1秒経っていたら
                ## ステータスを Restart に設定し、エンコードタスクを再起動する
                if encoder_terminated_at is not None and time.time() - encoder_terminated_at > 1:
                    is_restart_required = True  # エンコーダーの再起動を要求
                    if self.retry_count < self.max_retry_count:  # リトライの制限内であれば
                        Logging.debug('Detects encoder termination (Controller thread).')
                        livestream.setStatus('Restart', 'エンコードが途中で停止しました。エンコードタスクを再起動します。')
                    if encoder_type == 'FFmpeg':
                        # 直近 50 件のログを表示
                        for log in lines[-51:-1]:
                            Logging.warning(log)
                        break
                    # HWEncC ではログを詳細にハンドリングするためにログレベルを debug に設定しているため、FFmpeg よりもログが圧倒的に多い
                    elif encoder_type == 'QSVEncC' or encoder_type == 'NVEncC' or encoder_type == 'VCEEncC':
                        # 直近 150 件のログを表示
                        for log in lines[-151:-1]:
                            Logging.warning(log)
                        break

                # エンコーダーが意図せず終了した場合、エンコーダーを（明示的に）終了する
                if not buffer and encoder.poll() is not None:
                    # エンコーダーの再起動を要求
                    is_restart_required = True
                    # エンコーダーの再起動前提のため、あえて Offline にはせず Restart とする
                    if self.retry_count < self.max_retry_count:  # リトライの制限内であれば
                        livestream.setStatus('Restart', 'エンコーダーが強制終了されました。エンコードタスクを再起動します。')
                    if encoder_type == 'FFmpeg':
                        # 直近 50 件のログを表示
                        for log in lines[-51:-1]:
                            Logging.warning(log)
                        break
                    # HWEncC ではログを詳細にハンドリングするためにログレベルを debug に設定しているため、FFmpeg よりもログが圧倒的に多い
                    elif encoder_type == 'QSVEncC' or encoder_type == 'NVEncC' or encoder_type == 'VCEEncC':
                        # 直近 150 件のログを表示
                        for log in lines[-151:-1]:
                            Logging.warning(log)
                        break
                    break

        # スレッドプール上で実行し、終了するのを待つ
        await asyncio.to_thread(controller)

        # ***** エンコード終了後の処理 *****

        # 明示的にプロセスを終了する
        tsreadex.kill()
        encoder.kill()

        # エンコードタスクを再起動する（エンコーダーの再起動が必要な場合）
        if is_restart_required is True:

            # チューナーをアンロックする（ EDCB バックエンドのみ）
            # 新しいエンコードタスクが今回立ち上げたチューナーを再利用できるようにする
            # エンコーダーの再起動が必要なだけでチューナー自体はそのまま使えるし、わざわざ閉じてからもう一度開くのは無駄
            if CONFIG['general']['backend'] == 'EDCB':
                tuner.unlock()

            # 最大再起動回数が 0 より上であれば
            if self.retry_count < self.max_retry_count:
                self.retry_count += 1  # カウントを増やす
                await asyncio.sleep(0.1)  # 少し待つ
                asyncio.create_task(self.run(channel_id, quality))  # 新しいタスクを立ち上げる

            # 最大再起動回数を使い果たしたので、Offline にする
            else:

                # Offline に設定
                if program_present.is_free == True:  # 無料放送時
                    livestream.setStatus('Offline', 'ライブストリームの再起動に失敗しました。')
                else:  # 有料放送時（契約されていないため視聴できないことが原因の可能性が高い）
                    livestream.setStatus('Offline', 'ライブストリームの再起動に失敗しました。契約されていないため視聴できません。')

                # チューナーを終了する（ EDCB バックエンドのみ）
                if CONFIG['general']['backend'] == 'EDCB':
                    await tuner.close()

        # 通常終了
        else:

            # EDCB バックエンドのみ
            if CONFIG['general']['backend'] == 'EDCB':

                # チャンネル切り替え時にチューナーが再利用されるように、3秒ほど待つ
                # 3秒間の間にチューナーの制御権限が新しいエンコードタスクに委譲されれば、下記の通り実際にチューナーが閉じられることはない
                await asyncio.sleep(3)

                # チューナーを終了する（まだ制御を他のチューナーインスタンスに委譲していない場合）
                # Idling に移行しアンロック状態になっている間にチューナーが再利用された場合、制御権限をもう持っていないため実際には何も起こらない
                await tuner.close()

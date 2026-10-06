# 分片 MP4 修复 HTTP 服务（fmp4-recover）

接收一个**未加密** `init.mp4` 与若干**乱序 / 重复 / 缺片 / 尾片截断**的 fMP4
分片，只支持 **H.264 视频**与 **AAC 音频**，在**不转码**的前提下还原原始录制
时间轴，输出可被 `ffprobe` 正常读取的 MP4，并逐轨报告缺口、重复与舍弃区间。

- 后端：Python 3.11 + FastAPI + Uvicorn
- 校验/探测：FFmpeg 套件中的 `ffprobe`（修复过程本身**不调用 ffmpeg 转码**，
  样本字节原样复制）
- 容器：Docker / Docker Compose 一键启动

---

## 1. 它做了什么

1. 解析 `init.mp4`：`ftyp / moov / mvex / trex`、每轨 `tkhd / mdhd / hdlr /
   stsd(avcC, esds) / edts(elst)`，确认未加密且编码为 H.264 + AAC。
2. 解析每个分片：支持 `styp` 头、单个或多个 `moof/mdat`、**一个 moof 内多
   traf 音视频交织**、tfhd `base-data-offset` / `default-base-is-moof` 两种
   基址写法。
3. 结合 **trex/tfhd 默认值、tfdt、trun 逐样本 duration/size/flags/composition
   time offset**，还原每个样本的 DTS 与显示偏移（PTS = DTS + CTS），从而保证
   **B 帧重排**与分轨片段的音画同步。
4. 按每轨自身 `timescale` 精确换算为有理数秒（`Fraction`，不引入浮点误差），
   作为统一时间轴比较与交错输出的依据；edit list 原样保留并在响应中列出。
5. 逐轨排序后：
   - **去重**：同轨两个片段的 DTS 解码区间相同**且样本字节（SHA-256）相同**
     才丢弃后者；
   - **冲突重叠**：区间重叠但内容不同（或区间相同字节不同）→ `409`；
   - **缺口**：如实报告，**绝不压缩后续时间戳**——后续片段的 `tfdt` 保持原值，
     播放器在缺口处表现为停顿/静音；
   - **尾片截断**：只保留完整样本，被截断的样本及其后区间在 `dropped_tail`
     中报告。
6. 重新构造规范化的 `moof/traf(tfhd/tfdt/trun)+mdat`，样本数据逐字节复制，
   输出前用 `ffprobe` 校验；校验失败**不会留下任何下载文件**。

输出文件的时间轴与原录制一致：例如下文示例中视频首帧 `start_time=0.066667`
（B 帧重排延迟）、音频从 `0.000000` 开始。

---

## 2. Docker Compose 一键启动

```bash
docker compose up --build -d
```

服务监听在宿主机 `http://localhost:8000`：

```bash
curl http://localhost:8000/healthz
# {"ok":true}
```

修复结果保存在命名卷 `fmp4-jobs`（容器内 `/data/jobs`）。
停止服务：

```bash
docker compose down            # 保留已生成文件
docker compose down -v         # 连同结果卷一起删除
```

### 不用容器，本地运行

```bash
pip install -r requirements.txt
# 需要 PATH 中存在 ffprobe/ffmpeg（或用 FFPROBE_BIN 指定绝对路径）
export RECOVER_JOBS_DIR=/tmp/fmp4-recover/jobs
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RECOVER_JOBS_DIR` | `/tmp/fmp4-recover/jobs` | 修复结果输出目录（原子 rename 写入） |
| `FFPROBE_BIN` | `ffprobe` | ffprobe 可执行文件路径 |
| `FFMPEG_BIN` | `ffmpeg` | ffmpeg 可执行文件路径（当前仅留作扩展） |

---

## 3. HTTP API

### `POST /recover`

`multipart/form-data`：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `init` | 文件 | 是 | `init.mp4`（ftyp+moov，未加密） |
| `pieces` | 文件（可重复多个） | 是 | fMP4 分片，顺序任意，可重复，可尾部截断 |
| `base_url_override` | 字符串 | 否 | 自定义返回的下载地址前缀（反向代理场景） |

成功返回 `200`，JSON 关键字段：

- `job_id` / `download_url`：结果任务号与下载地址；
- `movie_timescale`：movie 时基；
- `timeline.duration_seconds`：统一时间轴上的媒体时长；
- `timeline.edit_list_notes`：edit list 在 movie/媒体两种时基下的映射；
- `tracks[]`：逐轨信息，含
  - `codec / timescale / width / height / sample_rate / channels`；
  - `received_fragments / kept_fragments / duplicates_removed / samples / bytes`；
  - `media_decode_range`：该轨样本 DTS 覆盖区间（轨道时基单位 + 秒）；
  - `gaps[]`：缺口区间（`dts_from/dts_to`、秒、`leading`）；
  - `dropped_tail[]`：尾片截断舍弃区间；
  - `edit_list[]`：原始 edit list 条目；
- `output.transcoded=false`、`output.bytes` 与 `output.ffprobe`：输出大小与
  ffprobe 复核结果（编码、time_base、时长、分辨率、采样率等）。

错误：

| HTTP | code | 触发条件 |
| --- | --- | --- |
| 409 | `FRAGMENT_OVERLAP` | 同轨片段 DTS 区间重叠但内容不同（去重仅限区间+字节完全相同） |
| 422 | `UNSUPPORTED_CODEC` | 编码不是 H.264/AAC（如 HEVC、Opus、AC-3、FLAC 等） |
| 422 | `ENCRYPTED` | init 或分片含 `saiz/saio/senc/pssh` 等加密 box |
| 422 | `INVALID_INIT` / `INVALID_FRAGMENT` | box 结构缺失或截断到无法定位 moof |
| 422 | `UNKNOWN_SAMPLE_BOUNDARY` | 无法确定样本边界（size 未知/为 0，或样本越界且 mdat 未截断） |
| 422 | `UNKNOWN_TRACK` | 分片引用了 init 中不存在的 track |
| 422 | `NO_USABLE_FRAGMENT` | 所有分片都没有完整样本 |
| 500 | `FFPROBE_FAILED` | 生成结果无法被 ffprobe 读取（此时不保留下载文件） |

### `GET /download/{job_id}`

下载修复后的 MP4（`Content-Type: video/mp4`、支持 Range）。任务号不存在返回
`404`。

---

## 4. 实际请求 / 响应示例

以下示例由仓库内测试素材实际运行产生（测试素材 6 秒：640x360@30fps、
High profile、**带 B 帧**的 H.264 + 48kHz/双声道 AAC；视频 timescale=15360，
音频 timescale=48000）。素材中：

- 视频第 3 个分片（2.0s–3.0s）**缺失**；
- 最后一个视频分片尾部被砍掉 300 字节（末样本不完整）；
- 第 1 个视频分片**重复上传**一次；
- 所有分片**乱序**上传（倒序音频、奇偶交错视频）。

生成测试素材（需要 ffmpeg）：

```bash
python3 tests/gen_fixtures.py
# 产物：tests/fixtures/init.mp4, tests/fixtures/{good,overlap,badcodec}/...
#       tests/fixtures_interleaved/（moov 带初始 mdat + moof 多 traf 交织）
```

### 4.1 上传分片（乱序 / 重复 / 缺片 / 尾截断）

```bash
JOB_DIR=tests/fixtures/good
curl -s -X POST http://localhost:8000/recover \
  -F "init=@tests/fixtures/init.mp4;type=video/mp4" \
  -F "pieces=@$JOB_DIR/v-005.m4s;type=video/iso.segment" \
  -F "pieces=@$JOB_DIR/v-003.m4s;type=video/iso.segment" \
  -F "pieces=@$JOB_DIR/v-000.m4s;type=video/iso.segment" \
  -F "pieces=@$JOB_DIR/a-006.m4s;type=video/iso.segment" \
  -F "pieces=@$JOB_DIR/a-005.m4s;type=video/iso.segment" \
  -F "pieces=@$JOB_DIR/a-004.m4s;type=video/iso.segment" \
  -F "pieces=@$JOB_DIR/a-003.m4s;type=video/iso.segment" \
  -F "pieces=@$JOB_DIR/a-002.m4s;type=video/iso.segment" \
  -F "pieces=@$JOB_DIR/a-001.m4s;type=video/iso.segment" \
  -F "pieces=@$JOB_DIR/a-000.m4s;type=video/iso.segment" \
  -F "pieces=@$JOB_DIR/v-004.m4s;type=video/iso.segment" \
  -F "pieces=@$JOB_DIR/v-001.m4s;type=video/iso.segment" \
  -F "pieces=@$JOB_DIR/v-000-dup.m4s;type=video/iso.segment"
```

> `job_id` 每次不同，下面的 `9d204a19...` 只是本次运行的真实返回值。

`200 OK` 响应（节选，数值与上面的请求一一对应）：

```json
{
  "job_id": "9d204a19adf74b498e5fe36b0e0f365b",
  "status": "ok",
  "download_url": "http://localhost:8000/download/9d204a19adf74b498e5fe36b0e0f365b",
  "input": {
    "init": "init.mp4",
    "pieces": ["v-005.m4s", "v-003.m4s", "v-000.m4s", "a-006.m4s",
               "a-005.m4s", "a-004.m4s", "a-003.m4s", "a-002.m4s",
               "a-001.m4s", "a-000.m4s", "v-004.m4s", "v-001.m4s",
               "v-000-dup.m4s"],
    "piece_count": 13
  },
  "movie_timescale": 1000,
  "timeline": {
    "duration_seconds": 6.066666666666666,
    "gap_policy": "keep",
    "note": "缺口时间轴原样保留（后续样本时间戳不压缩），播放器在缺口处表现为卡顿/静音。",
    "edit_list_notes": []
  },
  "tracks": [
    {
      "track_id": 1,
      "kind": "video",
      "codec": "h264",
      "timescale": 15360,
      "width": 640,
      "height": 360,
      "received_fragments": 6,
      "kept_fragments": 5,
      "duplicates_removed": 1,
      "samples": 149,
      "bytes": 513414,
      "media_decode_range": {
        "dts_from": 0, "dts_to": 91648,
        "seconds_from": 0.0, "seconds_to": 5.966666666666667,
        "duration_seconds": 5.966666666666667
      },
      "gaps": [
        {
          "track_id": 1,
          "dts_from": 30720, "dts_to": 46080,
          "seconds_from": 2.0, "seconds_to": 3.0,
          "duration_seconds": 1.0,
          "leading": false
        }
      ],
      "edit_list": [],
      "dropped_tail": [
        {
          "track_id": 1,
          "reason": "tail_sample_truncated",
          "dts_from": 91648, "dts_to": 92160,
          "seconds_from": 5.966666666666667, "seconds_to": 6.0
        }
      ]
    },
    {
      "track_id": 2,
      "kind": "audio",
      "codec": "aac",
      "timescale": 48000,
      "sample_rate": 48000,
      "channels": 2,
      "received_fragments": 7,
      "kept_fragments": 7,
      "duplicates_removed": 0,
      "samples": 283,
      "bytes": 48114,
      "media_decode_range": {
        "dts_from": 0, "dts_to": 291200,
        "seconds_from": 0.0, "seconds_to": 6.066666666666666,
        "duration_seconds": 6.066666666666666
      },
      "gaps": [],
      "edit_list": [],
      "dropped_tail": []
    }
  ],
  "output": {
    "transcoded": false,
    "bytes": 569814,
    "ffprobe": {
      "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
      "duration_seconds": 6.066667,
      "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264",
         "time_base": "1/15360", "duration_seconds": 5.966667,
         "width": 640, "height": 360},
        {"index": 1, "codec_type": "audio", "codec_name": "aac",
         "time_base": "1/48000", "duration_seconds": 6.066667,
         "sample_rate": "48000", "channels": 2}
      ]
    }
  }
}
```

说明：

- 视频收到 6 片（含 1 个重复片），保留 5 片，去重 1；缺口恰为
  `[2.0s, 3.0s)`；尾片舍弃 `[5.9667s, 6.0s)`（最后一个不完整样本）。
- 缺口之后的视频片段 `tfdt` 仍是 46080（3.0s），没有被前移压缩；ffprobe
  实测帧时间戳在 2.033s 后直接跳到 3.066s。
- 视频 149 帧 = 150（6×30fps）− 30（缺口）− 1（尾截断）；B 帧的
  composition time offset 逐样本保留，显示顺序为 I/B/B/P…，视频首帧显示
  时间 0.066667s，与原录制一致。

### 4.2 下载修复结果

```bash
curl -o recovered.mp4 http://localhost:8000/download/9d204a19adf74b498e5fe36b0e0f365b
```

响应头：

```http
HTTP/1.1 200 OK
content-type: video/mp4
accept-ranges: bytes
content-disposition: attachment; filename="recovered-9d204a19adf74b498e5fe36b0e0f365b.mp4"
content-length: 569814
```

独立校验（时间轴与原录制一致，无转码）：

```bash
ffprobe -v error -show_entries stream=codec_name,start_time,duration \
  -of compact recovered.mp4
# stream|codec_name=h264|start_time=0.066667|duration=5.966667
# stream|codec_name=aac|start_time=0.000000|duration=6.066667
```

### 4.3 同区间内容不同 → `409`

`tests/fixtures/overlap/` 中 `v-001.m4s` 与 `v-001-other.m4s` 的 DTS 区间完全
相同，但 mdat 中翻转了一个字节：

```bash
curl -s -X POST http://localhost:8000/recover \
  -F "init=@tests/fixtures/init.mp4;type=video/mp4" \
  -F "pieces=@tests/fixtures/overlap/v-000.m4s;type=video/iso.segment" \
  -F "pieces=@tests/fixtures/overlap/v-001.m4s;type=video/iso.segment" \
  -F "pieces=@tests/fixtures/overlap/v-001-other.m4s;type=video/iso.segment"
```

```http
HTTP/1.1 409 Conflict
```

```json
{
  "error": {
    "code": "FRAGMENT_OVERLAP",
    "message": "track 1 在 DTS [15360,30720) 存在内容不同的重叠片段（去重仅限区间与字节完全相同）",
    "details": {
      "overlaps": [
        {
          "track_id": 1,
          "dts_from": 15360, "dts_to": 30720,
          "seconds_from": 1.0, "seconds_to": 2.0,
          "fragment_a": {"source": "v-001-other.m4s", "seq": 2,
                         "dts_start": 15360, "dts_end": 30720},
          "fragment_b": {"source": "v-001.m4s", "seq": 2,
                         "dts_start": 15360, "dts_end": 30720}
        }
      ]
    }
  }
}
```

### 4.4 编码不支持 → `422`

用 HEVC 编码生成的 init 请求修复：

```bash
curl -s -X POST http://localhost:8000/recover \
  -F "init=@tests/fixtures/badcodec/init.mp4;type=video/mp4" \
  -F "pieces=@tests/fixtures/good/a-000.m4s;type=video/iso.segment"
```

```http
HTTP/1.1 422 Unprocessable Entity
```

```json
{
  "error": {
    "code": "UNSUPPORTED_CODEC",
    "message": "不支持的编码 H.265/HEVC，仅支持 H.264 与 AAC",
    "details": {}
  }
}
```

### 4.5 交织 moof（moov 带初始 mdat）场景

部分录制管线（非 `empty_moov`）会把首批样本放在 moov 之后的普通 `mdat` 中，
且每个 `moof` 内含视频/音频两个 `traf`，数据在共享 `mdat` 里交织，tfhd 使用
绝对 `base-data-offset`。`tests/fixtures_interleaved/` 覆盖该布局；把各 chunk
乱序并重复上传后，输出经 ffprobe 验证为 **50 视频帧 / 88 音频帧**，与原录制
逐帧一致，视频起点保持 0.08s 的 B 帧显示延迟。

---

## 5. 运行测试

```bash
# 1) 生成 ffmpeg 测试素材
python3 tests/gen_fixtures.py

# 2) 核心逻辑测试（纯 Python + ffprobe 校验，无需起服务）
python3 tests/test_core.py
```

覆盖：init 解析（分辨率/采样率/timescale）、B 帧 CTS 保留、乱序排序、区间+
字节双重去重、缺口不压缩、尾截断舍弃、409 重叠、422 编码/样本边界、交织
moof、初始 mdat 覆盖、帧数与音画同步。

---

## 6. 项目结构

```
app/
  mp4box.py   ISO-BMFF box 解析/构造（tfhd/tfdt/trun/trex/elst/mdhd/stsd…）
  fmp4.py     init/分片解析、默认值合并、DTS/CTS 还原、去重/重叠/缺口、重建
  main.py     FastAPI 路由、ffprobe 校验、任务文件原子落盘与下载
tests/
  gen_fixtures.py   用 ffmpeg 生成乱序/重复/缺片/截断/重叠/HEVC/交织素材
  test_core.py      端到端核心测试
Dockerfile
docker-compose.yml
requirements.txt
```

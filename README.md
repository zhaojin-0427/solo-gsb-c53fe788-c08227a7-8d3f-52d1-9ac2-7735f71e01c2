# fMP4 Recovery Service（分片 MP4 修复 HTTP API）

把一个**未加密的 `init.mp4`** 和一批**乱序、重复、缺片或尾片被截断**的
fragmented MP4（fMP4，`.m4s`/fMP4 HLS/DASH 分片）修复为一个完整、可直接播放、
**无转码**的 MP4。

- 仅支持 **H.264（AVC）视频** 与 **AAC 音频**（其他编码/加密流返回 `422`）
- 保留原始 `moov`（含 **edit list**、trex 默认值）
- 按 **tfhd / tfdt / trun + trex 默认值**逐样本还原 DTS 与 B 帧显示偏移（CTO）
- 每轨使用各自的 `timescale`，跨轨按精确分数统一时基
- 同一轨道**解码区间相同且样本字节相同** → 自动去重；区间重叠但内容不同 → `409`
- **缺口不会压缩后续时间轴**：后续分片保持原始 `tfdt`，缺口呈现为黑帧/静音区间
- 尾片截断时只保留**完整样本**，并在 `discarded_ranges` 中报告舍弃区间
- 输出经 `ffprobe` 校验成功后才提供下载；任何失败都不会留下下载文件

---

## 1. Docker Compose 一键启动

```bash
docker compose up -d --build
```

服务监听 `http://localhost:8000`，容器内含 `ffmpeg/ffprobe`（仅用 ffprobe 做
输出校验，不做转码）。结果文件保存在命名卷 `fmp4-data`（容器内 `/data`），
默认保留 1 小时，可通过环境变量调整：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RECOVER_STORE` | `/data` | 修复结果存放目录 |
| `RECOVER_TTL_SECONDS` | `3600` | 结果保留秒数，过期由后台任务清理 |

健康检查：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

交互式 API 文档：<http://localhost:8000/docs>（Swagger UI）。

### 本地（不用 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# 需要本机有 ffprobe（ffmpeg 包）
RECOVER_STORE=./data uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 2. API

### `POST /recover`

`multipart/form-data`：

| 字段 | 数量 | 说明 |
| --- | --- | --- |
| `init` | 1 | 未加密 fMP4 初始化分片（含 `ftyp`/`moov`、`mvex/trex`） |
| `segments` | 1..N | 媒体分片，顺序任意，可重复、可缺片、可有尾片截断 |

成功返回 `200`（JSON），其中含逐轨缺口报告与 `download_url`；随后用
`GET /download/{job_id}` 下载修复后的 MP4（`video/mp4`）。

错误状态码：

| 状态码 | 场景 |
| --- | --- |
| `400` | init 为空/不像初始化分片、未提供任何分片 |
| `409` | 同轨分片解码区间重叠，但样本字节不同（无法自动取舍） |
| `422` | 编码非 H.264/AAC、CENC 加密、引用未知 track、缺少 moof/mdat、尾片之外截断导致无法确定样本边界、重建结果 ffprobe 校验失败 |
| `404` | 下载未知或已过期的 job |

---

## 3. 实际 HTTP 请求 / 响应示例

以下示例用 ffmpeg 7.0 生成（1280x720@30fps、带 B 帧的 H.264 + 48kHz 立体声
AAC，每秒一个 fMP4 分片，共 6 个）：

```bash
ffmpeg -f lavfi -i "testsrc2=size=1280x720:rate=30:duration=6" \
       -f lavfi -i "sine=frequency=440:duration=6" \
       -map 0:v:0 -map 1:a:0 -c:v libx264 -pix_fmt yuv420p \
       -g 30 -keyint_min 30 -bf 2 -c:a aac -ar 48000 -ac 2 \
       -f hls -hls_time 1 -hls_playlist_type vod -hls_list_size 0 \
       -hls_segment_type fmp4 -hls_fmp4_init_filename init.mp4 \
       -hls_segment_filename "chunk-%03d.m4s" playlist.m3u8
```

### 3.1 乱序 + 重复 + 缺片 → 200

上传顺序为 `3 0 5 1 1 4`（缺第 2 片 `chunk-002`，`chunk-001` 传了两次）：

```bash
curl -s -X POST http://localhost:8000/recover \
  -F "init=@init.mp4;type=video/mp4" \
  -F "segments=@chunk-003.m4s" \
  -F "segments=@chunk-000.m4s" \
  -F "segments=@chunk-005.m4s" \
  -F "segments=@chunk-001.m4s" \
  -F "segments=@chunk-001.m4s" \
  -F "segments=@chunk-004.m4s"
```

实际响应（节选，完整字段见实现 `app/main.py`）：

```json
{
  "job_id": "ddbb6a6e2b61444098468408e21e4da0",
  "status": "ok",
  "download_url": "http://127.0.0.1:8000/download/ddbb6a6e2b61444098468408e21e4da0",
  "output": {
    "filename": "ddbb6a6e2b61444098468408e21e4da0.mp4",
    "size_bytes": 829222,
    "transcoded": false,
    "ffprobe": {
      "format_name": "mov,mp4,m4a,3gp,3gp,3g2,mj2",
      "duration_s": "6.021333",
      "streams": [
        {"codec_type": "video", "codec_name": "h264", "start_time": "0.066016",
         "duration": "6.000000", "width": 1280, "height": 720,
         "avg_frame_rate": "30/1", "has_b_frames": 2},
        {"codec_type": "audio", "codec_name": "aac", "start_time": "0.045000",
         "duration": "6.021333", "sample_rate": "48000", "channels": 2}
      ]
    }
  },
  "timeline": {
    "movie_timescale": 1000,
    "max_track_end_ms": 6021.333,
    "note": "Original tfdt decode timestamps are preserved; gaps remain as silent/black intervals and do not shift later media."
  },
  "tracks": [
    {
      "track_id": 1, "type": "video", "codec": "h264", "timescale": 15360,
      "width": 1280.0, "height": 720.0,
      "edit_list": [
        {"segment_duration_ms": 66.0, "media_time": -1, "media_rate": 1.0},
        {"segment_duration_ms": null, "media_time": 1024,
         "media_time_ms": 66.667, "media_rate": 1.0}
      ],
      "fragments_kept": 5, "samples_kept": 150,
      "decode_start_dts": 0, "decode_end_dts": 92160,
      "decode_range_ms": [0.0, 6000.0],
      "gaps": [
        {"from_dts": 30720, "to_dts": 46080,
         "from_ms": 2000.0, "to_ms": 3000.0, "duration_ms": 1000.0}
      ]
    },
    {
      "track_id": 2, "type": "audio", "codec": "aac",
      "timescale": 48000, "channels": 2, "sample_rate": 48000,
      "fragments_kept": 5, "samples_kept": 236,
      "decode_range_ms": [0.0, 6021.333],
      "gaps": [
        {"from_dts": 94208, "to_dts": 142336,
         "from_ms": 1962.667, "to_ms": 2965.333, "duration_ms": 1002.667}
      ]
    }
  ],
  "fragments": {
    "source_files": 6, "input_fragments": 6,
    "output_fragments": 10, "duplicates_removed": 2
  },
  "duplicates": [
    {"track_id": 1, "source_file": 4, "other_source_file": 3,
     "from_dts": 15360, "to_dts": 30720,
     "from_ms": 1000.0, "to_ms": 2000.0,
     "fingerprint": "d71037b144c5e3eb"},
    {"track_id": 2, "source_file": 4, "other_source_file": 3,
     "from_dts": 46080, "to_dts": 94208,
     "from_ms": 960.0, "to_ms": 1962.667,
     "fingerprint": "5ddb975b52ff9561"}
  ],
  "discarded_ranges": []
}
```

> 缺口虽然让有效数据只剩约 5 秒，但输出总时长仍是 **6.021s**——缺失的第 3 秒
> 以时间轴空洞保留，后续样本时间戳没有前移。

### 3.2 下载结果

```bash
JOB=ddbb6a6e2b61444098468408e21e4da0
curl -s -OJ http://localhost:8000/download/$JOB
```

实际响应头：

```http
HTTP/1.1 200 OK
content-type: video/mp4
content-disposition: attachment; filename="recovered.mp4"
content-length: 829222
```

```bash
ffprobe -v error -show_entries format=duration \
  -show_entries stream=codec_name,start_time,duration -of compact recovered.mp4
# stream|codec_name=h264|start_time=0.066016|duration=6.000000
# stream|codec_name=aac|start_time=0.045000|duration=6.021333
# format|duration=6.021333
```

### 3.3 尾片截断 → 200（保留完整样本 + 舍弃区间）

把最后一片末尾截掉 40000 字节后照常上传，响应中的 `discarded_ranges`：

```json
[
  {"track_id": 1, "source_file": 5,
   "from_dts": 89088, "to_dts": 92160,
   "from_ms": 5800.0, "to_ms": 6000.0,
   "sample_count": 6, "reason": "tail_fragment_truncated"},
  {"track_id": 2, "source_file": 5,
   "from_dts": 238592, "to_dts": null,
   "from_ms": 4970.667, "to_ms": null,
   "sample_count": 50, "reason": "tail_fragment_truncated"}
]
```

`to_dts` 为 `null` 表示该尾片一个完整样本都没保留到、无法估算声明时长。
若被截断的不是尾片（后面还有同轨数据），返回 `422 truncated_middle`。

### 3.4 重叠但内容不同 → 409

把 `chunk-002` 的一个样本字节翻转（区间相同、字节不同），与原片一起上传：

```bash
curl -s -X POST http://localhost:8000/recover \
  -F "init=@init.mp4" \
  -F "segments=@chunk-000.m4s" -F "segments=@chunk-001.m4s" \
  -F "segments=@chunk-002.m4s" -F "segments=@chunk-002-tampered.m4s" \
  -F "segments=@chunk-004.m4s"
```

```http
HTTP/1.1 409 Conflict
```

```json
{
  "error": "fragment_overlap",
  "message": "fragments on the same track overlap but are not byte-identical; cannot choose between them automatically",
  "conflicts": [
    {
      "track_id": 1,
      "overlap_from_dts": 30720, "overlap_to_dts": 46080,
      "overlap_from_ms": 2000.0, "overlap_to_ms": 3000.0,
      "fragment_a": {"source_file": 3, "from_dts": 30720, "to_dts": 46080,
                     "fingerprint": "a505a6652eeb2e26"},
      "fragment_b": {"source_file": 2, "from_dts": 30720, "to_dts": 46080,
                     "fingerprint": "a550bc689bda69cf"},
      "decode_range_dts": [30720, 46080],
      "decode_range_ms": [2000.0, 3000.0]
    }
  ]
}
```

### 3.5 编码不符 / 无法解析 → 422

```bash
# 分片根本不是 fMP4
curl -i -X POST http://localhost:8000/recover \
  -F "init=@init.mp4" -F "segments=@junk.m4s"
# HTTP/1.1 422 Unprocessable Entity
```

```json
{"detail": {"error": "not_fragmented",
            "message": "segment 0 contains no fMP4 fragments (moof/mdat)"}}
```

HEVC（`hvc1`）init 返回：

```json
{"detail": {"error": "codec_unsupported",
            "message": "unsupported codec 'hvc1'; only H.264 video and AAC audio are supported"}}
```

---

## 4. 修复原理与时基处理

1. **解析 init**：遍历 `moov`，按轨读取 `tkhd/mdhd/hdlr/stsd/edts/mvex/trex`，
   校验编码（`avc1/avc3+avcC`、`mp4a+esds` 且为 AAC OTI/AOT）与 CENC（`sinf`）。
2. **解析分片**：对每个顶层 `moof`/`mdat`，逐 `traf` 读取 `mfhd/tfhd/tfdt/trun`。
   - 样本 duration/size/flags/CTO 缺失时回落到 `trex` 默认值；
   - 数据偏移以 `tfhd` 的 `base-data-offset`/`default-base-is-moof` 与
     `trun.data_offset` 共同还原；
   - `mdat` 不足时按 `trun` 声明的样本大小顺序截取，能保留几个完整样本就保留几个。
3. **逐轨排序、去重、冲突检测**：按 `(track_id, tfdt)` 排序；指纹为
   `track_id + base_dts + 每个样本(duration,size,cto,flags) + 样本字节` 的
   SHA-256。指纹相同 → 去重；区间相交且指纹不同 → `409`。
4. **缺口计算**：相邻片段若 `start_dts > 上一片 end_dts`，差值即缺口（同时给
   各轨 `timescale` 下的原始 DTS 与毫秒值）。不写任何填充样本、不改写后续
   `tfdt`，播放器按正常不连续时间轴呈现黑帧/静音。
5. **重建**：原样输出 init 的 `ftyp`+`moov`（edit list 字节保留），再把每个
   `traf` 重新封装为单轨 `moof+mdat`（`tfdt` v1 64 位、`trun` v1 带符号 CTO，
   显式 data_offset/duration/size/flags/CTO），跨轨按
   `Fraction(dts, timescale)` 精确排序交织，`mfhd` 重新编号。
6. **校验与发布**：先写临时文件，`ffprobe -show_format -show_streams` 通过后
   原子改名发布；失败即删除临时文件，不产生可下载结果。

因此 B 帧的解码顺序/显示顺序、AAC priming 与 edit list 造成的音视频起始偏移都
与原录制一致（测试中重建文件与原始字节拼接文件的每包 DTS/PTS 完全相等）。

## 5. 测试

```bash
# 需要 ffmpeg/ffprobe 在 PATH 中（用于生成带 B 帧的测试素材与校验结果）
pip install pytest
pytest -q
```

覆盖：乱序/重复去重、逐轨缺口与时间轴不压缩、B 帧 CTO、分轨单轨 moof 交织、
尾片截断保留完整样本、中部截断 422、内容冲突 409、HEVC/CENC 422、HTTP 上传下载、
失败不留文件、重建结果与原始时间轴逐包 PTS/DTS 一致。

## 目录结构

```
app/
  mp4boxes.py      # ISOBMFF box 遍历/构建
  fmp4.py          # init/moof/traf/tfdt/trun 解析与单轨 moof 重建
  recovery.py      # 排序、去重、409 冲突、缺口、截断报告、跨轨交织
  ffmpeg_probe.py  # ffprobe 输出校验
  main.py          # FastAPI 路由、结果存储与 TTL 清理
tests/             # pytest 端到端测试
Dockerfile, docker-compose.yml, requirements.txt
```

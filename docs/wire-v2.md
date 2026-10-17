# Watch Memory 密封上传协议 v2（canonical）

此文件是手表（及可选的 iPhone 备用中转）与接收器之间唯一的 v2 合同。生产不发送 Bearer、明文音频或明文 metadata，不降级 v1。默认部署由 Tailscale Funnel 或你自己的反向代理提供公网 HTTPS，转发到只监听 loopback 的入口；在 TLS 之外，应用层仍密封音频与 metadata，回执另做请求绑定认证。HTTP 可达或 health 成功本身不证明接收器身份。

## 配对与派生

配对配置（手表通过一次性码向 `POST /v2/pair/claim` 认领得到，属于凭据）：`{"version":2,"endpoint":"https://your-receiver.example","pairing_secret":"<43-char base64url string>","protocol":"watch-memory-chunks-v2"}`。App 端也接受私网/Tailscale 地址的 HTTP endpoint，但手表直传只使用 HTTPS。

pairing_secret来自32个密码学随机bytes的无padding base64url；**KDF的IKM是该字符串的UTF8 bytes，不先base64解码**。手表/iPhone 存 Keychain，接收器配置权限 600，秘密/二维码不进入工具输出、日志、源码包或公开页面。v1 配对一律拒绝，不做静默迁移。

两把32-byte key均用HKDF-SHA256：salt为UTF8 `watch-memory-v2`；encryption info为UTF8 `upload-encryption`；receipt info为UTF8 `receipt-authentication`。

## 请求

`PUT /v2/chunks/{chunk_id}`，chunk_id为canonical小写UUID。

Headers仅：`Content-Type: application/octet-stream`、明确Content-Length、`X-Watch-Challenge: {fresh lowercase UUID}`。每个新应用attempt生成新challenge和新12-byte随机nonce，即使event相同；同一background URLSession task的系统重发/恢复可以复用该attempt原wire与challenge。没有Authorization/rootsecret header。

AAD是以下字符串的精确UTF8 bytes（最后一行无尾换行）：

```
watch-memory-upload-v2
PUT
/v2/chunks/{chunk_id}
{challenge}
```

Plaintext：4-byte unsigned big-endian metadata长度 + metadata UTF8 JSON bytes + 音频bytes。

metadata至多65536 bytes；沿用原chunk schema_version=1，包括chunk_id/session_id/sequence/capture_started_at UTC/duration_s/sha256/byte_count/original_name/sample_rate_hz=16000/channels=1，以及mime、source_app/device/is_test。容器支持CAF(audio/x-caf)和AAC M4A(audio/mp4)，文件名chunk UUID加对应扩展；SHA针对原始音频，不针对ciphertext。

音频最大16777216 bytes。wire是 `nonce(12) || AES-256-GCM ciphertext || tag(16)`，总长度最大16842784 bytes。必须先验证GCM tag和AAD，才解析、检查媒体或提交。维护中的cryptography AESGCM实现，不自行实现密码原语。

## 响应认证

JSON成功必须包含schema_version=2、protocol=watch-memory-chunks-v2、stored=true、sha256/source_id、chunk_id/session_id/sequence、status accepted或already_accepted。stored只表示持久保管，不表示已转写。

所有可签名的v2响应（包括错误）保留原始JSON bytes，发送：

`X-Watch-Receipt-MAC: base64(HMAC-SHA256(receiptKey, prefix || rawJSONBytes))`

prefix的精确UTF8 bytes是：

```
watch-memory-receipt-v2
{challenge}
{lowercase SHA256 of full request wire body}
{decimal HTTP status}
```

**prefix最后有一个换行**，然后直接拼rawJSON bytes，不重新编码或排序JSON。客户端先常数时间验证MAC，再解JSON，核HTTP2xx、schema/protocol、完整event身份与所有SHA；不接受旧attempt回执、修改status、错误body或错误MAC。未收到可信完整回执则保留数据重试。

GET /health仅无私密摘要，不发送pairing_secret，不用于Keychain配对成功判定；第一个MAC正确的正式回执才验证接收端持有秘密。生产/v1一律拒绝；legacy 仅两端 loopback 且显式 fixture 模式（只用于单元测试），不用于设备。

## 幂等与保管

同event完全一致重试200，同event冲突409；不同session/sequence的相同音频SHA保独立manifest并共享canonical ASR。capture_date 由实际 capture_started_at 按接收端配置的时区（`timezone`，默认 Asia/Shanghai）换算，每个 event 保留自己的时间。no_speech消费成功不建空文档、不重复GPU。

接收具备 16 MiB 音频限制、8 个总连接、每连接 120 秒、可配置的本机原音预算（`max_local_bytes`）与 2 GiB 剩余空间保护；超出时返回 507 且不 ACK，手表保留原音稍后重试。507/非 2xx/不可信回执都不能 ACK。公开版接收器保留全部原音，不做自动回收（`gc_enabled` 必须为 false）。

## 验证层次

Swift CryptoKit与Python cryptography的确定性公开测试vector、真实HTTP加密fixture、wrong-key/bitflip/path/challenge/旧ACK/生产明文拒收是协议验收。它们不代替真机后台 URLSession、手表采集、设备安装、耗电或中断恢复验收。

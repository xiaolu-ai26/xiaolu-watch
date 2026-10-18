# 今日页、身体背景与删除传播

今日页增加一个阅读入口：自然对话卡、卡片摘要、当天概览和独立的身体／地点背景。口语清稿与七删一改全文继续保留。其他人的讲话和未确认身份的内容不会被过滤；声纹只用于归属提示。

## 接收端配置

此功能需要 Phone v3 接收模块和更新后的 iPhone App。传输协议、权限及删除接口见 [Phone v3 合同](phone-v3-api.md)。手机使用独立配对凭据；不要把录音设备密钥复制给手机接口。

在已有私有文字配置中增加以下字段，保留原来的 `home`、模型配置、预算、截止日期和录音路径：

```json
{
  "context_root": "C:/private/watch-context",
  "reading_root": "C:/private/watch-context/reading",
  "timezone": "Asia/Taipei"
}
```

上例是配置增量，不是完整配置。`context_root` 应位于受限的本地文件系统，限定服务用户及管理员访问；不是公开资料目录。文字任务与今日任务读取同一份文字配置，使用同一个 `state.sqlite3` 费用账本：

```sh
python processor/text_worker.py --config /absolute/private/text-output.json
python processor/overview_worker.py --config /absolute/private/text-output.json
```

`overview_worker` 有独立进程锁，默认每 120 秒检查一次。卡片摘要按内容版本缓存；日概览基于卡片摘要生成，同样缓存。每轮最多发出两个新摘要请求，失败有界重试。所有请求共享既有的每日 token 预留限额，不能给今日任务另建空费用数据库。没有录音时不调用整理模型。

运行 ASR 的进程还需设置 `WATCH_MEMORY_CONTEXT_ROOT` 为同一目录，并确保 receiver 的 `context_store.py`、`platform_store.py` 可导入。例如完整仓库安装可把 `receiver` 目录加入 `PYTHONPATH`；自定义平铺部署必须使用实际模块目录。ASR 和发布过程共用删除 generation 锁，禁止退休的原始来源重新转写或发布。

## 健康和位置如何处理

手机经用户授权读取真实 HealthKit 记录，原始测量和坐标只进入本地接收端。文字模型仅接收对话文本及归属标签，不接收健康测量、坐标或声纹特征。

身体行可展示已经记录的睡眠时长、静息心率／HRV 记录中位数、运动时长和带时段的粗地点标签。睡眠／运动重叠区间取并集，避免设备或睡眠阶段重复计时；睡眠窗口明确为前日 18:00 至当日 18:00，截至当前已收到的数据，不声称整夜采集完整。地点表示历史停留，不把最后一条记录当作当前位置。

压力相关生理信号比较是实验性的个人历史比较，不是诊断或心理压力测量。日静息心率估计和瞬时心率不混用；同一比较只使用相同测量指标、场景和来源。瞬时记录需要明确静止元数据、真实低步数覆盖或睡眠区间，运动附近的样本排除。缺少活动数据不能推断静止。

当前启发式要求至少七个有足够样本的历史日，并在最长 28 天读取窗内计算每日中位数基线。这个门槛是工程限制，不是临床验证标准，也不意味着佩戴七天就一定能分析。没有足够可比数据时明确显示未知。输出仅描述心率升高与 HRV 降低是否共同偏离个人历史，不从同时发生的谈话推断原因。

## 删除语义与同步

删除采用确认计划、持久墓碑和异步阶段状态：先退休指定对话，再处理原音、转写、摘要、云文档、笔记和手机投影。退休来源不能通过重试或重新上传复活。

共享录音只按实际词级对齐边界处理。属于其他对话的音频保留并验证；粗时间范围、未知边界或重叠音频会显示 `shared_audio_pending`，不声称原音全部删除。

飞书通过持久化的对话到 block ID／内容归属删除对应块，保留其他对话块。人工改写、同文替换成新块或未知请求结果都进入明确冲突状态，不整篇覆盖。某一天的云冲突不会阻塞新录音转写、其他日期发布或本地新稿。

Mac 拉取支持清单墓碑，只回收仍匹配自身已管理哈希的文件；人工编辑保留并报告冲突。Mac 完成实际文件处理后，通过现有认证 SSH 通道写回 `consumer-ack.json`。接收端核对 generation、清单版本、文件身份和冲突情况后，才把笔记删除阶段标为完成。Mac 离线时该阶段保持等待。

当前同步副本回收目录为项目笔记根目录下的隐藏 `.watch-memory-trash`，便于恢复。删除状态不能用“卡片从列表消失”代替全部阶段完成。原始 HealthKit 与系统位置记录不属于录音卡删除范围，不会被此操作删除。

## 验证与边界

自动化测试覆盖缺数据／混杂场景、测量分池、退休序号与冷启动、摘要在途结果丢弃、真实发送边界互斥、重复正文的 block 身份、共享对话保留和 Mac 消费回执。合成数据通过不等于用户真机健康／位置授权完成，也不等于实际日常压力准确率得到验证。最终安装需在真实设备上由用户完成系统授权后检查数据到达。

## 保留语义与增量概览

阅读编辑新增私有保留计划，将每个输入单元标为保留或有明确原因的冗余／填词／寒暄／推广。短句、未知归属、他人完整讲解和疑似媒体来源都不能作为删除有意义内容的理由。声纹标签保留在详情元数据，正文不再逐段重复“归属未确认”。摘要依据整理稿实际保留的语义，不能重新引入已经剔除的推广。

Phone v3 增加可选字段，旧 App 可忽略：`cards.reading_role`（main/background/unknown）、`cards.summary_state`；日概览包括实际覆盖起止、更新时刻、是否部分完成及已覆盖／待处理数量；身体背景 `measured_through_at` 使用实际健康样本结束时间，缺测量为 null，不取投影刷新时间。

新卡尚未处理不会阻断已完成内容的概览。有效的旧覆盖可在增量处理中继续阅读，通常十分钟合并一次新覆盖，减少文案抖动；修订或删除使依赖失效时立即更新。后台仍沿用同一费用账本和上限，不为新版本重置预算。

## 经确认重新整理某一天

`processor/reprocess_reading.py` 分候选和应用两个阶段，原音和 ASR 不变：

```sh
python processor/reprocess_reading.py --config /private/text-output.json --date YYYY-MM-DD --candidate-dir /private/revision-candidate
```

候选存放于私有目录，保留 source/group 身份和精确删除范围。先备份状态与原批次，随后用现有费用账本生成净化候选；必须人工检查语义、短想法、他人内容、推广处理和旧问题对照。审核通过后，才将候选 JSON 的 `approved_for_apply` 标为 true 并执行 `--apply-reviewed`。

应用时要先让正式文字 worker 进入安全停止状态，以取得同一 worker OS 锁。飞书只替换经过完整身份／内容／格式核验的两段受管正文范围，保留文档和章节标题，不调用整篇 overwrite。发现手工内容、资源或样式变化则停止该云操作，保留冲突。来源版本、截止时间或删除 generation 变化会使候选失效。失败或未知外部结果需要回读检查，不应盲目重发。核对云文档、受管本地两稿与新概览后，再结束本次整理。

### Processing progress and daily budget accounting

The optional `ingestion` projection separates received audio, completed local ASR and organized text. `latest_recorded_at` and `latest_transcribed_at` are the end times of the latest audio events committed at those stages. `latest_organized_at` is a continuous event-time frontier: it does not skip an earlier pending source, and empty ASR sources count as confirmed no speech. None of these fields is a current microphone indicator or the projection refresh time. Counts refer to unique sources, exclude retired/test input and expose no source identities. States are `empty`, `processing`, `budget_wait` and `up_to_date`; `queued_text_count` includes budget-blocked text. Overview coverage remains independent.

Provider admission reserves a bounded worst-case allowance atomically. Completed, model-matched `stop` responses with internally consistent integer token usage can settle that allowance to the actual total in an append-only `admission_settlements` table. Existing admission history remains unchanged. Missing, malformed or non-`stop` outcomes retain the full reservation; cache-hit input remains part of prompt tokens, and reasoning remains part of completion tokens. Neither is counted twice or discounted. Settlement and admission share SQLite `BEGIN IMMEDIATE` transactions, and repeated settlement cannot release the same allowance twice. The daily limit and model effort do not change.

Natural grouping treats question/answer exchanges, examples, short interruptions and a return to the same problem as one discussion where the content supports continuity. The provider receives up to 1,800 characters of retained previous-group context in addition to a bounded raw tail. A confirmed continuation reuses the existing group ID; transport file boundaries do not create a new conversation by themselves. Existing historical group IDs and deletion ranges are not automatically merged by proximity.

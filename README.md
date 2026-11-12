# 跨机构科研资源身份归并

面向合作资料接收人员的 Python 采集组件。对设备来源记录做字符规范化，保留机构内身份、修订及观察时间。
输入样本都是本地合成数据，example 域名只表示来源身份，不需联网访问。

## 安装与运行

使用 Python 3.11，依赖仅为标准库，SQLite 随解释器提供，无需安装第三方包。
从项目根目录执行；演示启动真实的回环 HTTP 来源、处理响应、事务入库后读取实际结果。
每次演示使用独立临时目录，结束后清理；通常两秒内结束，不回放历史输出。

```bash
python3 -m compileall -q collector
python3 -m unittest discover -s tests -v
python3 -m collector demo
```

常规导入保留在本地数据库中，可重复运行；默认路径已忽略，不会进入版本库。

```bash
python3 -m collector import --input fixtures/sample.json --db var/records.sqlite3
python3 -m collector show --db var/records.sqlite3
python3 -m collector serve --db var/records.sqlite3 --port 8080
```

查询服务监听回环地址，`GET /records` 返回数据库中的记录；按 Ctrl-C 停止。

## 组件与数据

`collector/domain.py` 负责来源格式和领域校验；`storage.py` 保留原始对象、导入凭据和当前记录。
单次导入在同一事务内提交，异常时整体回滚；相同来源和原文只登记一次凭据。
当前记录以领域标识为键，同键再次导入替换当前观察值，不代表保留了完整修订历史。
`transport.py` 提供请求客户端和本地响应服务；服务按路径依次消费响应脚本，并记录实际请求。
`api.py` 提供只读查询入口。`fixtures/sample.json` 是当前导入器支持的输入。
其他附件描述同领域的合成页面、协议或事件，可以由调用方构造本地来源。
当前响应服务是顺序脚本工具，不自动推断认证、文件或表单业务；领域对象提供对应的本地交互。

## 实体归并服务

`collector/resolution.py` 在原始记录库之上归并多来源设备记录，输出稳定资源号（`resource-N`，只增不复用）。
自动归并只接受充分证据：同一机构（旧名按生效时间归一）+ 同型号 + 序列号一致且未脱敏；
型号相同只算候选，序列号缺失、脱敏或冲突的记录对进入候选队列，交人工确认。
实体保留字段级来源、置信依据与冲突值；当前机构归属取观察时间最新的成员，
更旧的快照（`observed` 更小）在入库时被拦截，原文与凭据仍完整保留。
人工决定（合并/拆分）是持久约束，后续抓取不会覆盖；决定携带基准版本，
若自动更新已先行改变实体，提交返回版本冲突（HTTP 409）并附当前版本，重新获取后可按各实体版本重试。
拆分不删除任何行：误合并实体退役留痕，成员获得新资源号，
实体事件与成员映射事件完整记录"谁何时并入/拆出"，可解释地回放每次归属变化。

```bash
python3 -m collector resolve --input fixtures/resources.json --db var/resources.sqlite3
python3 -m collector entities --db var/resources.sqlite3
python3 -m collector decide --db var/resources.sqlite3 --decision '{"type":"merge","sources":["a:7","b:2"],"version":1}'
```

`resolve` 演示名录入库、别名归一、候选解释、人工合并与撤销拆分的全过程，
末尾输出完整性校验（每条采集记录恰好属于一个活跃实体，无丢失、无重复计数）。
查询服务额外提供：`GET /entities`、`GET /entities/{资源号}`、`GET /entities/{资源号}/events`、
`GET /candidates`（候选解释）、`GET /records/{键}/history`（资源号映射史）、
`GET /integrity`（完整性校验）、`POST /decisions`（人工确认/拆分，409 表示版本冲突）。

## 可复现运行

宿主机或容器均不需要网络。镜像准备好以后可以关闭外部网络再运行上述命令。
仓库内不含真实账号、新闻正文或人员资料；不要将生产凭据写入样本或日志。

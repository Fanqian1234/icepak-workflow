# 本机唯一生产队列

正式入口：`main.py queue --queue-file runs/state/REQUESTED_CASES.txt`。
Windows 任务：`NeuralCHT_IcepakWorkflow_Local`；旧任务继续禁用。

## 权威状态

- `runs/state/REQUESTED_CASES.txt`：本机任务清单，一行一个 Case；调度器运行中会重新读取，不再把命令行82例永久缓存。
- `runs/state/QUEUE_STATE.json`：管理器状态、实际活动 Case、资源、进程 CPU/I/O 进度和暂停原因。
- `runs/state/case_XXXX.json`：Case 阶段、进程出生时间、代码修订、失败原因和原生证据。
- `runs/published/case_XXXX`：新流程独立验收结果；不等于已经覆盖 `D:\3DIC_model\case\case_XXXX`。

## 新增任务

先确认服务器排除这些本机专属 ID，再执行 `python -B main.py enqueue --ids case_XXXX,case_YYYY`。
它只原子更新任务清单，不启动第二个管理器，不改在途 Case 的参数。

## 安全修改代码

1. 执行 `python -B main.py drain`，停止新准入，不终止当前求解。
2. 等待 `QUEUE_STATE.json` 为 `DRAINED`，核实没有本项目在途 Python/Fluent/HDM。部署期间禁用 Windows 任务触发。
3. 在隔离临时目录修改和测试整组相关模块；不得边求解边写正式生产文件。
4. 无在途任务时统一部署，执行 `python -B test_reliability.py`。
5. 执行 `python -B main.py resume`，再启用并启动原 Windows 任务。

每次新的正式发布更新 `settings.py` 内人工可读的 `CODE_REVISION`；物理网格规则改变时更新 `MESH_REVISION`。不增加文件哈希准入。

## 失败和清理

失败 Case 进入修复等待区；目录锁住、启动失败、输入不合法不能阻断其他 Case。
未验收原生数据不因 `FAILED` 标签而自动删除。已发布数据由父管理器在工作进程退出后回收原生临时目录；PID须同时匹配进程出生时间与角色。

## 科学验收

统一完整结果检查覆盖 READY、21项必需采样/证据文件、网格形状和有限数值、四档T0、真实原生 Source 单元、鳍片/接触、40次迭代温度稳定、质量与能量守恒。
原生解析不支持、文件尚未写完和真实 Source 缺失分别报告；网格检查在长时间流热求解前执行，并保存 `NATIVE_MESH_AUDIT.json`。
不得通过跳过科学验收增加成功数。

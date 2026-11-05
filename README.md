# icepak-workflow

Windows + Ansys AEDT/Icepak 2023.2 的 NeuralCHT Icepak/Fluent 生产采样流水线。当前版本包含队列调度、原生 CHT、物理连续性验证、Source 原生温度监控、面积积分采样、准入验收和发布收据。

## 当前入口

```powershell
python -B main.py plan case_XXXX
python -B main.py enqueue --ids case_XXXX,case_YYYY
python -B main.py queue --queue-file runs/state/REQUESTED_CASES.txt
python -B main.py status
python -B main.py drain
python -B main.py resume
python -B main.py cleanup
python -B test_reliability.py
```

正式操作顺序和 Windows 任务说明见 [OPERATIONS.md](OPERATIONS.md)。

## 模块职责

- `main.py`：唯一生产入口；执行 Case 生命周期、排他工作区、发布和 drain/resume。
- `scheduler.py`：队列锁、动态请求文件、资源准入、worker 监控、超时、孤儿回收和修复等待。
- `settings.py`、`case_spec.py`：路径、版本标识、运行策略和物理 Case 解析。
- `geometry.py`、`icepak.py`、`mesh.py`：物理几何、AEDT/Icepak 建模、网格和 CHT。
- `physical_convergence.py`：从同一 Fluent restart field 继续最多 40 步，测量 Source 温度稳定性并绑定最终原生场。
- `native_source_temperature_monitor.py/.c`：按实际 Source solid zone 编译只读 Fluent 监控，校验 zone 身份和报告绑定。
- `native_io.py`、`native_fin_gate.py`：读取原生网格/场，验证 Source、封装、鳍片和接触单元。
- `sampling.py`：输出 80×80 Gamma、40×40 HTC、128×128/48×48 Source 温度和功率图。
- `acceptance.py`、`publish.py`：守恒、覆盖率、连续性、发布包完整性和 READY/CHT 收据。
- `data/`：重型 full-truth 任务及桥接逻辑。
- `test_reliability.py`、`tests/fixtures/`：不启动 AEDT 的可靠性和输入异常测试。

## 当前提交相对上一版的主要变化

- 新增 `physical_convergence.py`，将原生求解后的继续计算、40 步温度稳定性和最终 Fluent 场身份绑定纳入发布前门禁。
- 新增 `native_source_temperature_monitor.py/.c`，按真实 Source zone 编译监控，校验绑定哈希、Source 单元数量和体积。
- 调度器改为动态读取 `REQUESTED_CASES.txt`，增加 `enqueue`、`drain`、`resume`、活动 worker 识别和修复等待。
- 发布流程增加完整采样包检查、物理输入绑定、代码/网格修订号和 CHT admission 收据。
- 新增可靠性测试；运行数据、AEDT 工程、Fluent 网格/场、日志和缓存继续排除在仓库之外。

## 依赖与运行边界

这是 Windows/Ansys 专用生产代码，不是纯 Python CFD 求解器。运行至少需要：

- Ansys AEDT/Icepak 2023.2、Fluent 和 PyAEDT；
- Python 包 `psutil`、`numpy`、`scipy`；
- 项目级 `case/CASE_FEATURES.csv`、Case 原生工程、网格和 Fluent 字段；
- 与当前部署一致的 `CASE_MASTER.csv` 和历史队列状态收据。

当前代码中的 `settings.py` 将 `CASE_MASTER` 指向 `case/CASE_MASTER.csv`，而项目长期规则把根目录 `CASE_MASTER.csv` 作为权威接口；部署前必须核对两者身份，不能默认它们一致。full-truth 入口还保留若干本机 `D:/E:/F:` 外部资料路径，需要按实际机器配置。

## 验证状态

- `python -m compileall`：通过。
- `python -B test_reliability.py`：当前环境因缺少 `psutil` 未执行完成。
- `python -B main.py status`：同样因缺少 `psutil` 阻塞。
- 未在本次同步中启动真实 CHT，也未上传 `runs/` 运行数据。

未在原项目中指定许可证。

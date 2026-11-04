# icepak-workflow

独立的 Icepak/Fluent NeuralCHT 生产工作流代码，面向 Windows + Ansys AEDT/Icepak 2023 R2 环境。代码负责从权威 Case 表读取物理输入，构造封装与散热器几何，生成四档 package-only T0，运行原生 CHT，验证原生网格/鳍片/接触面，按面积守恒采样 Gamma HTC、Source 温度和功率图，并发布带收据的样本。

## 代码结构

- `main.py`: 唯一命令行入口，提供 `plan`、`case`、`queue`、`status`、`cleanup`。
- `scheduler.py`: 单调度器锁、资源准入、并发 worker、超时和孤儿工作区回收。
- `settings.py`: 项目路径、网格分辨率、T0 换热系数、资源策略和排除策略。
- `case_spec.py`: 解析并校验 `CASE_FEATURES.csv` 的尺寸、材料、source 布局、功率和鳍片参数。
- `geometry.py`: 根据实际物理输入生成封装、TIM2、散热器和 Gamma 平面；不按 case ID 或温度真值切换几何算法。
- `t0.py`: 生成/复用 h0=250/500/1000/2000 的四档 package-only 温度场。
- `icepak.py`: AEDT/Icepak 会话、建模、网格、求解和原生字段导出。
- `native_io.py`、`native_fin_gate.py`: 读取 Fluent 原生网格/场并验证实体材料、鳍片和接触面。
- `sampling.py`: 面积积分采样 80×80 Gamma、40×40 HTC、128×128/48×48 Source 温度及功率图。
- `acceptance.py`、`publish.py`: 守恒、覆盖率、收敛、文件身份和 READY 收据门禁。
- `data/run_full_truth_task.py`、`data/bridge_fulltruth_common.py`: 原生 full-truth 任务与桥接逻辑，属于重型数据生成入口。

## 使用

从本目录运行：

```powershell
python -B main.py plan case_1254
python -B main.py case case_1254 --cores 4
python -B main.py queue --ids case_1254,case_1256
python -B main.py queue
python -B main.py status
python -B main.py cleanup
```

运行前必须准备项目级权威输入：

- `CASE_MASTER.csv`
- `case/CASE_FEATURES.csv`
- 与案例绑定的原生 AEDT/Fluent 工程、网格和字段文件
- 可用的 Ansys AEDT 2023.2 / PyAEDT 环境

默认路径由 `settings.py` 根据项目目录推导，工作区和发布结果写入上级实验目录的 `runs/work`、`runs/state`、`runs/samples`、`runs/published`。因此本仓库代码不能仅凭克隆后直接运行；需要将它放回兼容的项目目录，或先按本项目的路径合同调整 `settings.py`。

## 设计要点

- 每个 Case 使用独占工作区和状态记录，避免重复 worker 覆盖结果。
- 调度器在启动前检查内存、磁盘和旧队列占用；失败、超时和清理延迟都会留下状态。
- 物理输入、Source 功率、FEOL 分区、Gamma 面积、两侧 Source 温度和 package 能量闭合均在发布前复核。
- 采样文件与 `validation.json`、`sampling_receipt.json`、`READY.json` 一起发布；未通过门禁的原生结果不会生成可训练样本。

## 已知边界

- 这是 Windows/Ansys 专用工作流，不是纯 Python CFD 求解器。
- `data/run_full_truth_task.py` 保留了历史生产环境的外部盘符和 AEDT 安装路径，需要按部署机器核对。
- 当前目录未包含 Case 数据、AEDT 工程、Fluent 原生网格/字段、运行缓存或发布样本；这些内容不应提交到代码仓库。
- `python -m compileall icepak_workflow` 已通过；没有在无 AEDT 环境中执行真实 CHT。

## License

未在原项目中指定许可证。

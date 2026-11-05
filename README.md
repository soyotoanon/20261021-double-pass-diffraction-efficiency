# 20261021 AOM 双程衍射效率 / AOM Double-Pass Diffraction Efficiency

2026-10-21 的 AOM double-pass 一级衍射效率随 RF 驱动幅度变化的实测数据，以及当时使用的
简化测量界面（Tkinter + Thorlabs PM100D3 + RIGOL DG5352 Pro）。

Measured double-pass first-order diffraction efficiency vs RF drive amplitude, together with the
simplified measurement GUI used on that day.

## 实验条件 / Setup

| 项目 | 内容 |
| --- | --- |
| AOM | SGT100-556-2TA-B30，中心频率 100 MHz，工作波长 556 nm，double-pass 光路 |
| RF 源 | RIGOL DG5352 Pro，CH1，100 MHz，经功放驱动 AOM |
| 扫描幅度 | 300 → 550 mVpp，步长 1 mVpp（每点 5 次采样取平均） |
| 功率计 | Thorlabs PM100D3（序列号 M01306624），556 nm |
| 入射参考 | 1.058522 mW（60 秒入射测量的**后 50 秒**平均值，前 10 秒用于摆稳探头，不计入） |
| 效率定义 | η = P(double-pass 输出) / P(入射参考) × 100 % |

## 结果摘要 / Results

- η(300 mVpp) = **43.72 %**
- 峰值 η = **82.93 % @ 492 mVpp**
- η(550 mVpp) = **78.67 %**；480–500 mVpp 区间约 82–83 %，接近平台
- 早先一次 5 mVpp 步长的粗扫（51 点）峰值 83.97 % @ 500 mVpp，与细扫一致

## 目录 / Contents

| 路径 | 说明 |
| --- | --- |
| `simple_app.py` | 简化界面：入射功率 60 秒稳定性测量（后 50 秒统计）+ double-pass 效率实时扫描 |
| `core.py` | 计算模块：稳定性统计、扫描点生成、效率换算 |
| `devices.py` | 仪器驱动：Thorlabs PM100D、RIGOL DG 系列，及模拟设备 |
| `start_gui.cmd` / `install_dependencies.cmd` / `run_self_test.cmd` | 启动、装依赖、自检 |
| `test_core.py` / `test_scan_integration.py` | 9 项单元/集成自检 |
| `render_table.py` | 把扫描 CSV 渲染成表格图片 |
| `double_pass_efficiency_table.png` | 251 个扫描点的原始数据表格（图片） |
| `ui_after_change.png` | 测量完成时的界面截图 |
| `measurements/` | 原始数据 CSV（两次入射测量、粗扫与细扫）及细扫曲线图 |

## 原始数据 / Raw data

| 文件 | 内容 |
| --- | --- |
| `measurements/20261021_145847_incident_60s.csv` | 本次扫描采用的入射功率 60 秒记录（参考 1.058522 mW） |
| `measurements/20261021_145022_incident_60s.csv` | 更早一次入射功率 60 秒记录（参考 1.046652 mW，未用于本次扫描） |
| `measurements/20261021_150205_double_pass_scan.csv` | 细扫原始数据：300–550 mVpp，步长 1 mVpp，251 点 |
| `measurements/20261021_145954_double_pass_scan.csv` | 粗扫原始数据：300–550 mVpp，步长 5 mVpp，51 点 |
| `measurements/20261021_150205_double_pass_scan.png` | 细扫的功率/效率曲线图 |

## 运行 / Run

```powershell
install_dependencies.cmd      # pip install -r requirements.txt
start_gui.cmd                 # 打开简化界面
run_self_test.cmd             # 跑 9 项自检
```

界面流程：先把探头放到 AOM 入射位置点“开始测量 60 秒”（前 10 秒不计入，采用后 50 秒均值），
再把探头移到 double-pass 输出，设置起点/终点/步长后扫描；效率曲线实时更新，CSV 自动落盘到
`measurements/`。停止、报错或关窗都会关闭 RF。

## 注意 / Notes

- 扫描期间不要移动 AOM、透镜、回射镜和光阑；功放增益与供电保持固定。
- 简化模式不单独扣暗功率；若探头无光时读数相对信号不可忽略，请先在功率计上归零。
- 这只 AOM 的额定 RF 驱动功率尚未从数据表确认，550 mVpp（约 1.76 W 功放输出）仅按实测记录保留，
  长期工作点请以器件数据表为准。


## 从 0 mVpp 开始扫描

简化界面默认从 0 mVpp 开始。0 点通过关闭发生器 RF 测量并保存残余光功率及其相对入射功率，后续正幅度点依次开启 RF 扫描。该残光未自动扣除，不能视作严格的零光功率。100 MHz、50 Ω 条件下正幅度不得低于 1 mVpp；扫描上限仍为 550 mVpp。完整界面保持原有正起点要求。

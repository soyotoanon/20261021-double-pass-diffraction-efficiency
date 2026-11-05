# 原版：AOM 双程衍射效率扫描

原版直接使用功率计读数，不作 80% 修正。程序：[simple_app.py](../../simple_app.py)；启动：[start_gui.cmd](../../start_gui.cmd)。

## 测量方法

556 nm double-pass 光路；信号发生器为 RIGOL DG5352 Pro，CH1 固定 100 MHz，经功放驱动 AOM；功率计为 Thorlabs PM100D3。先测量入射光 60 秒，前 10 秒用于放置探头，后 50 秒均值作为入射参考；再测量双程输出。效率 = 双程输出功率 / 入射参考功率 × 100%。每个扫描点测量 5 次并取平均。

原版默认 0 → 550 mVpp，步长 10 mVpp，可在界面中调整；0 mVpp 时关闭 RF，测得的是残余光功率。程序不自动扣除暗功率。

## 2026-10-21 实验数据

入射参考 **1.058522 mW**。1 mVpp 步长的 300 → 550 mVpp 扫描中，最高记录为 **82.93%（492 mVpp）**。这是该次扫描的结果。

- [细扫数据：300 → 550 mVpp，1 mVpp 步长](../../measurements/20261021_150205_double_pass_scan.csv) · [曲线](../../measurements/20261021_150205_double_pass_scan.png)
- [粗扫数据：300 → 550 mVpp，5 mVpp 步长](../../measurements/20261021_145954_double_pass_scan.csv)
- [本次采用的入射功率记录](../../measurements/20261021_145847_incident_60s.csv) · [更早一次入射记录](../../measurements/20261021_145022_incident_60s.csv)
- [细扫数据表图](../../measurements/double_pass_efficiency_table.png) · [当时的界面截图](../../measurements/ui_after_change.png)

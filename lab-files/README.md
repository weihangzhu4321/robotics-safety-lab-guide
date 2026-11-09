# 机器人与安全实验配套源码

先在 [实验指导网站](https://weihangzhu4321.github.io/robotics-safety-lab-guide/) 打开对应章节。这里按实验编号保存源码、ROS 配置、仿真资源和示例参数。已准备好的 Ubuntu A/M 环境可直接按实验零使用。从空环境开始时，请先看 [环境重建说明](environment/README.md)。

| 实验 | 目录用途 |
|---|---|
| 01–03 | 共用 `robot_labs/`，复制一份到 `~/robot_labs` 即可。包含 TurtleSim、连接、建图和导航入口。 |
| 04 | `工具/` 放到 `~/lab4_prep/工具/`，用于 Gazebo、循线预览和相机检查。 |
| 05–07 | 分别放到 `~/lab5_prep` 至 `~/lab7_prep`。包含本机检查、隔离实验与抓包辅助工具。实验六的 `A低速副本/` 按指南加入 A 的 `~/robot_labs`。 |
| 08 | 放到 `~/lab8_prep`。`依赖/downloads.sha256.json` 记录 PyCryptodome 等依赖的下载来源和哈希，依赖文件需按清单取得后交给安装脚本。 |
| 09 | 放到 `~/lab9_prep`。包含自编识别器和测试。课程示例图见 [实验九网页](https://weihangzhu4321.github.io/robotics-safety-lab-guide/lab-09.html)，教师识别程序和模型权重需另行取得。 |
| 10 | 放到 `~/lab10_prep`。从本机隔离靶场入口开始，候选项由脚本为每次运行临时生成。 |
| 11 | 放到 `~/lab11_prep`。按指南将 `lab11_formation` 放入 catkin 工作区。包含 Gazebo 仿真和默认拒绝发送目标的 Action 客户端。实车短测程序、现场地图及 Action 测试服务需另行准备。 |

`CHANGE_ME` 等占位符按自己的实验网络填写。需要发包或发布运动指令的实验，先按章节核实隔离范围、目标和停止方式。`SHA256SUMS.txt` 可用于检查下载内容。

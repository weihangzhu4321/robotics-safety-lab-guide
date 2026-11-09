# 环境重建说明

本目录保留了 Noetic 基础镜像的 Dockerfile、当时使用的附加软件包清单，以及课程 Docker 归档的 SHA-256。完整 Ubuntu A/M 虚拟机和 Docker 镜像归档需另行保留。源码重建依赖上游的软件包仍然可取，生成的镜像哈希也可能不同。

虚拟机用 VMware、Ubuntu 24.04 LTS x86-64 和 UEFI。A、M 均设 2 个虚拟 CPU，内存分别为 3 GB 和 1.5 GB。初次开机保持虚拟网卡断开，配置好后再按 [实验零](https://weihangzhu4321.github.io/robotics-safety-lab-guide/lab-00.html) 选择 NAT 或桥接。A 需要 XFCE、VMware Tools、Docker 和 X11 授权工具。M 需要 XFCE、VMware Tools、Wireshark、tshark、tcpdump、dsniff、Scapy、dpkt、Paramiko 和 OpenSSH。若使用实验七的 hping3 或 nmap 路线，另行检查相应工具。

1. 有原始课程 Docker 归档时，将归档和本目录的 `.sha256` 一起放入 `robot_labs/images/`，再从 `robot_labs/` 运行 `bash load-image.sh`。脚本先校验再导入。
2. 需要从源码重建时，先从 [Ubuntu Base 发行归档](https://cdimage.ubuntu.com/ubuntu-base/releases/20.04/release/) 取得 `ubuntu-base-20.04.5-base-amd64.tar.gz` 并核对官方 `SHA256SUMS`，导入 Docker 并标记为 `lab3-focal-base:20.04.5`。用干净系统的公共 CA 证书包和当前可验证的 ROS 软件源签名公钥填充 Dockerfile 指定的两个文件。旧镜像地址在 Dockerfile 中保留供核对，构建前确认软件源仍提供 Focal 和 Noetic。构建后标记为 `lab3-ros1:noetic`。
3. 参考 `noetic-addons.sha256.json` 和 `noetic-buildtools.sha256.json` 获取可验证的软件包，在基础镜像内安装 TurtleSim、rqt_plot、rqt_graph 和 build-essential，标记为 `robot-labs:noetic-20261022`。之后从 `~/robot_labs` 运行 `bash lab.sh health`，确认能找到实验用的 ROS 软件包。
4. 实验四、九、十一的视觉或 Gazebo 路线，还要按实验四工具的 Dockerfile 构建 `robot-lab4:noetic-prep`。基础镜像用 `robot-labs:noetic-20261022`，构建后按网站步骤做本机检查。

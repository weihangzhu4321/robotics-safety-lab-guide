FROM lab3-focal-base:20.04.5
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8
COPY ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY ros-archive-keyring.gpg /usr/share/keyrings/ros-archive-keyring.gpg
RUN printf '%s\n' \
    'deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ focal main restricted universe multiverse' \
    'deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ focal-updates main restricted universe multiverse' \
    'deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ focal-security main restricted universe multiverse' \
    > /etc/apt/sources.list \
 && printf '%s\n' 'deb [signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] https://mirrors.tuna.tsinghua.edu.cn/ros/ubuntu/ focal main' > /etc/apt/sources.list.d/ros1.list \
 && printf '%s\n' 'Acquire::Retries "3";' 'Acquire::https::Timeout "45";' > /etc/apt/apt.conf.d/80lab3-download \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
    ca-certificates curl iproute2 iputils-ping python3-tk xauth fonts-noto-cjk \
    ros-noetic-ros-base ros-noetic-gmapping ros-noetic-amcl ros-noetic-map-server \
    ros-noetic-move-base ros-noetic-dwa-local-planner ros-noetic-navfn ros-noetic-rviz \
    ros-noetic-turtlebot3-slam ros-noetic-turtlebot3-navigation ros-noetic-turtlebot3-teleop \
    ros-noetic-turtlebot3-bringup ros-noetic-turtlebot3-description \
    ros-noetic-robot-state-publisher ros-noetic-tf \
 && useradd --uid 1000 --create-home --shell /bin/bash labuser \
 && mkdir -p /lab3 \
 && chown 1000:1000 /lab3 \
 && apt-get clean
ENV TURTLEBOT3_MODEL=burger
WORKDIR /lab3
CMD ["/bin/bash"]

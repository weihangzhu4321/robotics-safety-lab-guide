"""Read-only check: never start a second controller while another publishes velocity."""
import sys
import socket
import rosgraph

socket.setdefaulttimeout(6)

master = rosgraph.Master('/robot_labs_controller_check')
publishers, _, _ = master.getSystemState()
active = [n for topic, nodes in publishers if topic == '/cmd_vel' for n in nodes]
if active:
    print('发现已有 /cmd_vel 发布者：' + ', '.join(active))
    print('请由原操作者关闭遥控/导航程序，再重试；不要盲目 kill 别人的节点。')
    sys.exit(1)
print('/cmd_vel 没有已有发布者。')

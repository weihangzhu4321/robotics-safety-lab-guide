"""Explicit cancel/stop request. Publishes only zero velocity."""
import time
import rospy
from actionlib_msgs.msg import GoalID
from geometry_msgs.msg import Twist

rospy.init_node('robot_labs_cancel', anonymous=True)
cancel = rospy.Publisher('/move_base/cancel', GoalID, queue_size=2, latch=True)
velocity = rospy.Publisher('/cmd_vel', Twist, queue_size=2)
time.sleep(0.5)
for _ in range(20):
    if rospy.is_shutdown():
        break
    cancel.publish(GoalID())
    velocity.publish(Twist())
    time.sleep(0.05)
print('已发送取消和零速度，请观察实车是否停稳。')

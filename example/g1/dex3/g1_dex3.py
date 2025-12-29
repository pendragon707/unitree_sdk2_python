import sys
import time
import math
import select
import termios
import tty
import numpy as np
from typing import List

from enum import IntEnum

import threading
from multiprocessing import Process, Array, Value, Lock

# Import Unitree SDK2 types
try:
    from unitree_sdk2py.core.channel import ChannelFactory, ChannelFactoryInitialize, ChannelPublisher, ChannelSubscriber
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import HandCmd_, HandState_
    from unitree_sdk2py.idl.default import unitree_hg_msg_dds__HandCmd_
except ImportError as e:
    print("Error importing Unitree SDK2 or IDL types:", e)
    sys.exit(1)

# Constants
MOTOR_MAX = 7
SENSOR_MAX = 9

class Dex3_1_Left_JointIndex(IntEnum):
    kLeftHandThumb0 = 0
    kLeftHandThumb1 = 1
    kLeftHandThumb2 = 2
    kLeftHandMiddle0 = 3
    kLeftHandMiddle1 = 4
    kLeftHandIndex0 = 5
    kLeftHandIndex1 = 6

class Dex3_1_Right_JointIndex(IntEnum):
    kRightHandThumb0 = 0
    kRightHandThumb1 = 1
    kRightHandThumb2 = 2
    kRightHandIndex0 = 3
    kRightHandIndex1 = 4
    kRightHandMiddle0 = 5
    kRightHandMiddle1 = 6

Dex3_Num_Motors = 7
kTopicDex3LeftCommand = "rt/dex3/left/cmd"
kTopicDex3RightCommand = "rt/dex3/right/cmd"
kTopicDex3LeftState = "rt/dex3/left/state"
kTopicDex3RightState = "rt/dex3/right/state"

# Hand joint limits (radians)
MAX_LIMITS_LEFT  = [ 1.05,  1.05,  1.75,  0.0,   0.0,   0.0,   0.0  ]
MIN_LIMITS_LEFT  = [-1.05, -0.724, 0.0, -1.57, -1.75, -1.57, -1.75]
MAX_LIMITS_RIGHT = [ 1.05,  0.742, 0.0,  1.57,  1.75,  1.57,  1.75]
MIN_LIMITS_RIGHT = [-1.05, -1.05, -1.75, 0.0,   0.0,   0.0,   0.0 ]

# Enum-like state
STATE_INIT    = 0
STATE_ROTATE  = 1
STATE_GRIP    = 2
STATE_STOP    = 3
STATE_PRINT   = 4

STATE_NAMES = {
    STATE_INIT:   "INIT",
    STATE_ROTATE: "ROTATE",
    STATE_GRIP:   "GRIP",
    STATE_STOP:   "STOP",
    STATE_PRINT:  "PRINT"
}

# Global state
current_state = STATE_INIT
state_lock = threading.Lock()
hand_id = 0  # 0 = left, 1 = right
is_left_hand = True

# DDS channels
# handcmd_publisher = None
# handstate_subscriber = None
latest_state_msg = None

# RIS mode packing helper
def pack_ris_mode(motor_id: int, status: int = 1, timeout: int = 0) -> int:
    mode = (motor_id & 0x0F) | ((status & 0x07) << 4) | ((timeout & 0x01) << 7)
    return mode

# Callback for hand state subscription
def hand_state_callback(msg: HandState_):
    global latest_state_msg
    latest_state_msg = msg

# Non-blocking keyboard input (Linux only)
def get_key_non_blocking() -> str:
    if select.select([sys.stdin], [], [], 0) == ([], [], []):
        return ''
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setraw(sys.stdin.fileno())
        char = sys.stdin.read(1)
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
    return char

def subscribe_hand_state(LeftHandState_subscriber, RightHandState_subscriber, left_hand_state_array, right_hand_state_array):
    while True:
        left_hand_msg  = LeftHandState_subscriber.Read()
        right_hand_msg = RightHandState_subscriber.Read()
        if left_hand_msg is not None and right_hand_msg is not None:
            # Update left hand state
            for idx, id in enumerate(Dex3_1_Left_JointIndex):
                left_hand_state_array[idx] = left_hand_msg.motor_state[id].q
            # Update right hand state
            for idx, id in enumerate(Dex3_1_Right_JointIndex):
                right_hand_state_array[idx] = right_hand_msg.motor_state[id].q
        time.sleep(0.002)

# Input monitoring thread
def input_thread():
    global current_state
    while True:
        key = get_key_non_blocking()
        if key == 'q':
            print("Exiting...")
            with state_lock:
                current_state = STATE_STOP
            break
        elif key == 'r':
            with state_lock:
                current_state = STATE_ROTATE
        elif key == 'g':
            with state_lock:
                current_state = STATE_GRIP
        elif key == 'p':
            with state_lock:
                current_state = STATE_PRINT
        elif key == 's':
            with state_lock:
                current_state = STATE_STOP
        time.sleep(0.1)

# Helper: get limits based on hand
def get_limits():
    if is_left_hand:
        return MAX_LIMITS_LEFT, MIN_LIMITS_LEFT
    else:
        return MAX_LIMITS_RIGHT, MIN_LIMITS_RIGHT

# State actions
_count = 0
_dir = 1

def rotate_motors(handcmd_publisher):
    global _count, _dir
    max_lims, min_lims = get_limits()
    # msg = HandCmd_()
    msg = unitree_hg_msg_dds__HandCmd_()

    for i in range(MOTOR_MAX):
        mode = pack_ris_mode(i, status=1)
        msg.motor_cmd[i].mode = mode        
        msg.motor_cmd[i].dq   = 0.0        
        msg.motor_cmd[i].tau = 0.0
        msg.motor_cmd[i].kp = 0.5
        msg.motor_cmd[i].kd = 0.1

        range_val = max_lims[i] - min_lims[i]
        mid = (max_lims[i] + min_lims[i]) / 2.0
        amplitude = range_val / 2.0
        q = mid + amplitude * math.sin(_count / 20000.0 * math.pi)
        msg.motor_cmd[i].q = q

    handcmd_publisher.Write(msg)    

    _count += _dir
    if _count >= 10000:
        _dir = -1
    elif _count <= -10000:
        _dir = 1

    time.sleep(0.0001)  # ~100 µs

def grip_hand(handcmd_publisher):
    max_lims, min_lims = get_limits()    
    msg = unitree_hg_msg_dds__HandCmd_()
    msg.motor_cmd = [type(msg.motor_cmd[0])() for _ in range(MOTOR_MAX)]

    for i in range(MOTOR_MAX):
        mode = pack_ris_mode(i, status=1)
        msg.motor_cmd[i].mode = mode
        msg.motor_cmd[i].tau = 0.0
        msg.motor_cmd[i].dq = 0.0
        msg.motor_cmd[i].kp = 1.5
        msg.motor_cmd[i].kd = 0.1

        mid = (max_lims[i] + min_lims[i]) / 2.0
        msg.motor_cmd[i].q = mid

    handcmd_publisher.Write(msg)
    time.sleep(1.0)  # 1 sec hold

def stop_motors(handcmd_publisher):
    msg = unitree_hg_msg_dds__HandCmd_()
    msg.motor_cmd = [type(msg.motor_cmd[0])() for _ in range(MOTOR_MAX)]

    for i in range(MOTOR_MAX):
        mode = pack_ris_mode(i, status=1, timeout=1)
        msg.motor_cmd[i].mode = mode
        msg.motor_cmd[i].tau = 0.0
        msg.motor_cmd[i].q = 0.0
        msg.motor_cmd[i].dq = 0.0
        msg.motor_cmd[i].kp = 0.0
        msg.motor_cmd[i].kd = 0.0

    handcmd_publisher.Write(msg)
    time.sleep(1.0)

def print_state():
    if latest_state_msg is None:
        print("No state received yet.")
        time.sleep(0.1)
        return

    max_lims, min_lims = get_limits()
    q_norm = []
    for i in range(7):
        q_raw = latest_state_msg.motor_state[i].q
        normalized = (q_raw - min_lims[i]) / (max_lims[i] - min_lims[i])
        normalized = max(0.0, min(1.0, normalized))
        q_norm.append(normalized)

    # Clear screen
    print("\033[2J\033[H", end="")
    print("-- Hand State --")
    print("--- Current State: Test ---")
    print("Commands:")
    print("  r - Rotate")
    print("  g - Grip")
    print("  p - Print state")
    print("  q - Quit")
    hand_label = "L" if is_left_hand else "R"
    print(f" {hand_label}: {np.array(q_norm)}")
    time.sleep(0.1)

# Main
if __name__ == "__main__":
    print(" --- Unitree Robotics --- ")
    print("     Dex3 Hand Example      \n")

    hand_input = input("Please input the hand id (L for left hand, R for right hand): ").strip().upper()
    if hand_input == "L":
        hand_id = 0
        is_left_hand = True
        cmd_topic = "rt/dex3/left/cmd"
        state_topic = "rt/dex3/left/state"
    elif hand_input == "R":
        hand_id = 1
        is_left_hand = False
        cmd_topic = "rt/dex3/right/cmd"
        state_topic = "rt/dex3/right/state"
    else:
        print("Invalid hand id. Please input 'L' or 'R'.")
        sys.exit(1)

    ChannelFactoryInitialize(1)
    # initialize handcmd publisher and handstate subscriber
    LeftHandCmb_publisher = ChannelPublisher(kTopicDex3LeftCommand, HandCmd_)
    LeftHandCmb_publisher.Init()
    RightHandCmb_publisher = ChannelPublisher(kTopicDex3RightCommand, HandCmd_)
    RightHandCmb_publisher.Init()

    LeftHandState_subscriber = ChannelSubscriber(kTopicDex3LeftState, HandState_)
    LeftHandState_subscriber.Init()
    RightHandState_subscriber = ChannelSubscriber(kTopicDex3RightState, HandState_)
    RightHandState_subscriber.Init()

    # Shared Arrays for hand states
    left_hand_state_array  = Array('d', Dex3_Num_Motors, lock=True)  
    right_hand_state_array = Array('d', Dex3_Num_Motors, lock=True)

    # initialize subscribe thread
    subscribe_state_thread = threading.Thread(target=subscribe_hand_state, args=(LeftHandState_subscriber, RightHandState_subscriber, left_hand_state_array, right_hand_state_array))
    subscribe_state_thread.daemon = True
    subscribe_state_thread.start()

    while True:
        if any(left_hand_state_array) and any(right_hand_state_array):
            break
        time.sleep(0.01)
        print("[Dex3_1_Controller] Waiting to subscribe dds...")
    print("[Dex3_1_Controller] Subscribe dds ok.")

    hand_control_process = Process(target=input_thread)
    hand_control_process.daemon = True
    hand_control_process.start()

    print("Initialize Dex3_1_Controller OK!")

    # # Initialize SDK
    # factory = ChannelFactory()
    # factory.Init(0, None)

    # # Create publisher and subscriber
    # handcmd_publisher = factory.CreatePublisher(HandCmd_, cmd_topic)
    # handstate_subscriber = factory.CreateSubscriber(HandState_, state_topic, hand_state_callback)

    # # Initialize message structures
    # init_msg = HandCmd_()
    # init_msg.motor_cmd = [type(init_msg.motor_cmd[0])() for _ in range(MOTOR_MAX)]
    # handcmd_publisher.Write(init_msg)  # Optional warm-up

    # # Start input thread
    # input_t = threading.Thread(target=input_thread, daemon=True)
    # input_t.start()

    print("\nCommands:")
    print("  r - Rotate")
    print("  g - Grip")
    print("  p - Print state")
    print("  s - Stop")
    print("  q - Quit\n")

    last_state = -1
    while True:
        with state_lock:
            state = current_state

        if state != last_state:
            print(f"\n--- Current State: {STATE_NAMES.get(state, 'UNKNOWN')} ---")
            last_state = state

        if state == STATE_INIT:
            print("Initializing...")
            with state_lock:
                current_state = STATE_ROTATE
        elif state == STATE_ROTATE:
            rotate_motors(LeftHandCmb_publisher)
        elif state == STATE_GRIP:
            grip_hand(LeftHandCmb_publisher)
        elif state == STATE_STOP:
            stop_motors(LeftHandCmb_publisher)
            break
        elif state == STATE_PRINT:
            print_state()
        else:
            print("Invalid state!")
            break

    print("Shutting down...")
    # input_t.join(timeout=1)
    hand_control_process.join(timeout=1)
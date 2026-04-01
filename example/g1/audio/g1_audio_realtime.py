import sys
import time
import threading
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
import sounddevice as sd
import numpy as np

# Audio settings (must match robot's expected format)
SAMPLE_RATE = 16000
NUM_CHANNELS = 1
CHUNK_SIZE = 1024  # Samples per chunk for streaming

def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <network_interface>")
        print("Example: python3 pc_mic_to_robot_speaker.py eth0")
        sys.exit(1)

    net_interface = sys.argv[1]

    # Initialize Unitree Audio Client
    ChannelFactoryInitialize(0, net_interface)
    audioClient = AudioClient()
    audioClient.SetTimeout(10.0)
    audioClient.Init()

    audioClient.SetVolume(100)
    
    print("[INFO] Audio client initialized")
    print("[INFO] Press Ctrl+C to stop streaming")
    
    # Generate a unique stream ID
    import uuid
    stream_id = f"pc_mic_{uuid.uuid4().hex[:8]}"
    
    # Flag to control streaming
    streaming = True
    
    # Start playback stream on robot (prepare to receive audio)
    ret = audioClient.PlayStream(stream_id, str(int(time.time() * 1000)), [])
    print(f"[INFO] PlayStream initialized, ret={ret}")
    
    # Callback function for microphone input
    def audio_callback(indata, frames, time_info, status):
        if not streaming:
            return
        
        if status:
            print(f"[WARNING] {status}")
        
        # Convert numpy array to list of bytes for sending to robot
        # indata is int16 PCM data
        pcm_bytes = indata.tobytes()
        
        # Send to robot speakers
        ret = audioClient.PlayStream(
            stream_id, 
            str(int(time.time() * 1000)), 
            list(pcm_bytes)
        )
        if ret != 0:
            print(f"[ERROR] PlayStream failed with ret={ret}")
    
    # Start microphone capture stream
    print("[INFO] Starting microphone capture...")
    print("[INFO] Speak into your PC microphone, audio will play on robot speakers")
    
    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=NUM_CHANNELS,
            dtype='int16',
            blocksize=CHUNK_SIZE,
            callback=audio_callback
        ):
            # Keep running until Ctrl+C
            while streaming:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[INFO] Stopping...")
        streaming = False
    except Exception as e:
        print(f"[ERROR] Audio stream error: {e}")
        streaming = False
    finally:
        # Stop playback on robot
        time.sleep(0.5)  # Allow final buffers to play
        ret = audioClient.PlayStop(stream_id)
        print(f"[INFO] Playback stopped, ret={ret}")

if __name__ == "__main__":
    main()
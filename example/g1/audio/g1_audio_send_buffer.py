import sys
import time
import struct
import threading
import queue
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

import pyaudio

# Audio configuration (MUST match robot requirements)
SAMPLE_RATE = 16000       # Hz - required by SDK
NUM_CHANNELS = 1          # mono - required by SDK
BITS_PER_SAMPLE = 16      # required by SDK
BYTES_PER_SAMPLE = 2
CHUNK_SAMPLES = 512       # 512 samples = 32ms at 16kHz (optimal for low latency)
CHUNK_BYTES = CHUNK_SAMPLES * NUM_CHANNELS * BYTES_PER_SAMPLE  # 1024 bytes

# Streaming configuration
STREAM_NAME = "mic_stream"
BUFFER_SIZE = 20          # Queue size: ~640ms of audio buffer for jitter absorption


class AudioStreamer:
    def __init__(self, net_interface: str):
        self.net_interface = net_interface
        self.audio_client = None
        self.stream_id = None
        self.running = False
        self.audio_queue = queue.Queue(maxsize=BUFFER_SIZE)
        self.capture_thread = None
        self.send_thread = None
        
    def init_sdk(self):
        """Initialize Unitree SDK and AudioClient"""
        ChannelFactoryInitialize(0, self.net_interface)
        self.audio_client = AudioClient()
        self.audio_client.SetTimeout(5.0)
        if self.audio_client.Init() != 0:
            raise RuntimeError("Failed to initialize AudioClient")
        self.audio_client.SetVolume(100)
        # Generate persistent stream_id for continuous playback
        self.stream_id = f"stream_{int(time.time() * 1000)}"
        print(f"[INFO] SDK initialized, stream_id: {self.stream_id}")
        
    def _capture_callback(self, in_data, frame_count, time_info, status):
        """PyAudio callback: capture microphone data and queue it"""
        if self.running and not self.audio_queue.full():
            self.audio_queue.put(in_data)
        return (None, pyaudio.paContinue)
    
    def _capture_thread_func(self):
        """Thread: capture audio from microphone"""                
        p = pyaudio.PyAudio()
        
        # Find input device (default or by name)
        device_index = None
        for i in range(p.get_device_count()):
            dev_info = p.get_device_info_by_index(i)
            if dev_info['maxInputChannels'] > 0 and 'microphone' in dev_info['name'].lower():
                device_index = i
                break
        
        if device_index is None:
            print("[ERROR] No microphone input device found")
            return
            
        stream = p.open(
            format=p.get_format_from_width(BITS_PER_SAMPLE // 8),
            channels=NUM_CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=CHUNK_SAMPLES,
            stream_callback=self._capture_callback,
            start=False
        )
        
        print(f"[INFO] Microphone capture started: {SAMPLE_RATE}Hz, {CHUNK_SAMPLES} samples/frame")
        stream.start_stream()
        
        while self.running:
            time.sleep(0.01)  # Keep thread alive
            
        stream.stop_stream()
        stream.close()
        p.terminate()
        print("[INFO] Microphone capture stopped")
    
    def _send_thread_func(self):
        """Thread: send queued audio chunks to robot with proper timing"""
        chunk_duration = CHUNK_SAMPLES / SAMPLE_RATE  # ~0.032 seconds
        next_send_time = time.time()
        
        print(f"[INFO] Audio sender started: sending every {chunk_duration*1000:.1f}ms")
        
        while self.running:
            try:
                # Non-blocking get with timeout to allow clean shutdown
                chunk = self.audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue
                
            # Send chunk to robot
            ret_code, _ = self.audio_client.PlayStream(
                STREAM_NAME, 
                self.stream_id,  # Same ID = continuous playback!
                list(chunk)       # Convert bytes to list[int] as SDK expects
            )
            
            if ret_code != 0:
                print(f"[WARN] Send failed (code {ret_code}), attempting recovery...")
                # Optional: re-init client here if needed
                
            self.audio_queue.task_done()
            
            # Timing control: send at audio rate, not as fast as possible
            next_send_time += chunk_duration
            sleep_time = next_send_time - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
            # If behind schedule, skip sleep to catch up (prevents buffer buildup)
            
        print("[INFO] Audio sender stopped")
    
    def start(self):
        """Start real-time audio streaming"""
        if self.running:
            return
        self.running = True
        
        self.init_sdk()
        
        # Start capture and send threads
        self.capture_thread = threading.Thread(target=self._capture_thread_func, daemon=True)
        self.send_thread = threading.Thread(target=self._send_thread_func, daemon=True)
        
        self.capture_thread.start()
        self.send_thread.start()
        print(f"[INFO] Streaming started: mic → robot ({SAMPLE_RATE}Hz, {CHUNK_SAMPLES} samples/chunk)")
    
    def stop(self):
        """Stop streaming gracefully"""
        if not self.running:
            return
        print("[INFO] Stopping stream...")
        self.running = False
        
        # Wait for threads to finish
        if self.capture_thread:
            self.capture_thread.join(timeout=2.0)
        if self.send_thread:
            self.send_thread.join(timeout=2.0)
            
        # Stop playback on robot
        if self.audio_client:
            self.audio_client.PlayStop(STREAM_NAME)
            print("[INFO] Robot playback stopped")
    
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <network_interface>")
        print(f"Example: python3 {sys.argv[0]} eth0")
        sys.exit(1)
    
    net_interface = sys.argv[1]
    
    try:
        with AudioStreamer(net_interface) as streamer:
            print("[INFO] Streaming active. Press Ctrl+C to stop...")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user")
    except Exception as e:
        print(f"[ERROR] Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":    
    main()
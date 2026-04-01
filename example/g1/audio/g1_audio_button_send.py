import sys
import threading
import pyaudio
import time
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
# Keep your existing local wav module for the play_pcm_stream function
from wav import play_pcm_stream 

# Audio Configuration (Must match Robot expectations)
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000
CHUNK = 1024

def record_audio():
    """
    Records audio from the default microphone until the user stops it.
    Returns: (pcm_list, sample_rate, num_channels, is_ok)
    """
    p = pyaudio.PyAudio()
    frames = []
    is_recording = threading.Event()
    recording_thread = None

    def audio_callback(in_data, frame_count, time_info, status):
        if is_recording.is_set():
            frames.append(in_data)
        return (None, pyaudio.paContinue)

    def record_worker():
        stream = p.open(format=FORMAT,
                        channels=CHANNELS,
                        rate=RATE,
                        input=True,
                        frames_per_buffer=CHUNK,
                        stream_callback=audio_callback)
        stream.start_stream()
        
        # Keep stream open while recording event is set
        while is_recording.is_set() or stream.is_active():
            if not is_recording.is_set() and not stream.is_active():
                break
            time.sleep(0.1)
            
        stream.stop_stream()
        stream.close()

    try:
        print("--- AUDIO RECORDER ---")
        input("Press [ENTER] to start recording...")
        
        print("🔴 Recording... (Speak now)")
        is_recording.set()
        
        # Start the recording thread
        recording_thread = threading.Thread(target=record_worker)
        recording_thread.start()
        
        # Wait for user to stop
        input("Press [ENTER] to stop recording...")
        
        print("⏹ Stopping...")
        is_recording.clear()
        recording_thread.join()
        p.terminate()

        if len(frames) == 0:
            print("[ERROR] No audio data recorded.")
            return [], 0, 0, False

        # Combine chunks into a list (matching read_wav behavior)
        # play_pcm_stream likely expects a list of byte chunks or a single buffer
        # We pass the list of chunks to be safe.
        return frames, RATE, CHANNELS, True

    except Exception as e:
        print(f"[ERROR] Recording failed: {e}")
        p.terminate()
        return [], 0, 0, False

def main():
    # Updated Usage: Only need network interface now
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <network_interface>")
        print(f"Example: python3 {sys.argv[0]} enp3s0")
        sys.exit(1)

    net_interface = sys.argv[1]

    # 1. Initialize Unitree Channel
    ChannelFactoryInitialize(0, net_interface)
    audioClient = AudioClient()
    audioClient.SetTimeout(10.0)
    audioClient.Init()
    audioClient.SetVolume(100)

    # 2. Record Audio (Replaces read_wav)
    pcm_list, sample_rate, num_channels, is_ok = record_audio()
    
    print(f"[DEBUG] Record success: {is_ok}")
    print(f"[DEBUG] Sample rate: {sample_rate} Hz")
    print(f"[DEBUG] Channels: {num_channels}")
    # Calculate total bytes for debug info
    total_bytes = sum(len(chunk) for chunk in pcm_list) if is_ok else 0
    print(f"[DEBUG] Total PCM bytes: {total_bytes}")
    
    if not is_ok or sample_rate != 16000 or num_channels != 1:
        print("[ERROR] Recording format mismatch or failed (must be 16kHz mono)")
        return

    # 3. Send to Robot
    print("[INFO] Sending audio to robot...")
    play_pcm_stream(audioClient, pcm_list, "example")

    # 4. Cleanup
    # Note: Depending on your wav.py implementation, PlayStop might be needed 
    # after the stream finishes naturally, or to interrupt it.
    # Keeping it from your original example:
    audioClient.PlayStop("example")
    print("[INFO] Done.")

if __name__ == "__main__":
    main()
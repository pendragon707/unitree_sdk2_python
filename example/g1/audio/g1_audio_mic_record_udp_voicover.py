import sys
import socket
import struct
import time
import threading

# from wav import record_pcm_multicast_to_wav

# Try to import sounddevice for live playback
try:
    import sounddevice as sd
    import numpy as np
    HAS_AUDIO = True
except ImportError:
    HAS_AUDIO = False
    print("[WARNING] sounddevice or numpy not found. Live playback disabled. Install via: pip install sounddevice numpy")

from wav import write_wave, record_pcm_multicast_to_wav # Assuming your existing wav module

def main():
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <network_interface_ip> [seconds] [output_wav]")
        print("")
        print("Modes:")
        print("  1. Save to File: python script.py 192.168.123.222 5 /tmp/record.wav")
        print("  2. Live Voiceover: python script.py 192.168.123.222  (No filename = Live Mode)")
        print("  3. Live + Save:    python script.py 192.168.123.222 5 /tmp/record.wav --live")
        sys.exit(1)

    iface_ip = sys.argv[1]
    
    # Parse optional arguments
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].replace('.','',1).isdigit() else 5.0
    out_wav = None
    force_live = False

    # Simple argument parsing logic
    arg_idx = 2
    if len(sys.argv) > 2:
        if sys.argv[2].replace('.','',1).isdigit():
            seconds = float(sys.argv[2])
            arg_idx = 3
        else:
            seconds = 5.0
            
    if len(sys.argv) > arg_idx:
        if sys.argv[arg_idx] == "--live":
            force_live = True
            if len(sys.argv) > arg_idx + 1:
                out_wav = sys.argv[arg_idx+1]
        else:
            out_wav = sys.argv[arg_idx]
            if len(sys.argv) > arg_idx + 1 and sys.argv[arg_idx+1] == "--live":
                force_live = True

    # Determine Mode
    # If no output file is specified OR --live flag is present -> Live Mode
    is_live_mode = (out_wav is None) or force_live
    
    if is_live_mode:
        if not HAS_AUDIO:
            print("[ERROR] Cannot start Live Voiceover: 'sounddevice' library missing.")
            print("Please install: pip install sounddevice numpy")
            sys.exit(1)
        print(f">>> MODE: LIVE VOICEOVER (Playing audio from robot mic) <<<")
        if out_wav:
            print(f"Also saving to: {out_wav}")
        else:
            print("Press Ctrl+C to stop.")
            
        run_live_voiceover(
            group_ip="239.168.123.161",
            port=5555,
            iface_ip=iface_ip,
            sample_rate=16000,
            num_channels=1,
            save_path=out_wav,
            max_seconds=seconds if out_wav else None # If saving, limit time. If pure live, run forever (None)
        )
    else:
        print(f">>> MODE: SAVE TO FILE ONLY <<<")
        record_pcm_multicast_to_wav(
            output_wav=out_wav,
            group_ip="239.168.123.161",
            port=5555,
            iface_ip=iface_ip,
            record_seconds=seconds,
            sample_rate=16000,
            num_channels=1,
        )

def run_live_voiceover(
    group_ip: str,
    port: int,
    iface_ip: str,
    sample_rate: int,
    num_channels: int,
    save_path: str = None,
    max_seconds: float = None
):
    """
    Receives UDP multicast audio and plays it live using sounddevice.
    Optionally saves to a file simultaneously.
    """
    bytes_per_sec = sample_rate * num_channels * 2
    target_bytes = int(max_seconds * bytes_per_sec) if max_seconds else -1
    
    print(f"[INFO] Joining multicast {group_ip}:{port} on {iface_ip}")
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass

        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
        sock.bind(("", port))

        mreq = struct.pack("=4s4s", socket.inet_aton(group_ip), socket.inet_aton(iface_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        
        sock.settimeout(2.0) # Timeout to allow checking stop conditions

        # Setup Audio Stream
        # dtype='int16' matches the 16-bit PCM data from the robot
        stream = sd.RawOutputStream(samplerate=sample_rate, channels=num_channels, dtype='int16')
        stream.start()
        print("[AUDIO] Playback stream started.")

        # Setup File Saving (if requested)
        file_handle = None
        if save_path:
            # We will write raw PCM first, then wrap in WAV later, 
            # or use a library that supports streaming WAV writing.
            # For simplicity here, we collect bytes to write a valid WAV at the end 
            # OR write raw and convert. Let's collect for valid WAV if duration is known,
            # otherwise just play. 
            # To keep it simple and robust for infinite streaming: 
            # We will write RAW pcm, then you can convert later, OR use a threaded WAV writer.
            # Here: Let's just save the raw pcm to a temp file and convert on exit if needed,
            # but since we want a .wav, let's assume finite recording if save_path is provided.
            all_data = bytearray() if max_seconds else None 
            if not max_seconds:
                print("[WARN] Saving infinite stream to WAV is not possible without truncation. Saving as RAW .pcm instead.")
                save_path = save_path.replace(".wav", ".pcm")
                file_handle = open(save_path, "wb")
            else:
                print(f"[INFO] Will save {max_seconds}s to {save_path} after playback.")

        total_bytes = 0
        t0 = time.time()
        running = True

        while running:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception as e:
                print(f"Socket error: {e}")
                break
            
            # Ensure even length for 16-bit
            if len(data) % 2 == 1:
                data = data[:-1]

            # 1. Play Live
            try:
                stream.write(data)
            except Exception as e:
                print(f"Audio playback error: {e}")
                break

            # 2. Save to File (if requested)
            if save_path:
                if max_seconds:
                    if all_data is not None:
                        all_data.extend(data)
                else:
                    if file_handle:
                        file_handle.write(data)

            total_bytes += len(data)

            # Check duration limit
            if max_seconds and total_bytes >= target_bytes:
                print(f"\n[INFO] Reached {max_seconds}s limit.")
                running = False
            
            # Optional: Check elapsed time for safety in infinite mode if needed
            if max_seconds is None and (time.time() - t0) > 3600: # Safety break after 1 hour
                print("\n[INFO] 1 hour limit reached for safety.")
                running = False

        stream.stop()
        stream.close()
        print("[AUDIO] Playback stopped.")

        # Finalize Save
        if save_path and max_seconds and all_data is not None:
            # Convert collected bytes to WAV
            from wav import write_wave # Assuming your existing function takes list of ints or bytes
            # We need to convert bytearray to list of ints for your existing write_wave if it expects that
            # Or modify write_wave to accept bytes. 
            # Assuming write_wave(output, rate, samples_list, channels)
            # Convert bytes to list of 16-bit signed ints
            import array
            samples = array.array('h', all_data) # 'h' is signed short (16-bit)
            
            ok = write_wave(save_path, sample_rate, samples.tolist(), num_channels=num_channels)
            if ok:
                print(f"[INFO] Saved WAV to {save_path}")
            else:
                print("[ERROR] Failed to write WAV file.")
        
        elif save_path and not max_seconds and file_handle:
            file_handle.close()
            print(f"[INFO] Saved RAW audio to {save_path} (convert to wav manually if needed)")

    finally:
        sock.close()

# Keep your original function for backward compatibility
def record_pcm_multicast_to_wav(
    output_wav: str,
    group_ip: str = "239.168.123.161",
    port: int = 5555,
    iface_ip: str = "192.168.123.222",
    record_seconds: float = 5.0,
    sample_rate: int = 16000,
    num_channels: int = 1,
    recv_buf_bytes: int = 65536,
    socket_timeout_sec: float = 5.0,
):
    import socket, struct, time
    # Import your existing write_wave from wav module
    from wav import write_wave 

    bytes_per_sec = sample_rate * num_channels * 2
    target_bytes = int(record_seconds * bytes_per_sec)

    print(f"[INFO] multicast group={group_ip} port={port} iface_ip={iface_ip}")
    print(f"[INFO] recording {record_seconds}s -> target_bytes={target_bytes}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass

        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
        sock.bind(("", port))

        mreq = struct.pack("=4s4s", socket.inet_aton(group_ip), socket.inet_aton(iface_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        sock.settimeout(socket_timeout_sec)

        total = 0
        pcm_samples = []
        t0 = time.time()
        print("[INFO] start record!")

        while total < target_bytes:
            try:
                data, _ = sock.recvfrom(recv_buf_bytes)
            except socket.timeout:
                raise RuntimeError("Timed out waiting for mic packets. Stream may be off or interface join is wrong.")

            if not data:
                continue
            if len(data) % 2 == 1:
                data = data[:-1]

            sample_count = len(data) // 2
            pcm_samples.extend(struct.unpack("<" + "h" * sample_count, data))
            total += len(data)

        elapsed = time.time() - t0
        print(f"[INFO] record finish! received_bytes={total} elapsed={elapsed:.2f}s samples={len(pcm_samples)}")

        ok = write_wave(output_wav, sample_rate, pcm_samples, num_channels=num_channels)
        if not ok:
            raise RuntimeError("write_wave failed")

        print(f"[INFO] saved to {output_wav}")

    finally:
        sock.close()

if __name__ == "__main__":
    main()
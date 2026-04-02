#!/usr/bin/env python3
"""
Continuous real-time audio receiver: Unitree G1 robot mic → laptop speakers
Handles UDP multicast jitter, packet loss, and timing for clean playback.

Usage:
    python3 g1_audio_receive_continuous.py <iface_ip> [output_wav_for_debug]
    Example: python3 g1_audio_receive_continuous.py 192.168.123.99 /tmp/debug.wav
"""

import sys
import time
import struct
import socket
import threading
import queue
import array
from collections import deque

# ============== AUDIO CONFIG (MUST MATCH ROBOT) ==============
SAMPLE_RATE = 16000           # Hz - fixed by SDK
NUM_CHANNELS = 1              # mono
BITS_PER_SAMPLE = 16          # 16-bit PCM
BYTES_PER_SAMPLE = 2
BYTES_PER_FRAME = NUM_CHANNELS * BYTES_PER_SAMPLE  # 2 bytes

# Packet format from robot: [seq_num:4 bytes][timestamp:8 bytes][pcm_data:N bytes]
# If your robot sends raw PCM without headers, set USE_PACKET_HEADER = False
USE_PACKET_HEADER = True
HEADER_SIZE = 12 if USE_PACKET_HEADER else 0
EXPECTED_PAYLOAD_SIZE = 512 * BYTES_PER_FRAME  # Adjust to match robot's chunk size

# ============== STREAMING CONFIG ==============
MULTICAST_GROUP = "239.168.123.201"
MULTICAST_PORT = 5555
JITTER_BUFFER_SIZE = 50       # Packets: ~1.6s buffer at 32ms/packet (adjustable)
PLAYBACK_BUFFER_MIN = 10      # Min packets before starting playback (avoid underrun)
MAX_GAP_PACKETS = 5           # Tolerate up to N missing packets before inserting silence
STATS_INTERVAL = 5.0          # Print stats every N seconds

# ============== GLOBAL STATE ==============
class StreamStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.received_packets = 0
        self.lost_packets = 0
        self.out_of_order = 0
        self.gaps_filled = 0
        self.playback_underruns = 0
        self.last_seq = None
        self.start_time = time.time()
    
    def update(self, seq_num, expected_seq):
        with self.lock:
            self.received_packets += 1
            if self.last_seq is not None and seq_num != expected_seq:
                if seq_num > expected_seq:
                    lost = seq_num - expected_seq
                    self.lost_packets += lost
                    self.gaps_filled += min(lost, MAX_GAP_PACKETS)
                else:
                    self.out_of_order += 1
            self.last_seq = seq_num
    
    def report_underrun(self):
        with self.lock:
            self.playback_underruns += 1
    
    def print_report(self):
        with self.lock:
            elapsed = time.time() - self.start_time
            rate = self.received_packets / elapsed if elapsed > 0 else 0
            loss_rate = (self.lost_packets / (self.received_packets + self.lost_packets) * 100) if (self.received_packets + self.lost_packets) > 0 else 0
            print(f"\n[STATS] t={elapsed:.1f}s | recv={self.received_packets} ({rate:.1f}pkt/s) | "
                  f"lost={self.lost_packets} ({loss_rate:.1f}%) | ooo={self.out_of_order} | "
                  f"gaps={self.gaps_filled} | underruns={self.playback_underruns}")


class AudioReceiver:
    def __init__(self, iface_ip: str, debug_wav: str = None):
        self.iface_ip = iface_ip
        self.debug_wav = debug_wav
        self.stats = StreamStats()
        
        # Threading control
        self.running = False
        self.recv_thread = None
        self.play_thread = None
        
        # Jitter buffer: deque of (seq_num, pcm_bytes)
        self.jitter_buffer = deque(maxlen=JITTER_BUFFER_SIZE)
        self.buffer_lock = threading.Lock()
        
        # Playback queue: ready-to-play PCM chunks (bytes)
        self.play_queue = queue.Queue(maxsize=JITTER_BUFFER_SIZE)
        
        # Sequence tracking
        self.expected_seq = None
        self.playback_started = False
        
        # Debug recording
        self.debug_samples = []
        self.debug_lock = threading.Lock()
        
    def _setup_multicast_socket(self):
        """Create and configure UDP multicast socket"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass  # Not all platforms support SO_REUSEPORT
        
        # Bind to specific interface
        sock.bind(("", MULTICAST_PORT))
        
        # Join multicast group on specified interface
        mreq = struct.pack("=4s4s", socket.inet_aton(MULTICAST_GROUP), socket.inet_aton(self.iface_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        
        # Critical: set reasonable timeout to allow clean shutdown
        sock.settimeout(1.0)
        
        print(f"[INFO] Multicast socket: {MULTICAST_GROUP}:{MULTICAST_PORT} on {self.iface_ip}")
        return sock
    
    def _parse_packet(self, data: bytes):
        """Extract sequence number and PCM data from robot packet"""
        print(f"[DEBUG] Packet: {len(data)} bytes, first 20: {data[:20].hex()}")

        if USE_PACKET_HEADER:
            if len(data) < HEADER_SIZE:
                return None, None
            seq_num = struct.unpack("<I", data[0:4])[0]
            # timestamp = struct.unpack("<Q", data[4:12])[0]  # Optional: use for latency tracking
            pcm_data = data[HEADER_SIZE:]
        else:
            # Raw PCM: generate synthetic sequence number (not ideal, but works if packets arrive in order)
            seq_num = None
            pcm_data = data
        
        # Validate PCM size
        if len(pcm_data) % BYTES_PER_FRAME != 0:
            print(f"[WARN] Packet size {len(pcm_data)} not multiple of frame size")
            pcm_data = pcm_data[:len(pcm_data) - (len(pcm_data) % BYTES_PER_FRAME)]
        
        return seq_num, pcm_data
    
    def _receive_thread_func(self):
        """Thread: receive UDP packets, reorder, and feed jitter buffer"""
        sock = self._setup_multicast_socket()
        local_expected_seq = 0  # Thread-local sequence tracking
        
        print("[INFO] Receiver thread started")
        
        while self.running:
            try:
                data, addr = sock.recvfrom(65536)
            except socket.timeout:
                continue  # Check running flag
            except OSError as e:
                print(f"[ERROR] Socket error: {e}")
                break
            
            seq_num, pcm_data = self._parse_packet(data)
            if pcm_data is None or len(pcm_data) == 0:
                continue
            
            # Update stats and track sequence
            if seq_num is not None:
                self.stats.update(seq_num, local_expected_seq)
                if local_expected_seq is None:
                    local_expected_seq = seq_num
                elif seq_num >= local_expected_seq:
                    local_expected_seq = seq_num + 1
                # else: out-of-order, handled by stats
            
            # Add to jitter buffer with lock
            with self.buffer_lock:
                if seq_num is not None:
                    # Insert in order (simple approach: append and sort periodically)
                    self.jitter_buffer.append((seq_num, pcm_data))
                    # Keep buffer sorted by sequence number
                    if len(self.jitter_buffer) > 10:
                        self.jitter_buffer = deque(
                            sorted(self.jitter_buffer, key=lambda x: x[0]), 
                            maxlen=JITTER_BUFFER_SIZE
                        )
                else:
                    # No sequence: just append (risk of disorder)
                    self.jitter_buffer.append((time.time(), pcm_data))
            
            # Debug recording
            if self.debug_wav:
                with self.debug_lock:
                    samples = struct.unpack("<" + "h" * (len(pcm_data) // 2), pcm_data)
                    self.debug_samples.extend(samples)
        
        sock.close()
        print("[INFO] Receiver thread stopped")
    
    def _reorder_and_feed_playback(self):
        """Extract ordered packets from jitter buffer and feed playback queue"""
        with self.buffer_lock:
            if len(self.jitter_buffer) < PLAYBACK_BUFFER_MIN:
                return  # Wait for buffer to fill
            
            # Extract packets in order
            sorted_packets = sorted(self.jitter_buffer, key=lambda x: x[0] if isinstance(x[0], int) else float('inf'))
            
            for seq_num, pcm_data in sorted_packets:
                # Check for gaps
                if self.expected_seq is not None and isinstance(seq_num, int):
                    gap = seq_num - self.expected_seq
                    if gap > 1 and gap <= MAX_GAP_PACKETS:
                        # Insert silence for missing packets
                        silence_frames = (gap - 1) * (EXPECTED_PAYLOAD_SIZE // BYTES_PER_FRAME)
                        silence = b'\x00' * (silence_frames * BYTES_PER_FRAME)
                        if not self.play_queue.full():
                            self.play_queue.put(silence)
                        self.stats.gaps_filled += 1
                    elif gap > MAX_GAP_PACKETS:
                        # Large gap: reset expected seq to avoid massive silence
                        print(f"[WARN] Large gap detected: {gap} packets, resetting sync")
                
                if isinstance(seq_num, int):
                    self.expected_seq = seq_num + 1
                
                # Feed to playback queue
                if not self.play_queue.full():
                    self.play_queue.put(pcm_data)
                else:
                    # Buffer full: drop oldest (adaptive)
                    if not self.play_queue.empty():
                        try:
                            self.play_queue.get_nowait()
                        except queue.Empty:
                            pass
                        self.play_queue.put(pcm_data)
            
            # Clear processed packets from jitter buffer
            self.jitter_buffer.clear()
    
    def _playback_thread_func(self):
        """Thread: play audio from queue with precise timing"""
        import pyaudio
        
        p = pyaudio.PyAudio()
        chunk_duration = EXPECTED_PAYLOAD_SIZE / (SAMPLE_RATE * BYTES_PER_FRAME)  # seconds
        
        stream = p.open(
            format=p.get_format_from_width(BITS_PER_SAMPLE // 8),
            channels=NUM_CHANNELS,
            rate=SAMPLE_RATE,
            output=True,
            frames_per_buffer=EXPECTED_PAYLOAD_SIZE // BYTES_PER_FRAME,
        )
        
        print(f"[INFO] Playback thread started: {SAMPLE_RATE}Hz, {chunk_duration*1000:.1f}ms/chunk")
        next_play_time = time.time()
        consecutive_underruns = 0
        
        while self.running:
            # Ensure jitter buffer is processed
            self._reorder_and_feed_playback()
            
            try:
                pcm_chunk = self.play_queue.get(timeout=0.1)
            except queue.Empty:
                # Buffer underrun
                if self.playback_started:
                    consecutive_underruns += 1
                    self.stats.report_underrun()
                    if consecutive_underruns > 3:
                        print(f"[WARN] Playback underrun x{consecutive_underruns}, inserting silence")
                        pcm_chunk = b'\x00' * EXPECTED_PAYLOAD_SIZE
                    else:
                        continue  # Wait for more data
                else:
                    continue  # Still buffering, not ready to play
            
            consecutive_underruns = 0
            
            # Start playback only after buffer is sufficiently filled
            if not self.playback_started and self.play_queue.qsize() >= PLAYBACK_BUFFER_MIN // 2:
                self.playback_started = True
                print("[INFO] Playback started (buffer ready)")
                next_play_time = time.time()  # Reset timing
            
            if not self.playback_started:
                continue
            
            # Play with precise timing
            stream.write(pcm_chunk)
            
            # Schedule next chunk based on audio duration (not wall clock)
            next_play_time += chunk_duration
            sleep_time = next_play_time - time.time()
            if sleep_time > 0.001:  # Only sleep if meaningful
                time.sleep(sleep_time)
            # If behind schedule, skip sleep to catch up (prevents drift)
        
        stream.stop_stream()
        stream.close()
        p.terminate()
        
        # Save debug WAV if requested
        if self.debug_wav and self.debug_samples:
            self._save_debug_wav()
        
        print("[INFO] Playback thread stopped")
    
    def _save_debug_wav(self):
        """Save received audio to WAV file for debugging"""
        try:
            import wave
            with wave.open(self.debug_wav, 'wb') as wf:
                wf.setnchannels(NUM_CHANNELS)
                wf.setsampwidth(BYTES_PER_SAMPLE)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(array.array('h', self.debug_samples).tobytes())
            print(f"[INFO] Debug audio saved to {self.debug_wav} ({len(self.debug_samples)} samples)")
        except Exception as e:
            print(f"[ERROR] Failed to save debug WAV: {e}")
    
    def _stats_thread_func(self):
        """Thread: periodically print streaming statistics"""
        while self.running:
            time.sleep(STATS_INTERVAL)
            if self.running:  # Double-check after sleep
                self.stats.print_report()
    
    def start(self):
        """Start continuous audio reception and playback"""
        if self.running:
            return
        self.running = True
        
        # Start threads
        self.recv_thread = threading.Thread(target=self._receive_thread_func, daemon=True)
        self.play_thread = threading.Thread(target=self._playback_thread_func, daemon=True)
        self.stats_thread = threading.Thread(target=self._stats_thread_func, daemon=True)
        
        self.recv_thread.start()
        self.play_thread.start()
        self.stats_thread.start()
        
        print(f"[INFO] Continuous audio reception started")
        print(f"      Multicast: {MULTICAST_GROUP}:{MULTICAST_PORT}")
        print(f"      Interface: {self.iface_ip}")
        print(f"      Press Ctrl+C to stop")
    
    def stop(self):
        """Stop reception and cleanup"""
        if not self.running:
            return
        print("\n[INFO] Stopping audio reception...")
        self.running = False
        
        # Wait for threads to finish
        for t in [self.recv_thread, self.play_thread, self.stats_thread]:
            if t and t.is_alive():
                t.join(timeout=2.0)
        
        # Final stats
        self.stats.print_report()
        
        # Save any remaining debug audio
        if self.debug_wav and self.debug_samples:
            self._save_debug_wav()


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <iface_ip> [debug_output.wav]")
        print(f"Example: python3 {sys.argv[0]} 192.168.123.99 /tmp/g1_debug.wav")
        sys.exit(1)
    
    iface_ip = sys.argv[1]
    debug_wav = sys.argv[2] if len(sys.argv) > 2 else None
    
    # Validate IP format
    try:
        socket.inet_aton(iface_ip)
    except socket.error:
        print(f"[ERROR] Invalid interface IP: {iface_ip}")
        sys.exit(1)
    
    receiver = None
    try:
        receiver = AudioReceiver(iface_ip, debug_wav)
        receiver.start()
        
        # Main loop: just wait for interrupt
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user")
    except Exception as e:
        print(f"[ERROR] Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        if receiver:
            receiver.stop()


if __name__ == "__main__":
    # Install dependency: pip install pyaudio
    # On Ubuntu: sudo apt install portaudio19-dev python3-dev
    main()
## Audio

Connect the Unitree G1 and the PC using Ethernet. The interface (for example, eth0 or wlo1) must have an IP address in the same subnet as the robot (for example, `192.168.123.222`). You can check the ip using command `ip addr show` or `ifconfig`.

1. Get audio from the robot's microphone 

- **Live Voiceover Only (No file saved)**

```bash
python g1_audio_mic_record_udp_voicover.py wlo1
```

You will hear the robot's microphone immediately through your computer speakers. 

- **Live Voiceover + Save to File (5 seconds)**

```bash
python g1_audio_mic_record_udp_voicover.py wlo1 5 /tmp/robot_voice.wav --live
```

- **Save only, no playback**

```bash
python g1_audio_mic_record_udp_voicover.py wlo1 5 /tmp/robot_voice.wav
```

2. Send audio to the robot's speakers

Install:

```bash
pip install sounddevice numpy
```

If the sound is not playing on the robot (test with g1_audio_client_play_wav.py), do the following (on robot`s PC2):

```bash
# Enable multicast routing
sudo sysctl -w net.ipv4.conf.all.mc_forwarding=1

# Allow IP forwarding
sudo sysctl -w net.ipv4.ip_forward=1

# Check firewall (disable temporarily for testing)
sudo ufw disable
```

- **Send already recorded audio**

```bash
python g1_audio_client_play_wav.py wlo1 test.wav
```

- **Real-time**

```bash
python g1_audio_send_buffer.py wlo1
```

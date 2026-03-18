This example is a test of Unitree G1/H1-2 robot.

**Note:** 
idl/unitree_go is used for Unitree Go2/B2/H1/B2w/Go2w robots
idl/unitree_hg is used for Unitree G1/H1-2 robots

## Audio

Connect the Unitree G1 and the PC using Ethernet. The interface (for example, eth0) must have an IP address in the same subnet as the robot (in my example, `192.168.123.222`). You can check the ip using command `ip addr show` or `ifconfig`.

- Live Voiceover Only (No file saved):

```bash
python g1_audio_mic_record_udp_voicover.py 192.168.123.222 
```

You will hear the robot's microphone immediately through your computer speakers. 

- Live Voiceover + Save to File (5 seconds):

```bash
python g1_audio_mic_record_udp_voicover.py 192.168.123.222 5 /tmp/robot_voice.wav --live
```

- Save only, no playback

```bash
python g1_audio_mic_record_udp_voicover.py 192.168.123.222 5 /tmp/robot_voice.wav
```
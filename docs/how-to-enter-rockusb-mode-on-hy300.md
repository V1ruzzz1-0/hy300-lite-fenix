# How to Enter RockUSB Mode on the HY300 #
User: v1ruzzz1_0
Date: 10/11/2026
Status: Documenting

While I was analyzing the HY300 (RK3326/clone) firmware at runtime, I realized that it wouldn't be very efficient to keep working that way, so I decided to figure out the best way to improve my research.

So I looked for a low-level mode that allows dumps/backups, and I found: RockUSB Mode.

Here's the process (WARNING: YOU ARE RESPONSIBLE FOR ANY DAMAGE!):

# Requirements
1. Cable:
  A. (DIY method): two old phone USB cables with USB-A on one end and anything on the other (it doesn't matter, as long as they carry data and one end is a working USB-A)
  B. (professional method): one USB-A-to-USB-A cable
2. (if you go the DIY route): electrical tape or heat-shrink tubing

# Assembly (DIY method)
1. Cut off the unused ends of the USB cable (e.g. USB-C), keeping the USB-A. Repeat this for the other cable. WARNING: avoid making a long cable. I spent almost 15 minutes trying to figure out why the PC wouldn't recognize the projector, and it just kept showing the errors "device descriptor read/64, error -71" and "Cannot enable. Maybe the USB cable is bad?"
2. Join the wires of the two cables between the two USB-A ends: 5V+ (usually red), GND (usually black), DP (usually green), DM (usually white). WARNING: don't fully trust the colors. If in doubt, check the continuity of the wires against the USB 2.0 standard. And if the cable has more than 4 wires (like USB 3.x cables), you can leave the extra ones disconnected and properly insulated, since we won't use them.
3. Properly insulate the wires with electrical tape or heat-shrink tubing (RECOMMENDED: check that none of the wires have continuity between contacts).
4. You now have a fully DIY USB-A-to-USB-A cable!

# Connection

There are SEVERAL methods here, because depending on your board only some of them may work (or none, if you're unlucky). I strongly recommend trying each one in order.

METHOD 1:
1. Disconnect the projector from AC power and make sure it's fully de-energized (I recommend holding the power button for 5 seconds).
2. Connect one end of the USB-A cable to the projector (WARNING: don't connect it to the PC yet, or connect it to the PC but not to the projector yet).
3. Using a SIM tray ejector pin (or whatever you have on hand), find the hole below the HDMI port (usually marked "HD") and press the internal button inside it, without using too much force.
4. While still holding the button down, connect the cable to the PC (or to the projector, if you haven't connected it yet), and release it only when the device is recognized.

METHOD 2:
1. Disconnect the projector from AC power and make sure it's fully de-energized.
2. Connect one end of the USB-A cable to the projector (WARNING: don't connect it to the PC yet, or connect it to the PC but not to the projector yet).
3. With the pin (or whatever you're using), press the button below the HDMI port.
4. Connect one end of the USB-A cable to the projector and the other to the PC, still holding the button down, and release it only when the device is recognized.

# WARNING: from method 3 onward, the projector is powered by AC, so I strongly recommend using a USB hub with VBUS backfeed protection, because if your projector is from a low-quality batch, it may not implement switching between device mode and host mode.

METHOD 3 (the one that worked for mine):
1. With the projector connected to AC power, put it in standby (with the red light on; make sure it isn't blue).
2. With the projector in standby, connect one end of the USB-A cable to the projector (WARNING: don't connect it to the PC yet, or connect it to the PC but not to the projector yet).
3. With the pin (or whatever you're using), press the button below the HDMI port and immediately connect the USB-A to the PC (or to the projector, if you already connected it to the PC), and release the button only when the device is recognized.

METHOD 4:
1. With the projector connected to AC power, put it in standby (with the red light on; make sure it isn't blue).
2. With the projector in standby, connect one end to the projector and the other to the PC.
3. With the pin (or whatever you're using), press the button below the HDMI port, and release it only when the device is recognized.

# Detection

On the computer, use these commands to confirm that the connection was successful:

- "lsusb"
- "rkdeveloptool l"` (if you have the tool)

This is what you should expect to see:

---
SYS-CORE% lsusb
Bus 001 Device 085: ID 2207:330d Fuzhou Rockchip Electronics Company USB download gadget
Bus 002 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
Bus 003 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
SYS-CORE% sudo rkdeveloptool ld
Bus 001 Device 085: ID 2207:330d Fuzhou Rockchip Electronics Company USB download gadget
DevNo=1 Vid=0x2207,Pid=0x330d,LocationID=102    Loader
SYS-CORE%
---

The idea of this document is to record and teach the procedure for entering RockUSB mode, for future expansion of the project (which is still just research).

/ -- /

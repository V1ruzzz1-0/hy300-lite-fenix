# UART and U-Boot Shell Discovery #
User: v1ruzzz1_0
Date: 10/12/2026
Status: Documenting and Investigating

During the RockUSB Mode investigation, I ended up damaging the USB-A port on my fenix, so I decided to open it up and try to repair the damage.

While opening it, I noticed that the damage had broken the DP and DM traces, so I decided to run a jumper to the resistors the original traces led to.

However, while soldering, I noticed 3 exposed pins with the following labels:
- GND: common ground, identified
- TX: apparently the UART transmitter
- RX: apparently the UART receiver

So I decided to connect my ESP32-C3 (which is running USB-TTL firmware) to those pins to check whether there were any logs. However, I had to use a Python script to find the correct speed for this port, and the answer was: 1502347 baud.

Then I checked the logs, and apparently, with Android already booted, this is what showed up:

---

SYS-CORE% picocom -b 1502347 /dev/ttyACM0 
picocom v3.1 
 
port is        : /dev/ttyACM0 
flowcontrol    : none 
baudrate is    : 1502347 
parity is      : none 
databits are   : 8 
stopbits are   : 1 
escape is      : C-a 
local echo is  : no 
noinit is      : no 
noreset is     : no 
hangup is      : no 
nolock is      : no 
send_cmd is    : sz -vv 
receive_cmd is : rz -vv -E 
imap is        :  
omap is        :  
emap is        : crcrlf,delbs, 
logfile is     : none 
initstring     : none 
exit_after is  : not set 
exit is        : no 
 
Type [C-a] [C-h] to see available commands 
Terminal ready 
[  132.775344] read strings 
[  133.818165] read descriptors 
[  133.818480] read strings 
[  134.861754] read descriptors 
[  134.862068] read strings 
[  135.908716] read descriptors 
[  135.909062] read strings 
[  136.948259] read descriptors 

---

It apparently has to do with FunctionFS receiving USB descriptors and strings, but it was firing nonstop, so I found it a bit odd, though expected for low-cost Chinese hardware.

Then I tried leaving picocom open and, as soon as I powered on the projector, pressing the space key nonstop, until I managed to interrupt the boot and land at:

---

=>

---

From there, I ran help to find out which commands were available.

Among the available commands were:

- printenv: query the environment variables.
- mmc: access the MMC management functions.
- gpt: query and manipulate GPT table information.
- download: enter Rockchip download mode.
- rockusb: use the RockUSB protocol.
- ums: expose USB Mass Storage.
- usbboot: boot a system from a USB device.

(These are only the relevant commands. I won't list them all because there are a lot of them.)

I'm still investigating this device's U-Boot, so more findings will be documented.

And my earlier idea of modifying the U-Boot image to include ums (USB Mass Storage Gadget) becomes an alternative idea, since the current U-Boot clearly already has the ums command, so for now I don't need to edit an image to enter Mass Storage mode.

Oh, and I almost forgot: even after repairing the USB traces, I still haven't managed to get it working, unfortunately. It's probably because my current soldering iron is too big for soldering small points, so the contact may not be great. For now, USB access stays unavailable until I get proper soldering tools, and I won't keep pushing on the repair so I don't make things worse.

So from now on, a good part of the investigation will be done over UART.

# ADB SHELL #

I figured that since it's a small piece of information, it wasn't worth creating another file.

So, apparently, in the developer settings, even though it's Android 11, there's no Wi-Fi debugging, only USB debugging (there is an option called "Internet ADB", but it doesn't work. I confirmed with nmap that there are no open ports related to ADB), but this USB debugging doesn't work either (even with a USB-A-to-USB-A cable).

The idea would be to change the boot parameters in U-Boot so we have ADB access over UART (not just repeated logs).

And yes, I already tried interrupting the logs with Android already loaded, but without success.

/ -- /

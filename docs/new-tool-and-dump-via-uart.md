# New Tool and Dump via UART #
User: v1ruzzz1_0
Date: 10/13/2026
Status: In progress

During the fenix investigation, I started looking for a reliable way to extract the eMMC partitions so I could analyze the original firmware more easily and have a way to recover in case of corruption.

# The RockUSB Mode Problem

Initially, the idea was to use RockUSB Mode to extract the partitions, but it turned out that some of the partitions were coming back corrupted in a pattern, so it didn't look like physical instability.

# The UMS Problem in U-Boot

My next bet was to try U-Boot's USB Mass Storage Gadget (UMS) mode, but the next limitation I identified was the lack of a USB controller implementation in U-Boot, so it wasn't possible to put the device in Gadget mode, or to dump to an external flash drive.

# The UART Alternative

Since I had already managed to get into the U-Boot shell over UART, I decided to use that connection to extract the partition data directly from the eMMC.

The idea is to read the partitions' contents in blocks, transfer that data over the serial connection, and rebuild the image files on the computer.

To automate this process, I started developing a Python tool called fenix-dump.py. I used Claude AI to draft the code based on my idea, and I fixed a few things (like block sizes, command-line mode, fixes for some visual bugs, etc.).

It automates communication with U-Boot, identifies the available partitions, and lets you select which ones to extract. It also has checkpoint and resume mechanisms, plus verification of the files produced.

So far, the tests I've run have given stable results, even with larger images. The tool is still under development, so I don't consider it fully ready, but it's already working normally with the tests I'm running.

The main limitation of this approach is speed. Although the serial connection is set to 1,500,000 baud, the effective transfer rate observed during the dumps is around 30.5 KiB/s. This means extracting larger partitions takes many hours or even days.

Even so, I prefer having an automated way to extract and verify the data over continuing to depend on transfer methods that I still haven't been able to confirm are stable.

I'm also splitting the work into three batches, according to the progress of the investigation and the size of the partitions.

# First Batch:

This batch gathers the first partitions I managed to extract via RockUSB Mode (apart from boot.img), which are public as of this Markdown file:

- security
- uboot
- trust
- misc
- dtbo
- vbmeta
- boot (first test of this new tool)

These images are already available in firmware/original/ and serve as a starting point for analyzing the firmware.

# Second Batch

The second batch gathers six other partitions, all extracted entirely by my tool:

- baseparameter
- zoom
- metadata
- recovery
- backup
- cache

The initial estimate for the full batch is around ten hours, although the actual duration may vary a bit with the speed.

My intention is to publish the tool together with this second batch of dumps, today, as long as I manage to finish the whole extraction of this batch and confirm its integrity.

That way, other people can also examine the files from their own devices, compare them with the one I'm using, and follow the tool's development (and report bugs, in case it's not their machine).

# Third Batch

The third batch will be the most time-consuming. It contains the two largest partitions identified in the eMMC table:

- super
- userdata

Together, they add up to more than 6 GiB of data.

Considering the transfer speed observed so far, reading these partitions continuously should take about four days...... Uh..... That's really a lot (I'm hoping my ThinkPad T480 survives until the end of the transfer). But at least the idea is that if the second batch's dump comes out fully intact, this one probably will too. The problem will be the wait, so the project may get few or no updates from today on.

# Expected Result

I'm quite excited about this stage of the investigation.

So far, limited access to the device's data has restricted what I can analyze. Each newly extracted image expands the possibilities for investigating the firmware's structure and better understanding how the system was built.

Once all the dumps are done and the files have passed the available checks, I hope to have a much more complete data set to continue the work.

I still don't know exactly what I'll find in the larger partitions, or what discoveries they might bring. That's exactly why I'm very curious to get to that stage.

# Porting REALLY Clean Systems (not debloated)

Although I haven't said much about it here (because I don't want to promise too much, I'm still learning, and I don't even know if the project will work out), I plan to port a mainline Linux tree and a clean U-Boot to this device, so it can have a wider variety of projects (like Armbian, a clean Android TV, etc.) and new uses it didn't have (or that were limited, such as a server, a halfway decent projector, a kind of bare-metal game console, or whatever this hardware can physically do).

And unfortunately, in the low-cost Chinese projector market it's very common for manufacturers to use Android and not release the tree they used, which makes these kinds of ports harder, but at least it's considerably fun to do.

# Wrapping Up

When this second batch is done and I have free access to the projector, I REALLY want to try injecting Magisk into boot.img to explore the userspace better.

/ -- /

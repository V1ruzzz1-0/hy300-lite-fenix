# Problem Reading Partitions in RockUSB Mode #
User: v1ruzzz1_0
date: 10/11/2026
Status: Investigating

While analyzing the integrity of the HY300 eMMC dump (RK3326/Clone), I noticed that some of the partitions were coming back partially or completely filled with "0xCC" instead of valid data (which is definitely not normal or expected for these partitions).

The affected partitions were:
- recovery (~96 MiB)
- backup (~384 MiB)
- cache (~384 MiB)
- metadata (~16 MiB)
- baseparameter (~1 MiB)
- zoom (~10 MiB)
- super (~2.34 GiB)
- userdata (~3.99 GiB)
- boot (~49 MiB)

So I decided to check whether this was a read failure specific to that one dump or something systematic. I redid the dump, and the anomalies repeated in exactly the same way.

Right after that, I wrote Python scripts to check whether the contamination affected the whole file, and apparently almost all of the files were completely contaminated. Some (e.g. boot.img itself) were almost entirely contaminated, but still contain valid data, which means there seems to be a pattern that needs to be investigated.

Honestly, I still don't know what it could be, but I can say it may be caused by something related to RockUSB mode, since the projector's Android boots normally, so the partitions are physically intact.

For now I haven't tried to push on the problem or change anything on the device. The idea is to only start modifying partitions once I have a stable dump first, because the original firmware for this projector isn't easy to find, which makes it valuable and the only recovery (and investigation) key if something goes wrong.

So I can't just try rewriting partitions when we don't yet have a clear way to recover them.

My next plan is to focus on U-Boot, since from the dump I managed to get of it, and from my analysis, it seems to be intact. So my idea is to finish confirming that it really is intact before any writes.

My plan for getting clean dumps of all the partitions (so that I can then start the real modifications) is, once the U-Boot's integrity is confirmed, to edit it so that instead of booting the system, it exposes the eMMC as a USB Mass Storage Gadget. If everything goes as expected, once it loads U-Boot into RAM and exposes the eMMC as a USB storage device, I'll be able to take a full dump of it without interference from RockUSB mode.

I’ll proceed with the plan; further updates will be made in new files.

/ -- /

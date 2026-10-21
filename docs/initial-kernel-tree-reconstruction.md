# Initial Kernel Tree Reconstruction #
User: v1ruzzz1_0  
Date: 10/21/2026  
Status: Completed/In Progress

Finally, after a week on the project, I got a real step forward on my fenix.

I tested something curious: I cloned the Rockchip downstream tree closest to the one my fenix uses (based on Linux 4.19.232, which is the same kernel version used by the fenix's original firmware). I extracted the '.config' from the original kernel image and tried to build the tree with that configuration, tidying up the .config a bit (it's not fully ready yet, I still need to see what to add and remove) and renaming it to 'fenix-rgo_defconfig' (nothing more than that, for now).

The tree used for these tests was this one:

- repository: https://github.com/rockchip-linux/kernel
- branch: develop-4.19
- commit: f39b590ea11da2d897644e42119ba5099b2eefc8

# Build

As a test, I decided to build with Clang 12.0.5 (the oldest one I had installed), but it turned out I had to deal with several kinds of Werrors, mainly in the Imagination Rogue GPU driver (even though the fenix's GPU is a Mali-G31 MP2), which were blocking the build, and which the older compilers from that tree's era ignored (GCC Linaro 6.3, which was used by the original firmware). So I temporarily disabled the Werrors of the strict-prototypes class (using 'KCFLAGS="-Wno-error=strict-prototypes"').

Then I finally managed to generate the compiled artifact. Then I extracted the original boot.img with magiskboot, replaced the kernel image (without replacing the dtb or editing dtbo.img), and repacked the image.

But I faced another problem: how would I test this image?

# New Tools

One of the problems I identified was that RockUSB Mode, in my earlier attempts, was corrupting the dumps, so I couldn't just trust it blindly. So I thought of another alternative.

So I decided to create a tool called 'fenix-flash'.

Basically, this new tool works essentially the same way as 'fenix-dump', but in reverse: instead of reading the partitions, it writes them.

Oh, and another tool I created is considerably simple, but useful. The fenix's default U-Boot has a problem: the timeout to interrupt the boot (and drop into the U-Boot shell) is extremely short, so the only viable way to interrupt the boot was a script that sends a spam of 'ctrl + c' to increase the chances of stopping the boot successfully.

It's called 'stop-uboot'.

# Test on the fenix

Right after writing the modified boot.img partition with the reconstructed kernel, I tried to start the fenix. I still wasn't very hopeful that it would even boot until.....

IT BOOTED ANDROID!!!

# Confirmation

As soon as I managed to bring up the image with my modified (and reconstructed, which by the way wasn't really a literal reconstruction, it was too easy) kernel, I decided to run some tests to confirm:

1. Kernel confirmation: 4.19.232 v1ruzzz1_0@SYS-CORE clang 12.0.5 (https:XXXXXXX) LLD 12.0.5 (https:XXXXXXX) #1 SMP PREEMPT Sun Oct 18 21:00:06 -03 2026
2. Minimum hardware (CPU, GPU, Display, Audio, Connectors, etc.): Partially

# Problems

Some of the problems I identified were:

1. Physical power button not working, requiring me to power on with the remote (theory: the physical signal's function may not be mapped or assigned to anything)
2. When pressing the power on/off button on the remote, the board shut down completely instead of going into standby, requiring an electrical reset for the board to return to standby mode. (Theory: there was a function that redirected shutdown calls to a 'standby' mode, in which the CPU could go into a waiting state).

Other than that, I haven't found (yet) any more problems. And considering this was an experimental attempt, it was better than expected (I wasn't even hopeful it would reach the boot logo).

# Initial Refinement

To make it minimally acceptable for me to post in the next commit (this Markdown will be posted along with it), besides organizing a lot of things, I also extracted the dtb used by the 'boot.img' and set it up to be built together with the tree. In this case I didn't need to redo the test, since I only decompiled the dtb and renamed it to fenix-rgo.dts (the most I tested was checking that it compiled).

# fenix-dump Improvements

Another thing I did was a few small improvements to fenix-dump, which was adding a message recommending the use of sudo (including in fenix-flash and stop-uboot).

Besides that, they were just imperceptible code changes to improve consistency.

# Wrapping Up

I still don't consider this tree "finished", but rather a proof of concept (PoC). There's still a lot to improve in this tree, but so far this achievement has been a considerable step forward for the hy300-lite project.

/ -- /

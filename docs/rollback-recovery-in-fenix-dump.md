# Rollback Recovery in fenix-dump #
User: v1ruzzz1_0
Date: 10/15/2026
Status: Completed/In Progress

While extracting the fenix firmware, I kept developing the fenix-dump tool to make UART transfers more reliable, especially for dumps of large partitions (which take hours, even days, and need a lot of stability the whole time).

So I ended up finishing version '1.1.0'! Here's what's new.

# CRC Rollback Implementation

One of the improvements I implemented was a recovery mechanism for persistent CRC failures during data transfer.

The idea came right after two failed dump attempts on the super partition. In the first attempt there was a power outage, so I preferred to start everything over from the beginning, since there was no way to continue, as the script didn't have rollback yet. In the second attempt it was electrical instability, so I realized it wouldn't be practical to always reset the progress, hence the new feature in this version.

Before, if part of the saved dump was damaged, the progress was doomed for lack of rollback, which was totally annoying, because restarting hours, or even days, of dumping over a few defective blocks is extremely tiring.

Now, when a persistent CRC mismatch occurs, the new rollback mechanism searches backward for an earlier point until it finds an intact area. Once it finds one, it has to validate two blocks of it to make sure it's a sound rollback point, then it resumes progress from that point, discarding the later data classified as invalid. The rollback is limited to 16 MiB before the problematic region. If it reaches the limit of problematic blocks and no valid block is identified, the script aborts.

The mechanism doesn't guarantee recovery from every kind of failure, but it adds an extra layer of protection against faulty dumps.

# New Versioning Scheme

With this new version, since I had to move on from '1.0.0', I ended up defining a standardized versioning scheme to better organize future updates.

The format will be X.Y.Z, where each digit has its own purpose:

- First digit (base version): identifies a generation of the script. It usually only changes when the second digit rolls over (that is, from nine back to 0). Major structural changes, but related to the second digit. By itself, the first digit doesn't directly drive changes.
- Second digit (sub-version): identifies main changes, such as implementing new features, improvements to how it works, and fixes for architectural problems.
- Third digit (hotfixes): reserved for quick (usually small) fixes for problems caused by sub-version updates, such as a function that starts failing after another change, broken syntax, and the like. I hope I rarely have to resort to this.

The goal is to keep a more organized and clearer history of changes and bug fixes.

# Current Dump Progress

At the time of this update, the extraction of the super partition has already passed 60% (a record, considering that the maximum on the previous version, '1.0.0', was 53.4%), and the process keeps moving forward without problems.

The speed remains stable at roughly 30.5 KiB/s, with about eight hours estimated remaining. Even with a bit of variation, that's the margin it's holding.

The expectation is to finish the extraction in the next few hours without problems, as long as the transfer keeps working normally as it is. As soon as this partition finishes being extracted, the next one will be userdata.

For now, the priority remains finishing the dumps and verifying the files obtained before moving on to bigger modifications to the system.

# fenix-dump Evolution

I've noticed the tool is evolving in an impressive way, and in a sense it's even needlessly good for just this project.

So I think that in the future I could extend the tool (if I manage to test it on other devices with U-Boot) to become a partition dumper for devices with U-Boot, not just for my fenix (hy300-lite).

Because this tool is too robust to stay stuck in a single project. I'll consider going in that direction in the future.

/ -- /

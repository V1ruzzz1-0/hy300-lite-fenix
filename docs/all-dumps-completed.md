# All Dumps Completed #
User: v1ruzzz1_0
Date: 10/17/2026
Status: Completed

Finally! After so long, all the fenix dumps are done (Batch 3 was finally completed, bringing in userdata). All the partitions I needed to extract are now on my computer and in the repository (if you're seeing this).

Now I can truthfully say we have a COMPLETE dump of the fenix's eMMC, which is a hugely important milestone for the project.

Honestly, I didn't expect this stage to take THIS LONG! The estimate was a little over 3 days, but it ended up taking almost 6 DAYS! That's just the UART method, since I'm still not counting all the investigation done beforehand.

A lot happened during that time, including power outages, CRC failures, and the need to improve the tool in the middle of the process. I ended up having to develop solutions so I could finish everything without worrying that a micro-instability in the middle of the night would wipe out all the progress.

Seriously, I spent a few nights not sleeping properly for fear that an instability would cause a CRC mismatch (it was one of the reasons I built that upgrade for the tool; after that I was able to relax better, but I was still wary of something failing).

By the way, the process was so long that I felt like I was trying to escape from prison with a spoon.

As for file integrity, I haven't done an individual verification of each one yet. Even so, I trust my tool enough to consider this stage done for now. I don't want to get stuck on this stage again (considering the problems I had), since I already have the files in hand and can move forward. I'll only consider doing a re-dump or a check when there's a real need, like a problem with damaged blocks, or when the project is mature enough to treat these images as reliable sources for recovering projectors with a faulty system using the stock ROM.

# Now What?

Honestly, that's a question I still need to answer. With all the dumps done, I have a base to start investigating the system with a lot more freedom. But before thinking about the next step, I think I need to rest.

My ThinkPad T480 was on for almost five days without a single shutdown. Poor thing. It deserves a break after keeping me company through this whole process.

By the way, going so long without being able to use my laptop normally was pretty draining, mainly because I ended up having to code straight from my phone for a good part of that period. You can do a lot that way, but it's not exactly the most pleasant experience when you're working on code for hours on end.

So, for now, I think the best thing to do is to stop for a bit, let the T480 rest, and get my own rhythm back before deciding what comes next.

What matters is that this stage is over. The dumps are done, the tool evolved a lot along the way, and now I have the files I need to keep working on the fenix.

For the first time since I started this extraction, I don't have a partition waiting in the queue.

And honestly, that already feels great.

/ -- /
